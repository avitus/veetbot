"""Playwright browser provider isolation and origin behavior."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_core.adapters.browser.playwright import (
    PlaywrightBrowserProvider,
    PythonPlaywrightRuntime,
)
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.execution import EgressMode, EgressPolicy


@dataclass
class FakeRuntime:
    starts: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    navigations: list[str] = field(default_factory=list)
    closed: bool = False
    fail: bool = False
    actions: list[BrowserAction] = field(default_factory=list)

    async def start(self, proxy_url: str, allowed_origins: tuple[str, ...]) -> None:
        self.starts.append((proxy_url, allowed_origins))

    async def navigate(self, url: str) -> BrowserObservation:
        if self.fail:
            raise RuntimeError("provider-private-diagnostic")
        self.navigations.append(url)
        return BrowserObservation(
            url=url,
            title="Example",
            revision="revision-1",
            text="Rendered page",
        )

    async def observe(self) -> BrowserObservation:
        return await self.navigate("https://example.org/current")

    async def act(self, action: BrowserAction) -> BrowserObservation:
        self.actions.append(action)
        return BrowserObservation(
            url="https://example.org/current",
            title="Example",
            revision="revision-2",
            text="Updated page",
        )

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeProxy:
    url: str = "http://127.0.0.1:43123"
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


async def test_playwright_provider_starts_with_scrubbed_egress_policy() -> None:
    runtime = FakeRuntime()
    proxy = FakeProxy()
    observed: list[tuple[EgressPolicy, str]] = []

    async def start_proxy(policy: EgressPolicy, *, tenant_id: str) -> FakeProxy:
        observed.append((policy, tenant_id))
        return proxy

    provider = PlaywrightBrowserProvider(
        tenant_id="tenant-a",
        allowed_origins=("https://example.org", "https://static.example.org"),
        runtime=runtime,
        proxy_factory=start_proxy,
    )

    page = await provider.navigate("https://example.org/account")

    assert page.url == "https://example.org/account"
    assert runtime.starts == [
        (
            proxy.url,
            ("https://example.org", "https://static.example.org"),
        )
    ]
    assert observed[0][1] == "tenant-a"
    assert observed[0][0].mode is EgressMode.ALLOWLIST
    assert {(item.host, item.ports) for item in observed[0][0].destinations} == {
        ("example.org", frozenset({443})),
        ("static.example.org", frozenset({443})),
    }


async def test_playwright_provider_rejects_out_of_policy_origin_before_start() -> None:
    runtime = FakeRuntime()
    proxy_called = False

    async def start_proxy(policy: EgressPolicy, *, tenant_id: str) -> FakeProxy:
        del policy, tenant_id
        nonlocal proxy_called
        proxy_called = True
        return FakeProxy()

    provider = PlaywrightBrowserProvider(
        tenant_id="tenant-a",
        allowed_origins=("https://example.org",),
        runtime=runtime,
        proxy_factory=start_proxy,
    )

    with pytest.raises(BrowserProviderError) as raised:
        await provider.navigate("https://other.example/account")

    assert raised.value.reason_code == "tool.browser.url_disallowed"
    assert raised.value.retryable is False
    assert runtime.starts == []
    assert not proxy_called


async def test_playwright_provider_normalizes_runtime_failure_and_closes_resources() -> None:
    runtime = FakeRuntime(fail=True)
    proxy = FakeProxy()

    async def start_proxy(policy: EgressPolicy, *, tenant_id: str) -> FakeProxy:
        del policy, tenant_id
        return proxy

    provider = PlaywrightBrowserProvider(
        tenant_id="tenant-a",
        allowed_origins=("https://example.org",),
        runtime=runtime,
        proxy_factory=start_proxy,
    )

    with pytest.raises(BrowserProviderError) as raised:
        await provider.navigate("https://example.org/account")
    await provider.close()

    assert "provider-private-diagnostic" not in str(raised.value)
    assert raised.value.reason_code == "tool.browser.provider_unavailable"
    assert raised.value.retryable is True
    assert runtime.closed
    assert proxy.closed


async def test_playwright_provider_cleans_up_when_runtime_start_fails() -> None:
    runtime = FakeRuntime()
    proxy = FakeProxy()

    async def fail_start(proxy_url: str, allowed_origins: tuple[str, ...]) -> None:
        del proxy_url, allowed_origins
        raise RuntimeError("private startup diagnostic")

    runtime.start = fail_start  # type: ignore[method-assign]

    async def start_proxy(policy: EgressPolicy, *, tenant_id: str) -> FakeProxy:
        del policy, tenant_id
        return proxy

    provider = PlaywrightBrowserProvider(
        tenant_id="tenant-a",
        allowed_origins=("https://example.org",),
        runtime=runtime,
        proxy_factory=start_proxy,
    )

    with pytest.raises(BrowserProviderError) as raised:
        await provider.navigate("https://example.org/account")

    assert raised.value.reason_code == "tool.browser.provider_unavailable"
    assert runtime.closed
    assert proxy.closed


async def test_playwright_provider_dispatches_revision_bound_action() -> None:
    runtime = FakeRuntime()
    proxy = FakeProxy()

    async def start_proxy(policy: EgressPolicy, *, tenant_id: str) -> FakeProxy:
        del policy, tenant_id
        return proxy

    provider = PlaywrightBrowserProvider(
        tenant_id="tenant-a",
        allowed_origins=("https://example.org",),
        runtime=runtime,
        proxy_factory=start_proxy,
    )
    action = BrowserAction(
        kind=BrowserActionKind.CLICK,
        expected_revision="revision-1",
        ref="revision-1:0",
    )

    observation = await provider.act(action)

    assert runtime.actions == [action]
    assert observation.revision == "revision-2"


async def test_playwright_runtime_close_resets_state_when_home_cleanup_fails() -> None:
    class FailingTemporaryHome:
        def cleanup(self) -> None:
            raise OSError("synthetic cleanup failure")

    runtime = PythonPlaywrightRuntime()
    runtime._temporary_home = FailingTemporaryHome()  # type: ignore[assignment]
    runtime._revision = "stale-revision"
    runtime._elements = {"stale": object()}  # type: ignore[dict-item]

    await runtime.close()

    assert runtime._temporary_home is None
    assert runtime._browser is None
    assert runtime._context is None
    assert runtime._page is None
    assert runtime._revision is None
    assert runtime._elements == {}
