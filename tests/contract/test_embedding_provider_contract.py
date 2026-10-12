"""Embedding wire identity, bounded usage, ordering and one-shot failure contract."""

import json
from typing import Any

import httpx
import pytest

from agent_core.adapters.embeddings.openai import OpenAIEmbeddingProvider
from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest
from agent_core.ports.embeddings import EmbeddingProvider
from tests.contract.semantic_memory_cases import SPACE

REQUEST = EmbeddingRequest(
    space=SPACE.model_copy(update={"provider": "openai", "model": "fixture-model"}),
    texts=("avoid dairy", "quiet workspace"),
)


def payload() -> dict[str, Any]:
    return {
        "model": "fixture-model",
        "object": "list",
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
        "data": [
            {"object": "embedding", "index": 1, "embedding": [0, 3, 0]},
            {"object": "embedding", "index": 0, "embedding": [2, 0, 0]},
        ],
    }


async def test_embedding_order_usage_and_exact_wire() -> None:
    calls = []

    async def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        assert str(req.url) == "https://api.openai.com/v1/embeddings"
        assert json.loads(req.content) == {
            "model": "fixture-model",
            "dimensions": 3,
            "encoding_format": "float",
            "input": list(REQUEST.texts),
        }
        return httpx.Response(200, json=payload())

    provider: EmbeddingProvider = OpenAIEmbeddingProvider(
        "synthetic-test-key", transport=httpx.MockTransport(respond)
    )
    response = await provider.embed(REQUEST)
    assert response.validate_for(REQUEST) == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert response.input_tokens == 4 and len(calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "model",
        "duplicate",
        "missing_usage",
        "zero_usage",
        "usage_ceiling",
        "nan",
        "zero_vector",
        "boolean",
        "dimension",
        "redirect",
        "error",
    ],
)
async def test_embedding_rejects_ambiguous_responses_without_retry(bad: str) -> None:
    calls = []

    async def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        body = payload()
        if bad == "model":
            body["model"] = "other"
        if bad == "duplicate":
            body["data"][1]["index"] = 1
        if bad == "missing_usage":
            del body["usage"]
        if bad == "zero_usage":
            body["usage"] = {"prompt_tokens": 0, "total_tokens": 0}
        if bad == "usage_ceiling":
            body["usage"] = {"prompt_tokens": 65536, "total_tokens": 65536}
        if bad == "nan":
            body["data"][0]["embedding"] = [float("nan"), 0, 1]
        if bad == "zero_vector":
            body["data"][0]["embedding"] = [0, 0, 0]
        if bad == "boolean":
            body["data"][0]["embedding"] = [True, 0, 1]
        if bad == "dimension":
            body["data"][0]["embedding"] = [1, 0]
        if bad == "redirect":
            return httpx.Response(302, headers={"location": "https://untrusted.invalid/"})
        if bad == "error":
            return httpx.Response(429, text="private provider echo")
        return httpx.Response(200, content=json.dumps(body).encode())

    provider = OpenAIEmbeddingProvider("synthetic-test-key", transport=httpx.MockTransport(respond))
    with pytest.raises(EmbeddingError) as failure:
        await provider.embed(REQUEST)
    assert len(calls) == 1 and "private provider echo" not in str(failure.value)


async def test_invalid_embedding_request_never_leaks_inputs_or_sends() -> None:
    calls = []

    async def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=payload())

    provider = OpenAIEmbeddingProvider("synthetic-test-key", transport=httpx.MockTransport(respond))
    invalid = REQUEST.model_copy(update={"texts": ("sensitive-example " * 500,)})
    with pytest.raises(EmbeddingError) as failure:
        await provider.embed(invalid)
    assert calls == [] and "sensitive-example" not in str(failure.value)
