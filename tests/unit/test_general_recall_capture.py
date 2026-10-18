"""Deployment capture must independently read inputs before model work."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_recall_capture import capture_deployment
from agent_core.memory.semantic_deployment import IndexConfiguration, SemanticCompositionInputs
from tests.contract.test_local_semantic_preparation import CONFIG, LOCAL
from tests.integration.m2_support import memory_settings


@pytest.fixture
def capture_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    from agent_core.evals import general_recall_composition

    model = tmp_path / "model.onnx"
    tokenizer = tmp_path / "tokenizer.json"
    model.write_bytes(b"synthetic model bytes")
    tokenizer.write_bytes(b"synthetic tokenizer bytes")
    declared = IndexConfiguration(
        embedding=CONFIG, local=LOCAL, model_directory=tmp_path, reader_sha256="a" * 64
    )
    assert declared.local is not None
    local = declared.local.model_copy(
        update={
            "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(),
        }
    )
    declared = declared.model_copy(
        update={
            "local": local,
            "embedding": declared.embedding.model_copy(update={"space": local.space}),
        }
    )
    directory = tmp_path / "config" / "memory"
    directory.mkdir(parents=True)
    (directory / "semantic-index.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "configurations": [declared.model_dump(mode="json")],
            }
        )
    )
    settings = replace(
        memory_settings(),
        config_dir=directory.parent,
        database_url="postgresql+asyncpg://localhost/production",
        memory_semantic_index_enabled=True,
    )
    scratch = replace(
        settings, database_url="postgresql+asyncpg://localhost/veetbot_m33_eval_capture"
    )
    actual = SemanticCompositionInputs(
        configuration={
            "principal": {
                "tenant": "actual",
                "owner": "owner",
                "scopes": ["memory:read"],
                "roles": ["user"],
            }
        },
        formation={"default": "resolved formation"},
        reader={"catalog": "resolved reader"},
    )
    seen = []

    @asynccontextmanager
    async def build(**kwargs: Any) -> Any:
        seen.append(kwargs)
        kwargs["recall_capture"](actual)
        pytest.fail("Capture must stop before exposing a runtime")
        yield

    monkeypatch.setattr(general_recall_composition, "build", build)
    return settings, scratch, declared, actual, seen


async def test_capture_uses_actual_graph_and_only_writes_scratch(
    capture_inputs: tuple[Any, ...],
) -> None:
    settings, scratch, profile, actual, seen = capture_inputs
    result = await capture_deployment(settings, scratch, authorized=True)
    assert result["dependencies"] == actual.dependencies(profile)
    assert result["composition"]["configuration"] == actual.configuration
    assert result["indexing"] == profile.model_dump(mode="json")
    assert seen[0]["settings"].database_url == scratch.database_url
    assert seen[0]["settings"].artifact_root != settings.artifact_root
    assert seen[0]["settings"].memory_semantic_index_enabled
    assert seen[0]["model_metadata_only"] and seen[0]["storage"] == "postgres"
    assert "principal" not in seen[0], "Bootstrap must resolve actual configured authority"
    assert "database_url" not in json.dumps(result)


@pytest.mark.parametrize(
    "failure", ["unauthorized", "same_database", "wrong_name", "model", "tokenizer", "symlink"]
)
async def test_invalid_capture_stops_before_composition(
    capture_inputs: tuple[Any, ...], failure: str
) -> None:
    settings, scratch, profile, _, seen = capture_inputs
    if failure == "same_database":
        settings = replace(settings, database_url=scratch.database_url)
    elif failure == "wrong_name":
        scratch = replace(scratch, database_url=settings.database_url)
    elif failure in {"model", "tokenizer", "symlink"}:
        path = profile.model_directory / (
            "tokenizer.json" if failure == "tokenizer" else "model.onnx"
        )
        if failure == "symlink":
            target = path.with_suffix(".original")
            path.rename(target)
            path.symlink_to(target)
        else:
            path.write_bytes(b"changed asset")
    with pytest.raises(ValueError, match=r"scratch|asset"):
        await capture_deployment(settings, scratch, authorized=failure != "unauthorized")
    assert not seen
