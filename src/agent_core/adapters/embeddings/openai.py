"""Bounded one-shot OpenAI embeddings transport, with no ambient routing."""

import asyncio
import json
from typing import Any

import httpx

from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest, EmbeddingResponse


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate response key")
        value[key] = item
    return value


def _decode(raw: bytes, request: EmbeddingRequest) -> EmbeddingResponse:
    try:
        body = json.loads(raw, object_pairs_hook=_object)
        if body["model"] != request.space.model or body["object"] != "list":
            raise ValueError
        tokens = body["usage"]["prompt_tokens"]
        if (
            type(tokens) is not int
            or tokens != body["usage"]["total_tokens"]
            or type(body["usage"]["total_tokens"]) is not int
        ):
            raise ValueError
        rows = body["data"]
        if type(rows) is not list or len(rows) != len(request.texts):
            raise ValueError
        indexes = [row["index"] for row in rows]
        if any(type(i) is not int for i in indexes) or sorted(indexes) != list(range(len(rows))):
            raise ValueError
        vectors = []
        for row in sorted(rows, key=lambda r: r["index"]):
            values = row["embedding"]
            if (
                row["object"] != "embedding"
                or type(values) is not list
                or any(type(v) not in (int, float) for v in values)
            ):
                raise ValueError
            vectors.append(tuple(values))
        result = EmbeddingResponse(
            request_digest=request.digest, vectors=tuple(vectors), input_tokens=tokens
        )
        return result.model_copy(update={"vectors": result.validate_for(request)})
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        raise EmbeddingError("invalid_response") from None


class OpenAIEmbeddingProvider:
    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key = api_key
        self._transport = transport

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        try:
            request = EmbeddingRequest.model_validate(request.model_dump())
        except ValueError:
            raise EmbeddingError("invalid_request") from None
        if request.space.provider != "openai" or not self._key:
            raise EmbeddingError("provider_unavailable")
        try:
            async with (
                asyncio.timeout(30),
                httpx.AsyncClient(
                    transport=self._transport or httpx.AsyncHTTPTransport(retries=0),
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(30, read=10),
                ) as client,
                client.stream(
                    "POST",
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={
                        "model": request.space.model,
                        "dimensions": request.space.dimensions,
                        "encoding_format": "float",
                        "input": list(request.texts),
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise EmbeddingError("provider_rejected")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 2_097_152:
                        raise EmbeddingError("response_too_large")
                    body.extend(chunk)
            return _decode(bytes(body), request)
        except (httpx.HTTPError, TimeoutError):
            raise EmbeddingError("transport_failed") from None
