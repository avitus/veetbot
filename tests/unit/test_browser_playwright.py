"""Playwright browser provider isolation and origin behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import ElementHandle, Locator, Page
from playwright.async_api import Error as PlaywrightError

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


async def test_observation_pipelines_slow_controls_with_bounded_concurrency() -> None:
    """Independent browser replies must not serialize a large login page's startup."""
    active = 0
    peak = 0
    overlapping = asyncio.Event()

    async def visible() -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active >= 2:
            overlapping.set()
        await overlapping.wait()
        return True

    async def disabled() -> bool:
        nonlocal active
        await asyncio.sleep(0)
        active -= 1
        return False

    handles = []
    for index in range(32):
        handle = AsyncMock(spec=ElementHandle)
        handle.is_visible.side_effect = visible
        handle.is_disabled.side_effect = disabled
        handle.evaluate.return_value = {
            "tag": "button",
            "role": None,
            "inputType": None,
            "name": f"Control {index}",
        }
        handles.append(handle)
    controls = Mock(spec=Locator)
    controls.element_handles = AsyncMock(return_value=handles)
    body = Mock(spec=Locator)
    body.inner_text = AsyncMock(return_value="Login page")
    page = Mock(spec=Page)
    page.url = "https://site.example/login"
    page.title = AsyncMock(return_value="Sign in")
    page.locator.side_effect = lambda selector: body if selector == "body" else controls

    observation = await asyncio.wait_for(PythonPlaywrightRuntime()._observation(page), 1)

    assert 2 <= peak <= 8
    assert active == 0
    assert [element.name for element in observation.elements] == [
        f"Control {index}" for index in range(32)
    ]


async def test_observation_keeps_the_element_snapshot_when_the_page_replaces_controls() -> None:
    """A changing login form must not wait for a vanished indexed selector."""
    handle = AsyncMock(spec=ElementHandle)
    handle.is_visible.return_value = True
    handle.get_attribute.return_value = None
    handle.evaluate.side_effect = lambda expression: (
        {"tag": "button", "role": None, "inputType": None, "name": "Log in"}
        if "getAttribute" in expression
        else "button"
    )
    handle.inner_text.return_value = "Log in"
    handle.is_disabled.return_value = False

    changing_control = Mock(spec=Locator)
    changing_control.is_visible = AsyncMock(return_value=True)
    changing_control.element_handle = AsyncMock(return_value=handle)
    changing_control.get_attribute = AsyncMock(return_value=None)
    changing_control.evaluate = AsyncMock(
        side_effect=PlaywrightError("control disappeared while resolving its indexed selector")
    )
    controls = Mock(spec=Locator)
    controls.count = AsyncMock(return_value=1)
    controls.nth.return_value = changing_control
    controls.element_handles = AsyncMock(return_value=[handle])
    body = Mock(spec=Locator)
    body.inner_text = AsyncMock(return_value="Welcome")
    page = Mock(spec=Page)
    page.url = "https://site.example/login"
    page.title = AsyncMock(return_value="Sign in")
    page.locator.side_effect = lambda selector: body if selector == "body" else controls
    runtime = PythonPlaywrightRuntime()

    observation = await runtime._observation(page)

    assert observation.text == "Welcome"
    assert len(observation.elements) == 1
    assert observation.elements[0].name == "Log in"
    assert observation.elements[0].role == "button"
    assert runtime._elements[observation.elements[0].ref] is handle


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


@dataclass
class FakeNavigationRequest:
    url: str

    @property
    def frame(self) -> SimpleNamespace:
        """Expose the main-frame identity supplied with a navigation request."""
        return SimpleNamespace(parent_frame=None)

    def is_navigation_request(self) -> bool:
        """Identify the fake request as a top-level navigation hop."""
        return True


class FakeRedirectingPage:
    """Report the navigation requests Chromium emits, then fail like a refused tunnel."""

    def __init__(self, hops: list[str]) -> None:
        """Initialize the fake navigation event state for this regression."""
        self.hops = hops
        self.handlers: dict[str, list[Callable[[Any], None]]] = {}

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        """Capture browser event callbacks for deterministic navigation simulation."""
        self.handlers.setdefault(event, []).append(handler)

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        """Simulate redirect events and the configured navigation outcome."""
        del wait_until, timeout
        for hop in (url, *self.hops):
            for handler in self.handlers.get("request", ()):
                handler(FakeNavigationRequest(hop))
        raise PlaywrightError(f"Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at {url}")


@pytest.mark.parametrize(
    ("hops", "expected_reason", "expected_retryable"),
    [
        (["https://www.duolingo.com/"], "tool.browser.url_disallowed", False),
        ([], "tool.browser.provider_unavailable", True),
    ],
)
async def test_playwright_runtime_classifies_failed_navigation_by_origin_policy(
    hops: list[str],
    expected_reason: str,
    expected_retryable: bool,
) -> None:
    """A redirect the egress policy refuses is a policy outcome, not a crash."""

    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://duolingo.com",)
    page = FakeRedirectingPage(hops)
    runtime._attach_page(page)  # type: ignore[arg-type]

    with pytest.raises(BrowserProviderError) as raised:
        await runtime.navigate("https://duolingo.com/")

    assert raised.value.reason_code == expected_reason
    assert raised.value.retryable is expected_retryable
    assert "ERR_TUNNEL" not in str(raised.value)
    assert "duolingo" not in str(raised.value)
