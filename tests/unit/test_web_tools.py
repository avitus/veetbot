"""Provider-neutral web tool behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import ToolFailureKind
from agent_core.domain.web import WebPage, WebProviderError, WebSearchRequest, WebSearchResult
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_search import WebSearchTool
from tests.contract.support import tool_context


@dataclass
class FakeWebProvider:
    name: str = "fake-web"
    page_content: str = "# Ada Lovelace\n\nA public biographical record."
    searches: list[WebSearchRequest] = field(default_factory=list)
    fetches: list[str] = field(default_factory=list)

    async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]:
        self.searches.append(request)
        return (
            WebSearchResult(
                title="Ada Lovelace",
                url="https://example.org/ada",
                snippet="A public biographical record.",
            ),
        )

    async def fetch(self, url: str) -> WebPage:
        self.fetches.append(url)
        return WebPage(
            url=url,
            title="Ada Lovelace",
            content=self.page_content,
        )

    async def close(self) -> None:
        return


@pytest.mark.parametrize("tool_type", [WebSearchTool, WebFetchTool])
def test_web_tools_are_bounded_read_only_external_capabilities(tool_type: type[object]) -> None:
    tool = tool_type(FakeWebProvider())  # type: ignore[call-arg]
    assert tool.spec.side_effect is SideEffectClass.NETWORK_READ  # type: ignore[attr-defined]
    assert tool.spec.risk is RiskLevel.LOW  # type: ignore[attr-defined]
    assert tool.spec.idempotency is IdempotencyClass.READ_ONLY  # type: ignore[attr-defined]
    assert tool.spec.target_kind == "web_provider"  # type: ignore[attr-defined]
    assert tool.spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED  # type: ignore[attr-defined]
    assert tool.spec.maximum_output_bytes <= 1_048_576  # type: ignore[attr-defined]


async def test_search_returns_the_provider_neutral_shape_as_untrusted_content() -> None:
    provider = FakeWebProvider()
    result = await WebSearchTool(provider).execute(
        {
            "query": "Ada Lovelace",
            "max_results": 5,
            "include_domains": ["example.org"],
            "recency": "month",
        },
        tool_context(),
    )

    assert result.ok
    assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert result.structured == {
        "provider": "fake-web",
        "results": [
            {
                "title": "Ada Lovelace",
                "url": "https://example.org/ada",
                "snippet": "A public biographical record.",
            }
        ],
    }
    assert provider.searches[0].query == "Ada Lovelace"


async def test_fetch_rejects_non_public_or_non_https_destinations_before_provider_call() -> None:
    provider = FakeWebProvider()
    tool = WebFetchTool(provider)

    for url in (
        "http://example.org/",
        "https://example.org:8443/private",
        "https://localhost/private",
        "https://127.0.0.1/private",
        "https://127.1/private",
        "https://10.1/private",
        "https://192.168.1/private",
        "https://169.254.169.254/latest/meta-data/",
        "https://service.internal/private",
        "https://user:" + "password@example.org/private",
        "https://" + ".".join(["é" * 30] * 7) + "/encoded-hostname-too-long",
    ):
        result = await tool.execute({"url": url}, tool_context())
        assert not result.ok
        assert result.failure is not None
        assert result.failure.reason_code == "tool.web.url_disallowed"

    assert provider.fetches == []


async def test_fetch_returns_page_content_as_external_untrusted() -> None:
    provider = FakeWebProvider()
    result = await WebFetchTool(provider).execute(
        {"url": "https://example.org/ada"},
        tool_context(),
    )

    assert result.ok
    assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert result.structured == {
        "provider": "fake-web",
        "url": "https://example.org/ada",
        "title": "Ada Lovelace",
        "content": "# Ada Lovelace\n\nA public biographical record.",
    }
    assert provider.fetches == ["https://example.org/ada"]


@dataclass
class FailingWebProvider(FakeWebProvider):
    reason_code: str = "tool.web.provider_unavailable"
    retryable: bool = True

    async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]:
        raise WebProviderError(self.reason_code, retryable=self.retryable)

    async def fetch(self, url: str) -> WebPage:
        raise WebProviderError(self.reason_code, retryable=self.retryable)


@pytest.mark.parametrize(
    ("reason_code", "retryable", "kind"),
    [
        ("tool.web.auth_failed", False, ToolFailureKind.PERMISSION),
        ("tool.web.output_invalid", False, ToolFailureKind.OUTPUT_INVALID),
        ("tool.web.provider_unavailable", True, ToolFailureKind.TRANSPORT),
        ("tool.web.provider_rejected", False, ToolFailureKind.UPSTREAM_ERROR),
    ],
)
async def test_provider_failures_keep_their_stable_kind_and_retryability(
    reason_code: str,
    retryable: bool,
    kind: ToolFailureKind,
) -> None:
    provider = FailingWebProvider(reason_code=reason_code, retryable=retryable)

    search_result = await WebSearchTool(provider).execute({"query": "Ada"}, tool_context())
    fetch_result = await WebFetchTool(provider).execute(
        {"url": "https://example.org/ada"}, tool_context()
    )

    for result in (search_result, fetch_result):
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind is kind
        assert result.failure.reason_code == reason_code
        assert result.failure.retryable is retryable
        assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": "   "},
        {"query": "x" * 501},
        {"query": "Ada", "max_results": 0},
        {"query": "Ada", "max_results": 11},
        {"query": "Ada", "include_domains": ["example.org"], "exclude_domains": ["example.net"]},
        {"query": "Ada", "include_domains": ["example.org", "example.org"]},
        {"query": "Ada", "include_domains": ["127.0.0.1"]},
        {"query": "Ada", "recency": "fortnight"},
    ],
)
async def test_search_rejects_invalid_arguments_before_any_provider_call(
    arguments: dict[str, object],
) -> None:
    provider = FakeWebProvider()

    result = await WebSearchTool(provider).execute(dict(arguments), tool_context())

    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert result.failure.reason_code == "tool.arguments_invalid"
    assert result.failure.retryable is False
    assert provider.searches == []


async def test_fetch_requires_a_string_url_before_any_provider_call() -> None:
    provider = FakeWebProvider()
    tool = WebFetchTool(provider)

    for arguments in ({}, {"url": 7}, {"url": None}):
        result = await tool.execute(dict(arguments), tool_context())
        assert not result.ok
        assert result.failure is not None
        assert result.failure.reason_code == "tool.web.url_disallowed"

    assert provider.fetches == []


async def test_search_bounds_maximal_results_within_the_declared_output_cap() -> None:
    maximal = WebSearchResult(
        title="😀" * 1024,
        url="https://example.org/" + "a" * 4070,
        snippet="😀" * 8192,
    )

    @dataclass
    class MaximalProvider(FakeWebProvider):
        async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]:
            return (maximal,) * request.max_results

    result = await WebSearchTool(MaximalProvider()).execute(
        {"query": "everything", "max_results": 10},
        tool_context(),
    )

    assert result.ok
    rendered = json.dumps(
        [part.model_dump(mode="json") for part in result.content],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(rendered) <= WebSearchTool.spec.maximum_output_bytes
    assert isinstance(result.structured, dict)
    returned = result.structured["results"]
    assert isinstance(returned, list)
    assert returned
    assert returned[0] == maximal.model_dump(mode="json")
    assert result.content == [
        TextPart(
            text=json.dumps(
                result.structured,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    ]


async def test_search_limits_nonconforming_provider_results_to_the_requested_count() -> None:
    @dataclass
    class OverReturningProvider(FakeWebProvider):
        async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]:
            return tuple(
                WebSearchResult(
                    title=f"Result {index}",
                    url=f"https://example.org/{index}",
                    snippet="A short result.",
                )
                for index in range(request.max_results + 3)
            )

    result = await WebSearchTool(OverReturningProvider()).execute(
        {"query": "everything", "max_results": 2},
        tool_context(),
    )

    assert result.ok
    assert isinstance(result.structured, dict)
    assert len(result.structured["results"]) == 2


async def test_fetch_bounds_multibyte_content_before_building_both_output_shapes() -> None:
    provider = FakeWebProvider(page_content="😀" * 300_000)

    result = await WebFetchTool(provider).execute(
        {"url": "https://example.org/large"},
        tool_context(),
    )

    assert result.ok
    assert isinstance(result.structured, dict)
    structured_content = result.structured["content"]
    assert isinstance(structured_content, str)
    assert result.content == [TextPart(text=structured_content)]
    assert len(structured_content.encode("utf-8")) <= WebFetchTool.spec.maximum_output_bytes
    assert structured_content.encode("utf-8").decode("utf-8") == structured_content
