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


def provider_factories() -> tuple[tuple[str, ProviderFactory], ...]:
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
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


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
async def test_web_provider_maps_exclude_domains_and_every_recency(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    observed: list[httpx.Request] = []
    empty: dict[str, object] = (
        {"results": []} if provider_name == "tavily" else {"success": True, "data": {"web": []}}
    )

    async def wire(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=empty)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        provider = factory(client)
        for recency in WebRecency:
            assert (
                await provider.search(
                    WebSearchRequest(
                        query="Ada Lovelace",
                        exclude_domains=("tracker.example",),
                        recency=recency,
                    )
                )
                == ()
            )

    recency_values = [json.loads(request.content) for request in observed]
    if provider_name == "tavily":
        assert all(body["exclude_domains"] == ["tracker.example"] for body in recency_values)
        assert all("include_domains" not in body for body in recency_values)
        assert [body["time_range"] for body in recency_values] == ["day", "week", "month", "year"]
    else:
        assert all(body["excludeDomains"] == ["tracker.example"] for body in recency_values)
        assert all("includeDomains" not in body for body in recency_values)
        assert [body["tbs"] for body in recency_values] == ["qdr:d", "qdr:w", "qdr:m", "qdr:y"]


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
async def test_web_provider_rejects_unsuccessful_success_shaped_bodies(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    """A 200 body that reports failure or the wrong arity is a permanent rejection."""

    bodies: list[dict[str, object]] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=bodies[-1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        provider = factory(client)
        if provider_name == "tavily":
            for raw_results in ([], [{"url": "a"}, {"url": "b"}]):
                bodies.append({"results": raw_results})
                with pytest.raises(WebProviderError) as raised:
                    await provider.fetch("https://example.org/ada")
                assert raised.value.reason_code == "tool.web.provider_rejected"
                assert raised.value.retryable is False
        else:
            bodies.append({"success": False, "data": {}})
            with pytest.raises(WebProviderError) as search_raised:
                await provider.search(WebSearchRequest(query="Ada Lovelace"))
            assert search_raised.value.reason_code == "tool.web.provider_rejected"
            with pytest.raises(WebProviderError) as fetch_raised:
                await provider.fetch("https://example.org/ada")
            assert fetch_raised.value.reason_code == "tool.web.provider_rejected"
            assert fetch_raised.value.retryable is False


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
async def test_web_provider_rejects_result_rows_that_fail_domain_validation(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    row = {"title": "t", "url": "https://127.0.0.1/private", "content": "x", "description": "x"}

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        payload: dict[str, object] = (
            {"results": [row]}
            if provider_name == "tavily"
            else {"success": True, "data": {"web": [row]}}
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.output_invalid"
    assert raised.value.retryable is False


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
@pytest.mark.parametrize(
    ("status", "reason_code", "retryable"),
    [
        (402, "tool.web.quota_exceeded", False),
        (403, "tool.web.auth_failed", False),
        (408, "tool.web.provider_unavailable", True),
        (425, "tool.web.provider_unavailable", True),
        (500, "tool.web.provider_unavailable", True),
        (503, "tool.web.provider_unavailable", True),
    ],
)
async def test_web_provider_status_taxonomy_is_stable(
    provider_name: str,
    factory: ProviderFactory,
    status: int,
    reason_code: str,
    retryable: bool,
) -> None:
    del provider_name

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, text="upstream-private-diagnostic")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == reason_code
    assert raised.value.retryable is retryable
    assert "upstream-private-diagnostic" not in str(raised.value)


@pytest.mark.parametrize("status", [432, 433])
async def test_tavily_custom_usage_limit_statuses_are_quota_failures(status: int) -> None:
    credentials = MappingCredentialResolver({"tavily": "synthetic-tavily-credential"})

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, text="upstream-private-diagnostic")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        provider = TavilyWebProvider(credentials=credentials, client=client)
        with pytest.raises(WebProviderError) as raised:
            await provider.search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.quota_exceeded"
    assert raised.value.retryable is False
    assert "upstream-private-diagnostic" not in str(raised.value)


async def test_non_tavily_custom_client_status_remains_a_provider_rejection() -> None:
    credentials = MappingCredentialResolver({"firecrawl": "synthetic-firecrawl-credential"})

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(432, text="upstream-private-diagnostic")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        provider = FirecrawlWebProvider(credentials=credentials, client=client)
        with pytest.raises(WebProviderError) as raised:
            await provider.search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.provider_rejected"
    assert raised.value.retryable is False
    assert "upstream-private-diagnostic" not in str(raised.value)


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
@pytest.mark.parametrize("body", [b"not-json", b"[1,2]"])
async def test_web_provider_rejects_undecodable_or_non_object_bodies(
    provider_name: str,
    factory: ProviderFactory,
    body: bytes,
) -> None:
    del provider_name

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.output_invalid"
    assert raised.value.retryable is False


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
async def test_web_provider_defaults_and_truncates_result_fields(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    row = {
        "url": "https://example.org/ada",
        "content": "s" * 9_000,
        "description": "s" * 9_000,
        "title": "t" * 2_000,
    }
    untitled = {"url": "https://example.org/untitled", "content": "x", "description": "x"}

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        payload: dict[str, object] = (
            {"results": [row, untitled]}
            if provider_name == "tavily"
            else {"success": True, "data": {"web": [row, untitled]}}
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        results = await factory(client).search(WebSearchRequest(query="Ada Lovelace"))

    assert len(results) == 2
    assert results[0].title == "t" * 1_024
    assert results[0].snippet == "s" * 8_192
    assert results[1].title == "Untitled result"


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
async def test_web_provider_bounds_fetched_page_content(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    long_content = "c" * 600_000

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        payload: dict[str, object] = (
            {"results": [{"url": "https://example.org/ada", "raw_content": long_content}]}
            if provider_name == "tavily"
            else {
                "success": True,
                "data": {
                    "markdown": long_content,
                    "metadata": {"title": "t" * 2_000, "sourceURL": "https://example.org/ada"},
                },
            }
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        page = await factory(client).fetch("https://example.org/ada")

    assert len(page.content) == 524_288
    if provider_name == "firecrawl":
        assert page.title == "t" * 1_024


@pytest.mark.parametrize("provider_name", ["tavily", "firecrawl"])
async def test_web_provider_close_only_closes_a_client_it_owns(provider_name: str) -> None:
    credentials = MappingCredentialResolver({})
    provider_type = TavilyWebProvider if provider_name == "tavily" else FirecrawlWebProvider

    owned = provider_type(credentials=credentials)
    await owned.close()
    assert owned._client.is_closed

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as shared:
        provider = provider_type(credentials=credentials, client=shared)
        await provider.close()
        assert shared.is_closed is False


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
