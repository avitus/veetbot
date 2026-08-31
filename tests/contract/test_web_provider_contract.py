"""Shared behavioral contract for every fixed-endpoint public-web adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.adapters.web.common import MAXIMUM_RESPONSE_BYTES
from agent_core.adapters.web.firecrawl import FirecrawlWebProvider
from agent_core.adapters.web.keenable import KeenableWebProvider
from agent_core.adapters.web.tavily import TavilyWebProvider
from agent_core.domain.web import WebProviderError, WebRecency, WebSearchRequest
from agent_core.ports.web import WebProvider

ProviderFactory = Callable[[httpx.AsyncClient], WebProvider]


def provider_factories() -> tuple[tuple[str, ProviderFactory], ...]:
    credentials = MappingCredentialResolver(
        {
            "tavily": "synthetic-tavily-credential",
            "firecrawl": "synthetic-firecrawl-credential",
            "keenable": "synthetic-keenable-credential",
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
        (
            "keenable",
            lambda client: KeenableWebProvider(credentials=credentials, client=client),
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
        elif provider_name == "firecrawl":
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
        else:
            payload = {
                "query": "Ada Lovelace",
                "results": [
                    {
                        "title": "Ada Lovelace",
                        "url": "https://example.org/ada",
                        "description": "A public biographical record.",
                        "snippet": "A public biographical record.",
                    }
                ],
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
    if provider_name == "keenable":
        assert request.headers["x-api-key"] == "synthetic-keenable-credential"
        assert "authorization" not in request.headers
    else:
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
    elif provider_name == "firecrawl":
        assert request.url == "https://api.firecrawl.dev/v2/search"
        assert body == {
            "query": "Ada Lovelace",
            "limit": 3,
            "sources": ["web"],
            "includeDomains": ["example.org"],
            "tbs": "qdr:m",
            "ignoreInvalidURLs": True,
        }
    else:
        assert request.url == "https://api.keenable.ai/v1/search"
        assert body == {
            "query": "Ada Lovelace",
            "site": "example.org",
            "published_after": "1mo",
            "snippet_max_length": 8192,
            "max_results": 3,
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
        elif provider_name == "firecrawl":
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
        else:
            payload = {
                "url": "https://example.org/ada",
                "title": "Ada Lovelace",
                "content": "# Ada Lovelace\n\nA public biographical record.",
            }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        page = await factory(client).fetch("https://example.org/ada")

    assert page.url == "https://example.org/ada"
    assert page.content == "# Ada Lovelace\n\nA public biographical record."
    assert page.title == (None if provider_name == "tavily" else "Ada Lovelace")
    assert len(observed) == 1
    request = observed[0]
    if provider_name == "tavily":
        body = json.loads(request.content)
        assert body == {
            "urls": ["https://example.org/ada"],
            "extract_depth": "basic",
            "include_images": False,
            "format": "markdown",
        }
    elif provider_name == "firecrawl":
        body = json.loads(request.content)
        assert body == {
            "url": "https://example.org/ada",
            "formats": ["markdown"],
            "onlyMainContent": True,
            "skipTlsVerification": False,
        }
    else:
        assert request.method == "GET"
        assert request.url.copy_with(query=None) == "https://api.keenable.ai/v1/fetch"
        assert dict(request.url.params) == {
            "url": "https://example.org/ada",
            "max_chars": "524288",
            "live": "true",
        }
        assert request.headers["x-api-key"] == "synthetic-keenable-credential"


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
            {"success": True, "data": {"web": "not-a-list"}}
            if provider_name == "firecrawl"
            else {"results": "not-a-list"}
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
        {"success": True, "data": {"web": []}} if provider_name == "firecrawl" else {"results": []}
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
    elif provider_name == "firecrawl":
        assert all(body["excludeDomains"] == ["tracker.example"] for body in recency_values)
        assert all("includeDomains" not in body for body in recency_values)
        assert [body["tbs"] for body in recency_values] == ["qdr:d", "qdr:w", "qdr:m", "qdr:y"]
    else:
        assert all("site" not in body for body in recency_values)
        assert all(body["max_results"] == 50 for body in recency_values)
        assert [body["published_after"] for body in recency_values] == [
            "1d",
            "7d",
            "1mo",
            "1y",
        ]


@pytest.mark.parametrize(("provider_name", "factory"), provider_factories())
async def test_web_provider_rejects_unsuccessful_success_shaped_bodies(
    provider_name: str,
    factory: ProviderFactory,
) -> None:
    """A 200 body that reports failure or the wrong arity is a permanent rejection."""

    bodies: list[dict[str, object]] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        """Return the latest success-shaped body selected by the test."""

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
        elif provider_name == "firecrawl":
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
    """Provider rows still pass through the public-web domain boundary."""

    row = {"title": "t", "url": "https://127.0.0.1/private", "content": "x", "description": "x"}

    async def wire(request: httpx.Request) -> httpx.Response:
        """Return one provider-shaped row with a disallowed result URL."""

        del request
        payload: dict[str, object] = (
            {"success": True, "data": {"web": [row]}}
            if provider_name == "firecrawl"
            else {"results": [row]}
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
    """HTTP failures retain provider-neutral reasons and retryability."""

    del provider_name

    async def wire(request: httpx.Request) -> httpx.Response:
        """Return the status selected by the taxonomy case."""

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
    """Tavily's nonstandard usage-limit statuses retain quota semantics."""

    credentials = MappingCredentialResolver({"tavily": "synthetic-tavily-credential"})

    async def wire(request: httpx.Request) -> httpx.Response:
        """Return Tavily's selected private usage-limit status."""

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
    """Do not assign Tavily's private status meanings to another provider."""

    credentials = MappingCredentialResolver({"firecrawl": "synthetic-firecrawl-credential"})

    async def wire(request: httpx.Request) -> httpx.Response:
        """Return a status with no Firecrawl-specific quota meaning."""

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
            {"success": True, "data": {"web": [row, untitled]}}
            if provider_name == "firecrawl"
            else {"results": [row, untitled]}
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
        if provider_name == "tavily":
            payload: dict[str, object] = {
                "results": [{"url": "https://example.org/ada", "raw_content": long_content}]
            }
        elif provider_name == "firecrawl":
            payload = {
                "success": True,
                "data": {
                    "markdown": long_content,
                    "metadata": {"title": "t" * 2_000, "sourceURL": "https://example.org/ada"},
                },
            }
        else:
            payload = {
                "url": "https://example.org/ada",
                "title": "t" * 2_000,
                "content": long_content,
            }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        page = await factory(client).fetch("https://example.org/ada")

    assert len(page.content) == 524_288
    if provider_name in {"firecrawl", "keenable"}:
        assert page.title == "t" * 1_024


@pytest.mark.parametrize("provider_name", ["tavily", "firecrawl", "keenable"])
async def test_web_provider_close_only_closes_a_client_it_owns(provider_name: str) -> None:
    credentials = MappingCredentialResolver({})
    if provider_name == "tavily":
        provider_type: type[Any] = TavilyWebProvider
    elif provider_name == "firecrawl":
        provider_type = FirecrawlWebProvider
    else:
        provider_type = KeenableWebProvider

    owned = provider_type(credentials=credentials)
    await owned.close()
    assert owned._client.is_closed

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as shared:
        provider = provider_type(credentials=credentials, client=shared)
        await provider.close()
        assert shared.is_closed is False


@pytest.mark.parametrize("provider_name", ["tavily", "firecrawl", "keenable"])
async def test_web_provider_missing_credential_is_a_stable_auth_failure(
    provider_name: str,
) -> None:
    credentials = MappingCredentialResolver({})

    async def reject_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network called without credential: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject_network)) as client:
        if provider_name == "tavily":
            provider: WebProvider = TavilyWebProvider(credentials=credentials, client=client)
        elif provider_name == "firecrawl":
            provider = FirecrawlWebProvider(credentials=credentials, client=client)
        else:
            provider = KeenableWebProvider(credentials=credentials, client=client)
        with pytest.raises(WebProviderError) as raised:
            await provider.search(WebSearchRequest(query="Ada Lovelace"))

    assert raised.value.reason_code == "tool.web.auth_failed"
    assert raised.value.retryable is False


async def test_keenable_preserves_multi_include_and_exclude_domain_semantics() -> None:
    """The narrower upstream site filter must not narrow the platform contract."""

    observed: list[dict[str, object]] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        observed.append(body)
        site = body.get("site")
        if site is not None:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": str(site),
                            "url": f"https://{site}/result",
                            "snippet": "included",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Allowed",
                        "url": "https://allowed.example/result",
                        "snippet": "allowed",
                    },
                    {
                        "title": "Excluded",
                        "url": "https://news.tracker.example/result",
                        "snippet": "excluded",
                    },
                ]
            },
        )

    factory = dict(provider_factories())["keenable"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        provider = factory(client)
        included = await provider.search(
            WebSearchRequest(
                query="Ada",
                max_results=2,
                include_domains=("first.example", "second.example"),
            )
        )
        excluded = await provider.search(
            WebSearchRequest(
                query="Ada",
                max_results=2,
                exclude_domains=("tracker.example",),
            )
        )

    assert [result.url for result in included] == [
        "https://first.example/result",
        "https://second.example/result",
    ]
    assert [result.url for result in excluded] == ["https://allowed.example/result"]
    assert [body.get("site") for body in observed[:2]] == [
        "first.example",
        "second.example",
    ]
    assert observed[2]["max_results"] == 50


async def test_keenable_malformed_key_status_is_a_stable_auth_failure() -> None:
    """Keenable documents HTTP 400 specifically for malformed API keys."""

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, text="malformed key diagnostic")

    factory = dict(provider_factories())["keenable"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).search(WebSearchRequest(query="Ada"))

    assert raised.value.reason_code == "tool.web.auth_failed"
    assert raised.value.retryable is False
    assert "malformed key diagnostic" not in str(raised.value)


async def test_keenable_reads_a_full_length_multi_byte_page() -> None:
    """The requested character budget must fit inside the response byte reader."""

    content = "\U0001f600" * 524_288

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        body = json.dumps(
            {"url": "https://example.org/ada", "title": "Ada", "content": content},
            ensure_ascii=False,
        ).encode("utf-8")
        assert len(body) > MAXIMUM_RESPONSE_BYTES
        return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})

    factory = dict(provider_factories())["keenable"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        page = await factory(client).fetch("https://example.org/ada")

    assert page.content == content


async def test_keenable_fetch_still_refuses_a_response_beyond_its_own_budget() -> None:
    """Widening the reader for requested characters must not unbound it."""

    async def wire(request: httpx.Request) -> httpx.Response:
        del request
        padding = b"a" * (4 * 524_288 + 64 * 1024 + 1)
        return httpx.Response(200, content=b'{"padding":"' + padding + b'"}')

    factory = dict(provider_factories())["keenable"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(WebProviderError) as raised:
            await factory(client).fetch("https://example.org/ada")

    assert raised.value.reason_code == "tool.web.output_invalid"
    assert raised.value.retryable is False
