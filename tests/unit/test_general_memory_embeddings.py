from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_memory_embeddings import decode_vectors


def body() -> dict[str, Any]:
    return {
        "data": [{"index": 1, "embedding": [0.0, 4.0]}, {"index": 0, "embedding": [3.0, 0.0]}],
        "usage": {"total_tokens": 10},
    }


def test_provider_order_is_verified_and_vectors_normalized() -> None:
    assert decode_vectors(body(), count=2, dimensions=2) == ((1.0, 0.0), (0.0, 1.0))


@pytest.mark.parametrize(
    "mutation", ["duplicate", "missing", "nan", "zero", "wrong_dimension", "usage"]
)
def test_invalid_embedding_batches_cannot_enter_the_comparison(mutation: str) -> None:
    payload = body()
    if mutation == "duplicate":
        payload["data"][1]["index"] = 1
    elif mutation == "missing":
        payload["data"].pop()
    elif mutation == "nan":
        payload["data"][0]["embedding"][0] = float("nan")
    elif mutation == "zero":
        payload["data"][0]["embedding"] = [0.0, 0.0]
    elif mutation == "wrong_dimension":
        payload["data"][0]["embedding"].append(1.0)
    else:
        payload["usage"]["total_tokens"] = -1
    with pytest.raises(ValueError):
        decode_vectors(payload, count=2, dimensions=2)


async def test_budget_rejection_happens_before_provider_send() -> None:
    from decimal import Decimal

    import httpx

    from agent_core.evals.general_memory_embeddings import embed

    sends = []

    def handle(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        raise AssertionError("unfunded send")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match="spend ceiling"):
            await embed(
                ("x" * 100,), api_key="synthetic", ceiling=Decimal("0.0000001"), client=client
            )
    assert not sends


async def test_provider_failure_is_not_retried_or_exposed() -> None:
    from decimal import Decimal

    import httpx

    from agent_core.evals.general_memory_embeddings import embed

    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="private upstream diagnostic", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match="status 429; response withheld") as raised:
            await embed(("synthetic",), api_key="synthetic", ceiling=Decimal("0.01"), client=client)
    assert calls == 1
    assert "private" not in str(raised.value)


async def test_experiment_uses_the_repository_provider_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json
    from decimal import Decimal

    from agent_core.evals import general_memory_embeddings as experiment

    seeds = tmp_path / "seeds.tsv"
    seeds.write_text("preferences\tdiet\tUser eats plants.\tDinner?\n")
    monkeypatch.setattr(experiment, "SEEDS", seeds)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-generic-credential")
    monkeypatch.setenv("VEETBOT_OPENAI_KEY", "repository-provider-credential")
    sparse = tmp_path / "sparse.json"
    sparse.write_text(
        json.dumps(
            {
                "seed_sha256": hashlib.sha256(seeds.read_bytes()).hexdigest(),
                "version": "general-recall-diagnostic@1",
                "noise": 0,
            }
        )
    )

    class StopAfterCredentialSelectionError(Exception):
        pass

    async def intercepted(*args, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["api_key"] == "repository-provider-credential"
        raise StopAfterCredentialSelectionError

    monkeypatch.setattr(experiment, "embed", intercepted)
    with pytest.raises(StopAfterCredentialSelectionError):
        await experiment.compare(sparse, tmp_path / "out.json", Decimal("0.05"))
