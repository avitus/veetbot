"""Provider-neutral web tool behavior."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.web import WebPage, WebSearchRequest, WebSearchResult
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_search import WebSearchTool
from tests.contract.support import tool_context


@dataclass
class FakeWebProvider:
    name: str = "fake-web"
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
            content="# Ada Lovelace\n\nA public biographical record.",
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
        "https://localhost/private",
        "https://127.0.0.1/private",
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
