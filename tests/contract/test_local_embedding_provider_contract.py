"""The local embedding port preserves the shared remote adapter contract."""

import json
import math
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from agent_core.adapters.embeddings.openai import OpenAIEmbeddingProvider
from agent_core.adapters.models.local_embeddings import LocalEmbeddingProvider
from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest
from agent_core.ports.embeddings import EmbeddingProvider
from tests.unit.test_local_embeddings import local_model  # noqa: F401


@pytest.fixture(params=["remote", "local"])
async def adapter(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[EmbeddingProvider, EmbeddingRequest, list[object]]]:
    root, config, state = request.getfixturevalue("local_model")
    calls: list[object] = []
    if request.param == "local":
        local = LocalEmbeddingProvider(root, config)

        class Observed:
            async def embed(self, value: EmbeddingRequest) -> Any:
                previous = state["calls"]
                try:
                    return await local.embed(value)
                finally:
                    calls.extend([None] * (state["calls"] - previous))

        try:
            yield (
                Observed(),
                EmbeddingRequest(space=config.space, texts=("breakfast", "early breakfast")),
                calls,
            )
        finally:
            await local.close()
    else:

        def respond(value: httpx.Request) -> httpx.Response:
            calls.append(value)
            payload = json.loads(value.content)
            return httpx.Response(
                200,
                json={
                    "model": payload["model"],
                    "object": "list",
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [float(i + 1), 1.0, 1.0]}
                        for i in reversed(range(len(payload["input"])))
                    ],
                },
            )

        remote = OpenAIEmbeddingProvider("synthetic", transport=httpx.MockTransport(respond))
        space = config.space.model_copy(update={"provider": "openai"})
        yield remote, EmbeddingRequest(space=space, texts=("breakfast", "early breakfast")), calls


async def test_bounded_ordered_normalized_vectors_and_usage(adapter: tuple[Any, ...]) -> None:
    provider, request, calls = adapter
    response = await provider.embed(request)
    vectors = response.validate_for(request)
    assert response.request_digest == request.digest
    assert 0 < response.input_tokens <= request.maximum_tokens
    assert len(vectors) == len(request.texts)
    assert vectors[0] != vectors[1]
    assert all(math.isclose(math.hypot(*vector), 1.0) for vector in vectors)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "texts", [(), (" ",), ("synthetic-sensitive " * 500,), ("breakfast",) * 33]
)
async def test_forged_invalid_inputs_never_reach_inference(
    adapter: tuple[Any, ...], texts: tuple[str, ...]
) -> None:
    provider, request, calls = adapter
    invalid = request.model_copy(update={"texts": texts})
    with pytest.raises(EmbeddingError) as failure:
        await provider.embed(invalid)
    assert calls == []
    assert "synthetic-sensitive" not in str(failure.value)
