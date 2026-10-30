"""Offline inference verifies files, preserves ordering and bounds native work."""

import asyncio
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from agent_core.adapters.models.local_embeddings import LocalEmbeddingProvider
from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest
from agent_core.domain.local_embeddings import LocalEmbeddingConfig


@pytest.fixture
def local_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    tokenizer = Tokenizer(WordLevel({"[UNK]": 0, "[PAD]": 1, "breakfast": 2, "early": 3}))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "model.onnx").write_bytes(b"unit-test-model")
    state: dict[str, Any] = {"calls": 0, "started": threading.Event(), "release": threading.Event()}
    state["release"].set()

    class Session:
        def __init__(self, model: bytes, **options: Any) -> None:
            assert model == b"unit-test-model"
            assert options["providers"] == ["CPUExecutionProvider"]
            state["options"] = options

        def get_inputs(self) -> list[Any]:
            return [
                SimpleNamespace(name=n) for n in ("input_ids", "attention_mask", "token_type_ids")
            ]

        def get_outputs(self) -> list[Any]:
            return [SimpleNamespace(name="last_hidden_state", shape=[None, None, 3])]

        def run(self, _: Any, inputs: dict[str, Any]) -> list[Any]:
            state["calls"] += 1
            state["started"].set()
            assert state["release"].wait(3), "Test must release its simulated native inference."
            ids = inputs["input_ids"].astype(float)
            output = np.stack([ids, ids * 2, np.ones_like(ids)], axis=-1)
            return [state.get("output", output)]

    monkeypatch.setattr("onnxruntime.InferenceSession", Session)
    config = LocalEmbeddingConfig(
        model="test-local",
        dimensions=3,
        maximum_tokens=8,
        threads=1,
        model_sha256=hashlib.sha256((tmp_path / "model.onnx").read_bytes()).hexdigest(),
        tokenizer_sha256=hashlib.sha256((tmp_path / "tokenizer.json").read_bytes()).hexdigest(),
    )
    return tmp_path, config, state


async def test_masked_pooling_order_and_usage_are_verified_without_network(
    local_model: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, config, state = local_model

    def no_network(*_: Any, **__: Any) -> None:
        pytest.fail("Offline embedding must not open a network connection.")

    monkeypatch.setattr("socket.create_connection", no_network)
    provider = LocalEmbeddingProvider(root, config)
    request = EmbeddingRequest(space=config.space, texts=("breakfast", "early breakfast"))
    response = await provider.embed(request)
    assert response.request_digest == request.digest
    assert response.input_tokens == 3
    vectors = response.validate_for(request)
    np.testing.assert_allclose(vectors[0], np.array([2, 4, 1]) / np.sqrt(21))
    np.testing.assert_allclose(vectors[1], np.array([2.5, 5, 1]) / np.sqrt(32.25))
    assert state["calls"] == 1
    await provider.close()


@pytest.mark.parametrize("tamper", ["changed", "symlink", "missing"])
async def test_unverified_model_files_are_refused(
    local_model: tuple[Any, ...], tamper: str
) -> None:
    root, config, state = local_model
    model = root / "model.onnx"
    if tamper == "changed":
        model.write_bytes(b"different graph")
    elif tamper == "symlink":
        model.rename(root / "elsewhere")
        model.symlink_to(root / "elsewhere")
    else:
        model.unlink()
    with pytest.raises(EmbeddingError):
        LocalEmbeddingProvider(root, config)
    assert state["calls"] == 0


async def test_overlong_inputs_are_rejected_without_truncation(
    local_model: tuple[Any, ...],
) -> None:
    root, config, state = local_model
    provider = LocalEmbeddingProvider(root, config)
    with pytest.raises(EmbeddingError):
        await provider.embed(EmbeddingRequest(space=config.space, texts=("breakfast " * 9,)))
    assert state["calls"] == 0
    await provider.close()


async def test_cancelled_native_work_keeps_its_slot_until_completion(
    local_model: tuple[Any, ...],
) -> None:
    root, config, state = local_model
    state["release"].clear()
    provider = LocalEmbeddingProvider(root, config)
    request = EmbeddingRequest(space=config.space, texts=("breakfast",))
    task = asyncio.create_task(provider.embed(request))
    try:
        for _ in range(100):
            if state["started"].is_set():
                break
            await asyncio.sleep(0.001)
        assert state["started"].is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(EmbeddingError, match="capacity"):
            await provider.embed(request)
        assert state["calls"] == 1
    finally:
        state["release"].set()
        await provider.close()


@pytest.mark.parametrize("bad", ["dimension", "zero", "nan", "boolean", "integer"])
async def test_invalid_native_vectors_are_rejected_without_retry(
    local_model: tuple[Any, ...],
    bad: str,
) -> None:
    root, config, state = local_model
    outputs = {
        "dimension": np.ones((1, 1, 2)),
        "zero": np.zeros((1, 1, 3)),
        "nan": np.full((1, 1, 3), np.nan),
        "boolean": np.ones((1, 1, 3), dtype=bool),
        "integer": np.ones((1, 1, 3), dtype=np.int32),
    }
    state["output"] = outputs[bad]
    provider = LocalEmbeddingProvider(root, config)
    try:
        with pytest.raises(EmbeddingError):
            await provider.embed(EmbeddingRequest(space=config.space, texts=("breakfast",)))
        assert state["calls"] == 1
    finally:
        await provider.close()


async def test_tokenization_does_not_create_a_second_parallel_worker_pool(
    local_model: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, config, _ = local_model

    def forbidden(*_: Any, **__: Any) -> Any:
        raise RuntimeError("Unbounded tokenizer batch pool")

    monkeypatch.setattr(Tokenizer, "encode_batch", forbidden)
    provider = LocalEmbeddingProvider(root, config)
    try:
        response = await provider.embed(
            EmbeddingRequest(space=config.space, texts=("breakfast", "early breakfast"))
        )
        assert response.input_tokens == 3
    finally:
        await provider.close()


@pytest.mark.parametrize("expected", [False, True])
def test_native_model_load_distinguishes_invalid_assets_from_programming_errors(
    local_model: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    expected: bool,
) -> None:
    from onnxruntime.capi.onnxruntime_pybind11_state import (  # type: ignore[import-untyped]
        InvalidProtobuf,
    )

    root, config, _ = local_model

    def fail(*args: Any, **kwargs: Any) -> Any:
        if expected:
            raise InvalidProtobuf("Private model diagnostic")
        raise RuntimeError("Unexpected native adapter implementation failure")

    monkeypatch.setattr("onnxruntime.InferenceSession", fail)
    error = EmbeddingError if expected else RuntimeError
    with pytest.raises(error) as caught:
        LocalEmbeddingProvider(root, config)
    assert "Private model diagnostic" not in str(caught.value)


@pytest.mark.parametrize("contents", [b"not JSON", b"\xff"])
def test_malformed_tokenizer_is_expected_unavailability(
    local_model: tuple[Any, ...],
    contents: bytes,
) -> None:
    root, config, _ = local_model
    (root / "tokenizer.json").write_bytes(contents)
    config = config.model_copy(update={"tokenizer_sha256": hashlib.sha256(contents).hexdigest()})
    with pytest.raises(EmbeddingError, match="tokenizer could not be loaded"):
        LocalEmbeddingProvider(root, config)
