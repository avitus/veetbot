"""Keenable implementation of the provider-neutral web port."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from agent_core.adapters.web.common import (
    MAXIMUM_RESPONSE_BYTES,
    optional_string,
    request_json,
    required_list,
    required_mapping,
    required_string,
)
from agent_core.domain.web import WebPage, WebProviderError, WebSearchRequest, WebSearchResult
from agent_core.ports.credentials import CredentialResolver

_MAXIMUM_SNIPPET_CHARACTERS = 8_192
_MAXIMUM_EXCLUDE_FILTERED_RESULTS = 50
_MAXIMUM_CONTENT_CHARACTERS = 524_288
# A search row also carries a title and a URL alongside its snippet.
_MAXIMUM_ROW_OVERHEAD_CHARACTERS = 1_024 + 4_096
# Keenable bounds `snippet_max_length` and `max_chars` in characters, while the
# shared reader bounds the response in bytes. One UTF-8 character occupies at
# most four bytes, so budget the reader for the characters this request asked
# for; otherwise a page of multi-byte text is refused as oversize instead of
# being truncated locally. A provider that escapes non-ASCII beyond that still
# fails closed on the same bound.
_UTF8_MAXIMUM_BYTES_PER_CHARACTER = 4
_ENVELOPE_HEADROOM_BYTES = 64 * 1024


def _response_byte_budget(maximum_characters: int) -> int:
    return max(
        MAXIMUM_RESPONSE_BYTES,
        _UTF8_MAXIMUM_BYTES_PER_CHARACTER * maximum_characters + _ENVELOPE_HEADROOM_BYTES,
    )


_RECENCY_DELTAS = {
    "day": "1d",
    "week": "7d",
    "month": "1mo",
    "year": "1y",
}


def _matches_domain(url: str, domains: tuple[str, ...]) -> bool:
    hostname = urlsplit(url).hostname
    return hostname is not None and any(
        hostname == domain or hostname.endswith("." + domain) for domain in domains
    )


class KeenableWebProvider:
    name = "keenable"

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
        # Keenable accepts one `site` per request. Fan out a bounded request per
        # include domain; exclude filters are enforced after an over-fetched,
        # still-provider-bounded response.
        sites: tuple[str | None, ...] = request.include_domains or (None,)
        maximum_results = (
            _MAXIMUM_EXCLUDE_FILTERED_RESULTS if request.exclude_domains else request.max_results
        )
        results: list[WebSearchResult] = []
        seen_urls: set[str] = set()
        for site in sites:
            payload: dict[str, object] = {
                "query": request.query,
                "snippet_max_length": _MAXIMUM_SNIPPET_CHARACTERS,
                "max_results": maximum_results,
            }
            if site is not None:
                payload["site"] = site
            if request.recency is not None:
                payload["published_after"] = _RECENCY_DELTAS[request.recency.value]
            response = await request_json(
                self._client,
                self._credentials,
                credential_name=self.name,
                method="POST",
                url="https://api.keenable.ai/v1/search",
                payload=payload,
                credential_header="X-API-Key",
                credential_prefix="",
                auth_failure_statuses=frozenset({400, 401, 403}),
                maximum_response_bytes=_response_byte_budget(
                    maximum_results
                    * (_MAXIMUM_SNIPPET_CHARACTERS + _MAXIMUM_ROW_OVERHEAD_CHARACTERS)
                ),
            )
            try:
                normalized = tuple(
                    WebSearchResult(
                        title=optional_string(item.get("title"), fallback="Untitled result")[:1024],
                        url=required_string(item.get("url")),
                        snippet=optional_string(
                            item.get("snippet"),
                            fallback=optional_string(item.get("description")),
                        )[:_MAXIMUM_SNIPPET_CHARACTERS],
                    )
                    for raw in required_list(response.get("results"))
                    for item in (required_mapping(raw),)
                )
            except ValidationError as exc:
                raise WebProviderError("tool.web.output_invalid", retryable=False) from exc
            for result in normalized:
                if request.exclude_domains and _matches_domain(result.url, request.exclude_domains):
                    continue
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                results.append(result)
        return tuple(results[: request.max_results])

    async def fetch(self, url: str) -> WebPage:
        response = await request_json(
            self._client,
            self._credentials,
            credential_name=self.name,
            method="GET",
            url="https://api.keenable.ai/v1/fetch",
            params={
                "url": url,
                "max_chars": _MAXIMUM_CONTENT_CHARACTERS,
                "live": "true",
            },
            credential_header="X-API-Key",
            credential_prefix="",
            auth_failure_statuses=frozenset({400, 401, 403}),
            maximum_response_bytes=_response_byte_budget(_MAXIMUM_CONTENT_CHARACTERS),
        )
        title = response.get("title")
        try:
            return WebPage(
                url=required_string(response.get("url")),
                title=title[:1024] if isinstance(title, str) else None,
                content=required_string(response.get("content"))[:_MAXIMUM_CONTENT_CHARACTERS],
            )
        except ValidationError as exc:
            raise WebProviderError("tool.web.output_invalid", retryable=False) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
