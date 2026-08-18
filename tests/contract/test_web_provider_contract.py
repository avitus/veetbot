"""Shared behavioral contract for the Tavily and Firecrawl web adapters."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.adapters.web.common import MAXIMUM_RESPONSE_BYTES
from agent_core.adapters.web.firecrawl import FirecrawlWebProvider
from agent_core.adapters.web.tavily import TavilyWebProvider
from agent_core.domain.web import WebProviderError, WebRecency, WebSearchRequest
from agent_core.ports.web import WebProvider

ProviderFactory = Callable[[httpx.AsyncClient], WebProvider]


def _provider_factories() -> tuple[tuple[str, ProviderFactory], ...]:
    credentials = MappingCredentialResolver(
        {
            "tavily": "synthetic-tavily-credential",
            "firecrawl": "synthetic-firecrawl-credential",
        }
    )
    return (
        (
            "tavily",
            lambda client: TavilyWebProvider(credentials=credentials, client=client),
        ),
        (
            "firecrawl",
            lambda client: FirecrawlWebProvider(credentials=credentials, client=client),
        ),
    )


@pytest.mark.parametrize(("provider_name", "factory"), _provider_factories())
async def test_web_provider_search_contract(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    observed: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        payload: dict[str, object]
        if provider_name == "tavily":
            payload = {
                "results": [
                    {
                        "title": "Ada Lovelace",
                        "url": "https://example.org/ada",
                        "content": "A public biographical record.",
                    }
                ]
            }
        else:
            payload = {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Ada Lovelace",
                            "url": "https://example.org/ada",
                            "description": "A public biographical record.",
                        }
                    ]
                },
            }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        provider = factory(client)
        results = await provider.search(
            WebSearchRequest(
                query="Ada Lovelace",
                max_results=3,
                include_domains=("example.org",),
                recency=WebRecency.MONTH,
            )
        )

    assert provider.name == provider_name
    assert [result.model_dump(mode="json") for result in results] == [
        {
            "title": "Ada Lovelace",
            "url": "https://example.org/ada",
            "snippet": "A public biographical record.",
        }
    ]
    assert len(observed) == 1
    request = observed[0]
    assert request.headers["authorization"].startswith("Bearer synthetic-")
    body = json.loads(request.content)
    if provider_name == "tavily":
        assert request.url == "https://api.tavily.com/search"
        assert body == {
            "query": "Ada Lovelace",
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": 3,
            "include_domains": ["example.org"],
            "time_range": "month",
        }
    else:
        assert request.url == "https://api.firecrawl.dev/v2/search"
        assert body == {
            "query": "Ada Lovelace",
            "limit": 3,
            "sources": ["web"],
            "includeDomains": ["example.org"],
            "tbs": "qdr:m",
            "ignoreInvalidURLs": True,
        }


@pytest.mark.parametrize(("provider_name", "factory"), _provider_factories())
async def test_web_provider_fetch_contract(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    observed: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        payload: dict[str, object]
        if provider_name == "tavily":
            payload = {
                "results": [
                    {
                        "url": "https://example.org/ada",
                        "raw_content": "# Ada Lovelace\n\nA public biographical record.",
                    }
                ],
                "failed_results": [],
            }
        else:
            payload = {
                "success": True,
                "data": {
                    "markdown": "# Ada Lovelace\n\nA public biographical record.",
                    "metadata": {
                        "title": "Ada Lovelace",
                        "sourceURL": "https://example.org/ada",
                    },
                },
            }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        page = await factory(client).fetch("https://example.org/ada")

    assert page.url == "https://example.org/ada"
    assert page.content == "# Ada Lovelace\n\nA public biographical record."
    assert page.title == (None if provider_name == "tavily" else "Ada Lovelace")
    assert len(observed) == 1
    body = json.loads(observed[0].content)
    if provider_name == "tavily":
        assert body == {
            "urls": ["https://example.org/ada"],
            "extract_depth": "basic",
            "include_images": False,
            "format": "markdown",
        }
    else:
        assert body == {
            "url": "https://example.org/ada",
            "formats": ["markdown"],
            "onlyMainContent": True,
            "skipTlsVerification": False,
        }


@pytest.mark.parametrize(("provider_name", "factory"), _provider_factories())
async def test_web_provider_never_exposes_upstream_error_text(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    upstream_diagnostic = "upstream-private-diagnostic"

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, text=upstream_diagnostic)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert upstream_diagnostic not in str(raised.value)
    assert raised.value.reason_code == "tool.web.provider_unavailable"
    assert raised.value.retryable is True


@pytest.mark.parametrize(("provider_name", "factory"), _provider_factories())
async def test_web_provider_rejected_credential_is_a_stable_auth_failure(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    del provider_name

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, text="invalid api key for account 12345")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.auth_failed"
    assert raised.value.retryable is False


@pytest.mark.parametrize(("provider_name", "factory"), _provider_factories())
async def test_web_provider_bounds_oversized_responses(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    del provider_name
    oversized = b'{"padding":"' + b"a" * (MAXIMUM_RESPONSE_BYTES + 1) + b'"}'

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=oversized)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.output_invalid"
    assert raised.value.retryable is False


@pytest.mark.parametrize(("provider_name", "factory"), _provider_factories())
async def test_web_provider_normalizes_permanent_rejections(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    del provider_name

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(422, text="provider-specific rejection")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.provider_rejected"
    assert raised.value.retryable is False


@pytest.mark.parametrize(("provider_name", "factory"), _provider_factories())
async def test_web_provider_normalizes_invalid_success_output(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        payload: dict[str, object] = (
            {"results": "not-a-list"}
            if provider_name == "tavily"
            else {"success": True, "data": {"web": "not-a-list"}}
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.output_invalid"
    assert raised.value.retryable is False


@pytest.mark.parametrize(("provider_name", "factory"), _provider_factories())
async def test_web_provider_normalizes_transport_failures(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    del provider_name

    async def wire(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic transport failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.provider_unavailable"
    assert raised.value.retryable is True


@pytest.mark.parametrize("provider_name", ["tavily", "firecrawl"])
async def test_web_provider_missing_credential_is_a_stable_auth_failure(
    provider_name: str,
) -> None:
    credentials = MappingCredentialResolver({})

    async def reject_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network called without credential: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject_network)) as client:
        provider: WebProvider = (
            TavilyWebProvider(credentials=credentials, client=client)
            if provider_name == "tavily"
            else FirecrawlWebProvider(credentials=credentials, client=client)
        )
        with pytest.raises(WebProviderError) as raised:
            await provider.search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.auth_failed"
    assert raised.value.retryable is False
