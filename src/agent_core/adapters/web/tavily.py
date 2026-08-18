"""Tavily implementation of the provider-neutral web port."""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from agent_core.adapters.web.common import (
    optional_string,
    post_json,
    required_list,
    required_mapping,
    required_string,
)
from agent_core.domain.web import WebPage, WebProviderError, WebSearchRequest, WebSearchResult
from agent_core.ports.credentials import CredentialResolver


class TavilyWebProvider:
    name = "tavily"

    def __init__(
        self,
        *,
        credentials: CredentialResolver,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials = credentials
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]:
        payload: dict[str, object] = {
            "query": request.query,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": request.max_results,
        }
        if request.include_domains:
            payload["include_domains"] = list(request.include_domains)
        if request.exclude_domains:
            payload["exclude_domains"] = list(request.exclude_domains)
        if request.recency is not None:
            payload["time_range"] = request.recency.value
        response = await post_json(
            self._client,
            self._credentials,
            credential_name=self.name,
            url="https://api.tavily.com/search",
            payload=payload,
        )
        try:
            results = tuple(
                WebSearchResult(
                    title=optional_string(item.get("title"), fallback="Untitled result")[:1024],
                    url=required_string(item.get("url")),
                    snippet=optional_string(item.get("content"))[:8192],
                )
                for raw in required_list(response.get("results"))
                for item in (required_mapping(raw),)
            )
        except ValidationError as exc:
            raise WebProviderError("tool.web.output_invalid", retryable=False) from exc
        return results

    async def fetch(self, url: str) -> WebPage:
        response = await post_json(
            self._client,
            self._credentials,
            credential_name=self.name,
            url="https://api.tavily.com/extract",
            payload={
                "urls": [url],
                "extract_depth": "basic",
                "include_images": False,
                "format": "markdown",
            },
        )
        raw_results = required_list(response.get("results"))
        if len(raw_results) != 1:
            raise WebProviderError("tool.web.provider_rejected", retryable=False)
        result = required_mapping(raw_results[0])
        try:
            return WebPage(
                url=required_string(result.get("url")),
                content=required_string(result.get("raw_content"))[:524_288],
            )
        except ValidationError as exc:
            raise WebProviderError("tool.web.output_invalid", retryable=False) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
