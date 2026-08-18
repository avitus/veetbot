"""Firecrawl implementation of the provider-neutral web port."""

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


class FirecrawlWebProvider:
    name = "firecrawl"

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
            "limit": request.max_results,
            "sources": ["web"],
            "ignoreInvalidURLs": True,
        }
        if request.include_domains:
            payload["includeDomains"] = list(request.include_domains)
        if request.exclude_domains:
            payload["excludeDomains"] = list(request.exclude_domains)
        if request.recency is not None:
            payload["tbs"] = {
                "day": "qdr:d",
                "week": "qdr:w",
                "month": "qdr:m",
                "year": "qdr:y",
            }[request.recency.value]
        response = await post_json(
            self._client,
            self._credentials,
            credential_name=self.name,
            url="https://api.firecrawl.dev/v2/search",
            payload=payload,
        )
        if response.get("success") is not True:
            raise WebProviderError("tool.web.provider_rejected", retryable=False)
        data = required_mapping(response.get("data"))
        try:
            results = tuple(
                WebSearchResult(
                    title=optional_string(item.get("title"), fallback="Untitled result")[:1024],
                    url=required_string(item.get("url")),
                    snippet=optional_string(item.get("description"))[:8192],
                )
                for raw in required_list(data.get("web"))
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
            url="https://api.firecrawl.dev/v2/scrape",
            payload={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "skipTlsVerification": False,
            },
        )
        if response.get("success") is not True:
            raise WebProviderError("tool.web.provider_rejected", retryable=False)
        data = required_mapping(response.get("data"))
        metadata = required_mapping(data.get("metadata"))
        title = metadata.get("title")
        try:
            return WebPage(
                url=optional_string(metadata.get("sourceURL"), fallback=url),
                title=title[:1024] if isinstance(title, str) else None,
                content=required_string(data.get("markdown"))[:524_288],
            )
        except ValidationError as exc:
            raise WebProviderError("tool.web.output_invalid", retryable=False) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
