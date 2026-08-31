"""Shared contract for deterministic web-provider routers."""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.adapters.web.routing import WeightedWebProviderRouter
from agent_core.domain.web import WebPage, WebSearchRequest, WebSearchResult
from agent_core.ports.web import WebProviderRouter


@dataclass
class _FakeWebProvider:
    name: str

    async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]:
        del request
        return ()

    async def fetch(self, url: str) -> WebPage:
        return WebPage(url=url, content="content")

    async def close(self) -> None:
        return


def test_weighted_router_applies_an_exact_deterministic_fifty_fifty_bucket_split() -> None:
    first = _FakeWebProvider(name="incumbent")
    second = _FakeWebProvider(name="keenable")
    router: WebProviderRouter = WeightedWebProviderRouter(
        ((first, 50), (second, 50)),
        bucket_for_key=lambda key: int(key),
    )

    selected = [router.select(routing_key=str(bucket)).name for bucket in range(100)]

    assert selected == ["incumbent"] * 50 + ["keenable"] * 50
    assert router.select(routing_key="75") is router.select(routing_key="75")


def test_weighted_router_refuses_invalid_or_duplicate_allocations() -> None:
    provider = _FakeWebProvider(name="provider")

    for allocations in ((), ((provider, 0),), ((provider, 50), (provider, 50))):
        try:
            WeightedWebProviderRouter(allocations)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid allocation accepted: {allocations!r}")
