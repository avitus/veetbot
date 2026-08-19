"""Shared behavioral contract for authenticated browser adapters."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_core.adapters.browser.playwright import PlaywrightBrowserProvider
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserElement,
    BrowserObservation,
)
from agent_core.domain.execution import EgressPolicy
from agent_core.ports.browser import BrowserProvider


@dataclass
class ContractBrowserProvider:
    """Contract fixture; production adapters join this suite in later slices."""

    name: str = "contract-browser"
    navigations: list[str] = field(default_factory=list)
    observations: int = 0
    closed: bool = False

    def allows(self, url: str) -> bool:
        return url == "https://example.org" or url.startswith("https://example.org/")

    def _page(self, url: str) -> BrowserObservation:
        return BrowserObservation(
            url=url,
            title="Contract page",
            revision="contract-revision-1",
            text="Rendered account content",
            elements=(BrowserElement(ref="opaque-1", role="button", name="Continue"),),
        )

    async def navigate(self, url: str) -> BrowserObservation:
        self.navigations.append(url)
        return self._page(url)

    async def observe(self) -> BrowserObservation:
        self.observations += 1
        return self._page("https://example.org/current")

    async def act(self, action: BrowserAction) -> BrowserObservation:
        return self._page(f"https://example.org/action/{action.kind.value}")

    async def close(self) -> None:
        self.closed = True


async def test_browser_provider_navigation_and_observation_contract() -> None:
    provider: BrowserProvider = ContractBrowserProvider()

    navigated = await provider.navigate("https://example.org/account")
    observed = await provider.observe()
    acted = await provider.act(
        BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision=observed.revision,
            ref=observed.elements[0].ref,
        )
    )

    assert provider.name == "contract-browser"
    assert navigated.url == "https://example.org/account"
    assert observed.url == "https://example.org/current"
    assert acted.url == "https://example.org/action/click"
    assert navigated.elements[0].ref == "opaque-1"
    assert navigated.elements[0].name == "Continue"
    assert provider.allows("https://example.org/account")
    assert not provider.allows("https://other.example/account")


async def test_browser_provider_close_contract() -> None:
    provider = ContractBrowserProvider()

    await provider.close()

    assert provider.closed


@dataclass
class AdapterRuntime:
    current_url: str = "https://example.org/"
    closed: bool = False

    async def start(self, proxy_url: str, allowed_origins: tuple[str, ...]) -> None:
        assert proxy_url.startswith("http://127.0.0.1:")
        assert allowed_origins == ("https://example.org",)

    async def navigate(self, url: str) -> BrowserObservation:
        self.current_url = url
        return await self.observe()

    async def observe(self) -> BrowserObservation:
        return BrowserObservation(
            url=self.current_url,
            title="Adapter contract page",
            revision="adapter-contract-revision",
            text="Rendered account content",
            elements=(BrowserElement(ref="opaque-2", role="button", name="Continue"),),
        )

    async def act(self, action: BrowserAction) -> BrowserObservation:
        self.current_url = f"https://example.org/action/{action.kind.value}"
        return await self.observe()

    async def close(self) -> None:
        self.closed = True


@dataclass
class AdapterProxy:
    url: str = "http://127.0.0.1:43123"
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


async def test_playwright_adapter_satisfies_browser_provider_contract() -> None:
    runtime = AdapterRuntime()
    proxy = AdapterProxy()

    async def start_proxy(policy: EgressPolicy, *, tenant_id: str) -> AdapterProxy:
        assert policy.destinations[0].host == "example.org"
        assert tenant_id == "tenant-contract"
        return proxy

    provider: BrowserProvider = PlaywrightBrowserProvider(
        tenant_id="tenant-contract",
        allowed_origins=("https://example.org",),
        runtime=runtime,
        proxy_factory=start_proxy,
    )

    navigated = await provider.navigate("https://example.org/account")
    observed = await provider.observe()
    acted = await provider.act(
        BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision=observed.revision,
            ref=observed.elements[0].ref,
        )
    )
    await provider.close()

    assert provider.name == "playwright"
    assert navigated.url == "https://example.org/account"
    assert observed.elements[0].ref == "opaque-2"
    assert acted.url == "https://example.org/action/click"
    assert provider.allows("https://example.org/account")
    assert not provider.allows("https://other.example/account")
    assert runtime.closed
    assert proxy.closed
