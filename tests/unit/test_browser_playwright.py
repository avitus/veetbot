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
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from agent_core.adapters.browser import playwright as playwright_adapter
from agent_core.adapters.browser.playwright import (
    PlaywrightBrowserProvider,
    PythonPlaywrightRuntime,
)
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationStatus,
    BrowserFieldKind,
    BrowserInteractiveEvent,
    BrowserLabelSource,
    BrowserObservation,
    BrowserPageEvidence,
    BrowserProviderError,
    BrowserTargetFacts,
)
from agent_core.domain.execution import EgressMode, EgressPolicy
from tests.real_browser_support import RealBrowserRuntime, local_https_site, require_real_browser


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
    page.evaluate = AsyncMock(return_value=[True] * len(handles))

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
    page.evaluate = AsyncMock(return_value=[True])
    runtime = PythonPlaywrightRuntime()

    observation = await runtime._observation(page)

    assert observation.text == "Welcome"
    assert len(observation.elements) == 1
    assert observation.elements[0].name == "Log in"
    assert observation.elements[0].role == "button"
    assert runtime._elements[observation.elements[0].ref] is handle


def observed_page(handles: list[AsyncMock], flags: list[bool]) -> Mock:
    """A page whose controls are ``handles`` and whose visibility pass returns ``flags``."""

    controls = Mock(spec=Locator)
    controls.element_handles = AsyncMock(return_value=handles)
    body = Mock(spec=Locator)
    body.inner_text = AsyncMock(return_value="Lesson")
    page = Mock(spec=Page)
    page.url = "https://site.example/lesson"
    page.title = AsyncMock(return_value="Lesson")
    page.locator.side_effect = lambda selector: body if selector == "body" else controls
    page.evaluate = AsyncMock(return_value=flags)
    return page


def control(name: str, *, visible: bool = True) -> AsyncMock:
    handle = AsyncMock(spec=ElementHandle)
    handle.is_visible.return_value = visible
    handle.is_disabled.return_value = False
    handle.evaluate.return_value = {"tag": "button", "role": None, "inputType": None, "name": name}
    return handle


async def test_hidden_controls_never_take_an_element_slot() -> None:
    """ADR-0130 decision 8: visibility is decided before the 256-element cap."""

    hidden = [control(f"Hidden {index}", visible=False) for index in range(300)]
    visible = [control(f"Answer {index}") for index in range(5)]
    page = observed_page(hidden + visible, [False] * 300 + [True] * 5)

    observation = await PythonPlaywrightRuntime()._observation(page)

    assert [element.name for element in observation.elements] == [
        f"Answer {index}" for index in range(5)
    ]
    for handle in hidden:
        handle.is_visible.assert_not_awaited()


async def test_observation_disposes_unkept_and_replaced_handles() -> None:
    """Handles the runtime no longer references are released in the page (decision 8)."""

    first_hidden = control("Hidden", visible=False)
    first_kept = control("Check")
    runtime = PythonPlaywrightRuntime()
    await runtime._observation(observed_page([first_hidden, first_kept], [False, True]))
    first_hidden.dispose.assert_awaited_once()
    first_kept.dispose.assert_not_awaited()

    second_kept = control("Continue")
    await runtime._observation(observed_page([second_kept], [True]))

    first_kept.dispose.assert_awaited_once()
    second_kept.dispose.assert_not_awaited()
    assert list(runtime._elements.values()) == [second_kept]


async def test_disposal_failures_never_fail_an_observation() -> None:
    stale = control("Hidden", visible=False)
    stale.dispose.side_effect = PlaywrightError("Target page, context or browser has been closed")

    observation = await PythonPlaywrightRuntime()._observation(
        observed_page([stale, control("Check")], [False, True])
    )

    assert [element.name for element in observation.elements] == ["Check"]


async def test_observation_scans_at_most_4096_candidates() -> None:
    handles = [control(f"Hidden {index}", visible=False) for index in range(4100)]
    page = observed_page(handles, [False] * 4096)

    await PythonPlaywrightRuntime()._observation(page)

    page.evaluate.assert_awaited_once()
    scanned = page.evaluate.await_args.args[1]
    assert len(scanned) == 4096


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
    runtime._sign_in_entered = True

    await runtime.close()

    assert runtime._temporary_home is None
    assert runtime._browser is None
    assert runtime._context is None
    assert runtime._page is None
    assert runtime._revision is None
    assert runtime._elements == {}
    assert runtime._sign_in_entered is False


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


class FakeVirtualDisplay:
    """Record the lifecycle of the private display an interactive ceremony owns."""

    def __init__(self, *, failure: BaseException | None = None) -> None:
        """Initialize the display with an optional synthetic start failure."""
        self.failure = failure
        self.started = False
        self.closed = False

    async def start(self) -> str:
        """Report a display name the way a started virtual display does."""
        if self.failure is not None:
            raise self.failure
        self.started = True
        return ":77"

    async def close(self) -> None:
        """Record that the owning runtime destroyed the display."""
        self.closed = True


@dataclass
class FakeChromiumLaunches:
    """Capture how the runtime launches Chromium without starting a browser."""

    launches: list[dict[str, Any]] = field(default_factory=list)
    contexts: list[dict[str, Any]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the Playwright entry point with this recording fake."""
        session = Mock()
        session.send = AsyncMock(return_value={"frameTree": {"frame": {"id": "main"}}})
        context = Mock()
        context.route = AsyncMock()
        context.new_page = AsyncMock(return_value=Mock())
        context.new_cdp_session = AsyncMock(return_value=session)
        context.close = AsyncMock()
        browser = Mock()
        browser.close = AsyncMock()

        async def new_context(**kwargs: Any) -> Mock:
            self.contexts.append(kwargs)
            return context

        async def launch(**kwargs: Any) -> Mock:
            self.launches.append(kwargs)
            return browser

        browser.new_context = new_context
        playwright = Mock()
        playwright.chromium.launch = launch
        playwright.stop = AsyncMock()
        manager = Mock()
        manager.start = AsyncMock(return_value=playwright)
        monkeypatch.setattr(
            "agent_core.adapters.browser.playwright.async_playwright", lambda: manager
        )


async def test_interactive_ceremony_launches_headed_chromium_on_its_own_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A headless login browser is refused by sites that score the login request."""
    chromium = FakeChromiumLaunches()
    chromium.install(monkeypatch)
    display = FakeVirtualDisplay()
    runtime = PythonPlaywrightRuntime(virtual_display_factory=lambda: display)

    await runtime.start("http://127.0.0.1:9", ("https://site.example",), interactive=True)

    assert chromium.launches[0]["headless"] is False
    assert chromium.launches[0]["env"]["DISPLAY"] == ":77"
    assert display.started

    await runtime.close()

    assert display.closed


async def test_run_attempt_lease_stays_headless_without_a_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = FakeChromiumLaunches()
    chromium.install(monkeypatch)
    display = FakeVirtualDisplay()
    runtime = PythonPlaywrightRuntime(virtual_display_factory=lambda: display)

    await runtime.start("http://127.0.0.1:9", ("https://site.example",), interactive=False)

    assert chromium.launches[0]["headless"] is True
    assert "DISPLAY" not in chromium.launches[0]["env"]
    assert not display.started


async def test_interactive_ceremony_never_falls_back_to_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = FakeChromiumLaunches()
    chromium.install(monkeypatch)
    display = FakeVirtualDisplay(failure=OSError("synthetic display failure"))
    runtime = PythonPlaywrightRuntime(virtual_display_factory=lambda: display)

    with pytest.raises(BrowserProviderError) as raised:
        await runtime.start("http://127.0.0.1:9", ("https://site.example",), interactive=True)

    assert raised.value.reason_code == "tool.browser.provider_unavailable"
    assert chromium.launches == []


async def test_cancelled_ceremony_start_destroys_its_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation is not an Exception, and the runtime does not own the display yet."""
    chromium = FakeChromiumLaunches()
    chromium.install(monkeypatch)
    display = FakeVirtualDisplay(failure=asyncio.CancelledError())
    runtime = PythonPlaywrightRuntime(virtual_display_factory=lambda: display)

    with pytest.raises(asyncio.CancelledError):
        await runtime.start("http://127.0.0.1:9", ("https://site.example",), interactive=True)

    assert display.closed
    assert chromium.launches == []


async def test_interactive_ceremony_reports_its_real_browser_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusing website is unsupported; the runtime must not disguise automation."""
    chromium = FakeChromiumLaunches()
    chromium.install(monkeypatch)
    runtime = PythonPlaywrightRuntime(virtual_display_factory=lambda: None)

    await runtime.start("http://127.0.0.1:9", ("https://site.example",), interactive=True)

    assert "user_agent" not in chromium.contexts[0]
    assert "ignore_default_args" not in chromium.launches[0]
    assert not any("AutomationControlled" in argument for argument in chromium.launches[0]["args"])


async def test_production_runtime_uses_the_platform_display_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = FakeChromiumLaunches()
    chromium.install(monkeypatch)
    display = FakeVirtualDisplay()
    monkeypatch.setattr(
        "agent_core.adapters.browser.playwright.platform_virtual_display", lambda: display
    )

    await PythonPlaywrightRuntime().start(
        "http://127.0.0.1:9", ("https://site.example",), interactive=True
    )

    assert chromium.launches[0]["env"]["DISPLAY"] == ":77"


class FakeCeremonyPage:
    """A login-ceremony page whose visible sign-in challenge the test controls."""

    def __init__(self, url: str) -> None:
        """Start signed out with no challenge showing, like a marketing landing page."""
        self.url = url
        self.password_visible = False
        self.challenge_text_visible = False
        self.challenge_text_hidden = False
        self.keyboard = SimpleNamespace(
            insert_text=AsyncMock(), type=AsyncMock(), press=AsyncMock()
        )
        self.mouse = SimpleNamespace(click=AsyncMock())

    def locator(self, selector: str) -> Mock:
        """Return the challenge fields; a hidden password input stays in the DOM."""
        assert "input[type=password]" in selector
        field = Mock(spec=Locator)
        field.is_visible = AsyncMock(return_value=self.password_visible)
        fields = Mock(spec=Locator)
        fields.count = AsyncMock(return_value=1)
        fields.nth.return_value = field
        return fields

    def get_by_text(self, pattern: object) -> Mock:
        """Return the MFA, passkey, and consent text matches; hidden ones stay in the DOM."""
        del pattern
        match = Mock(spec=Locator)
        match.is_visible = AsyncMock(return_value=self.challenge_text_visible)
        matches = Mock(spec=Locator)
        matches.count = AsyncMock(
            return_value=int(self.challenge_text_visible or self.challenge_text_hidden)
        )
        matches.nth.return_value = match
        return matches


def ceremony_runtime(page: FakeCeremonyPage) -> PythonPlaywrightRuntime:
    """Bind a runtime to a page whose context already holds analytics storage state."""
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://www.duolingo.com",)
    runtime._page = page  # type: ignore[assignment]
    runtime._context = SimpleNamespace(  # type: ignore[assignment]
        storage_state=AsyncMock(
            return_value={
                "cookies": [{"name": "consent", "value": "synthetic"}],
                "origins": [{"origin": "https://www.duolingo.com", "localStorage": []}],
            }
        )
    )
    return runtime


@pytest.mark.parametrize(
    "events",
    [
        pytest.param((), id="no-interaction"),
        pytest.param(
            (BrowserInteractiveEvent(kind="click", x=40, y=600),),
            id="consent-banner-click",
        ),
    ],
)
async def test_signed_out_page_with_analytics_cookies_is_not_ready(
    events: tuple[BrowserInteractiveEvent, ...],
) -> None:
    """Cookies a landing page sets on its own are not evidence that anyone signed in."""
    page = FakeCeremonyPage("https://www.duolingo.com/?isLoggingIn=true")
    runtime = ceremony_runtime(page)
    for event in events:
        await runtime.interactive_event(event)

    status = await runtime.authentication_status()

    assert status is BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED


async def test_sign_in_entered_at_a_visible_challenge_that_then_clears_is_ready() -> None:
    """The runtime mediated the sign-in, so it can vouch for the session it seals."""
    page = FakeCeremonyPage("https://www.duolingo.com/?isLoggingIn=true")
    runtime = ceremony_runtime(page)
    page.password_visible = True
    await runtime.interactive_event(BrowserInteractiveEvent(kind="text", text="synthetic-entry"))
    page.password_visible = False
    page.url = "https://www.duolingo.com/learn"

    status = await runtime.authentication_status()

    assert status is BrowserAuthenticationStatus.READY


async def test_text_sent_while_no_challenge_is_visible_is_not_sign_in_evidence() -> None:
    """A search box or a first e-mail step must not end the ceremony before the password."""
    page = FakeCeremonyPage("https://www.duolingo.com/?isLoggingIn=true")
    runtime = ceremony_runtime(page)
    await runtime.interactive_event(BrowserInteractiveEvent(kind="text", text="synthetic-entry"))

    status = await runtime.authentication_status()

    assert status is BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED
    page.keyboard.type.assert_awaited_once_with("synthetic-entry")


async def test_sent_text_reaches_the_page_as_key_presses_not_a_paste() -> None:
    """Websites score a login whose fields fill with no keyboard events as automated.

    The user typed every character on the direct surface, so the runtime replays
    each one as a key press rather than inserting the whole string at once.
    """
    page = FakeCeremonyPage("https://www.duolingo.com/?isLoggingIn=true")
    runtime = ceremony_runtime(page)
    page.password_visible = True

    await runtime.interactive_event(BrowserInteractiveEvent(kind="text", text="synthetic-entry"))

    page.keyboard.type.assert_awaited_once_with("synthetic-entry")
    page.keyboard.insert_text.assert_not_awaited()


@pytest.mark.parametrize(
    ("password_visible", "challenge_text_visible"),
    [
        pytest.param(True, False, id="rejected-password-form-still-showing"),
        pytest.param(False, True, id="verification-code-step"),
    ],
)
async def test_a_visible_challenge_still_needs_the_user_after_sign_in_was_entered(
    password_visible: bool,
    challenge_text_visible: bool,
) -> None:
    """Entering text never outranks a challenge the user has not completed."""
    page = FakeCeremonyPage("https://www.duolingo.com/?isLoggingIn=true")
    runtime = ceremony_runtime(page)
    page.password_visible = True
    await runtime.interactive_event(BrowserInteractiveEvent(kind="text", text="synthetic-entry"))
    page.password_visible = password_visible
    page.challenge_text_visible = challenge_text_visible

    status = await runtime.authentication_status()

    assert status is BrowserAuthenticationStatus.NEEDS_USER


async def test_hidden_challenge_text_does_not_hold_a_finished_sign_in() -> None:
    """A collapsed verification-code template in the DOM is not a challenge anyone sees."""
    page = FakeCeremonyPage("https://www.duolingo.com/?isLoggingIn=true")
    runtime = ceremony_runtime(page)
    page.password_visible = True
    await runtime.interactive_event(BrowserInteractiveEvent(kind="text", text="synthetic-entry"))
    page.password_visible = False
    page.challenge_text_hidden = True

    status = await runtime.authentication_status()

    assert status is BrowserAuthenticationStatus.READY


async def test_text_sent_beside_hidden_challenge_text_is_not_sign_in_evidence() -> None:
    """Hidden challenge text must not turn a search box into a mediated sign-in."""
    page = FakeCeremonyPage("https://www.duolingo.com/?isLoggingIn=true")
    runtime = ceremony_runtime(page)
    page.challenge_text_hidden = True
    await runtime.interactive_event(BrowserInteractiveEvent(kind="text", text="synthetic-entry"))
    page.challenge_text_hidden = False

    status = await runtime.authentication_status()

    assert status is BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED


class FakeEvidencePage(FakeCeremonyPage):
    """A page that loads one URL, optionally redirected, and reports its challenge."""

    def __init__(self, *, lands_on: str | None = None, refused_hop: str | None = None) -> None:
        super().__init__("about:blank")
        self.lands_on = lands_on
        self.refused_hop = refused_hop
        self.failure: PlaywrightError | None = None
        self.handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.loads: list[tuple[str, str, int]] = []
        self.idle_waits: list[tuple[str, int]] = []

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.loads.append((url, wait_until, timeout))
        if self.refused_hop is not None:
            for handler in self.handlers.get("request", ()):
                handler(FakeNavigationRequest(self.refused_hop))
            raise PlaywrightError(f"net::ERR_BLOCKED_BY_CLIENT at {self.refused_hop}")
        if self.failure is not None:
            raise self.failure
        self.url = self.lands_on or url

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self.idle_waits.append((state, timeout))
        raise PlaywrightError("Timeout 5000ms exceeded.")


def evidence_runtime(page: FakeEvidencePage) -> PythonPlaywrightRuntime:
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://www.duolingo.com",)
    runtime._attach_page(page)  # type: ignore[arg-type]
    return runtime


async def test_page_evidence_reports_origin_path_and_challenge() -> None:
    """ADR-0128: a verification load reports where it landed and whether it asks to sign in."""

    members = FakeEvidencePage()
    signed_out = FakeEvidencePage(lands_on="https://www.duolingo.com/log-in?next=/learn")
    signed_out.password_visible = True
    refused = FakeEvidencePage(refused_hop="https://accounts.example.net/login")

    kept = await evidence_runtime(members).load_page_evidence("https://www.duolingo.com/learn")
    challenged = await evidence_runtime(signed_out).load_page_evidence(
        "https://www.duolingo.com/learn"
    )
    left = await evidence_runtime(refused).load_page_evidence("https://www.duolingo.com/learn")

    assert kept == BrowserPageEvidence(
        on_allowed_origin=True, path="/learn", challenge_visible=False
    )
    assert challenged == BrowserPageEvidence(
        on_allowed_origin=True, path="/log-in", challenge_visible=True
    )
    assert left.on_allowed_origin is False
    assert members.loads == [("https://www.duolingo.com/learn", "load", 20_000)]
    assert members.idle_waits == [("networkidle", 5_000)]
    assert "learn" not in repr(kept)


async def test_page_evidence_normalizes_other_load_failures() -> None:
    page = FakeEvidencePage()
    page.failure = PlaywrightError("net::ERR_CONNECTION_RESET at https://www.duolingo.com/learn")

    with pytest.raises(BrowserProviderError) as raised:
        await evidence_runtime(page).load_page_evidence("https://www.duolingo.com/learn")

    assert raised.value.reason_code == "tool.browser.provider_unavailable"
    assert "duolingo" not in str(raised.value)


async def test_the_context_seam_carries_todays_arguments_and_only_a_subclass_extends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production never ignores certificate errors; a test runtime may (ADR-0128 R-4)."""

    class TestOnlyRuntime(PythonPlaywrightRuntime):
        def _context_options(self, storage_state: dict[str, object] | None) -> dict[str, Any]:
            return super()._context_options(storage_state) | {"ignore_https_errors": True}

    chromium = FakeChromiumLaunches()
    chromium.install(monkeypatch)
    await PythonPlaywrightRuntime().start("http://127.0.0.1:9", ("https://site.example",))
    await TestOnlyRuntime().start("http://127.0.0.1:9", ("https://site.example",))

    assert chromium.contexts[0] == {
        "accept_downloads": False,
        "service_workers": "block",
        "storage_state": None,
    }
    assert chromium.contexts[1] == {**chromium.contexts[0], "ignore_https_errors": True}


class FakeSettlingPage:
    """A lesson page whose in-page quiet wait is where late content lands (ADR-0130)."""

    def __init__(
        self,
        text: str,
        *,
        settles_to: str | None = None,
        never_settles: bool = False,
        navigation_destroys_first_wait: bool = False,
    ) -> None:
        self.url = "about:blank"
        self.text = text
        self.settles_to = settles_to
        self.never_settles = never_settles
        self.navigation_destroys_first_wait = navigation_destroys_first_wait
        self.quiet_waits: list[Any] = []
        self.load_waits: list[str] = []
        self.handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.buttons: list[AsyncMock] = []

    def button(self, name: str, *, on_click: Callable[[], None] | None = None) -> AsyncMock:
        handle = control(name)
        handle.get_attribute.return_value = None
        handle.is_enabled.return_value = True

        async def evaluate(expression: str) -> object:
            if "tagName.toLowerCase()" in expression and "getAttribute" not in expression:
                return "button"
            return {"tag": "button", "role": None, "inputType": None, "name": name}

        async def click(*, timeout: int) -> None:
            del timeout
            if on_click is not None:
                on_click()

        handle.evaluate.side_effect = evaluate
        handle.click.side_effect = click
        self.buttons.append(handle)
        return handle

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        del wait_until, timeout
        self.url = url

    def locator(self, selector: str) -> Mock:
        located = Mock(spec=Locator)
        if selector == "body":
            located.inner_text = AsyncMock(side_effect=lambda **_: self.text)
        else:
            located.element_handles = AsyncMock(side_effect=lambda: list(self.buttons))
        return located

    async def title(self) -> str:
        return "Lesson"

    async def evaluate(self, script: str, argument: Any = None) -> Any:
        if "getBoundingClientRect" in script:
            return [True] * len(argument)
        assert "MutationObserver" in script
        self.quiet_waits.append(argument)
        if self.navigation_destroys_first_wait and len(self.quiet_waits) == 1:
            raise PlaywrightError("Execution context was destroyed, most likely a navigation")
        if self.never_settles:
            await asyncio.sleep(3600)
        if self.settles_to is not None:
            self.text = self.settles_to
        return None

    async def wait_for_load_state(self, state: str, *, timeout: float) -> None:
        del timeout
        self.load_waits.append(state)


def settling_runtime(page: FakeSettlingPage) -> PythonPlaywrightRuntime:
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://site.example",)
    runtime._attach_page(page)  # type: ignore[arg-type]
    return runtime


@pytest.fixture
def quick_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playwright_adapter, "SETTLE_SECONDS", 0.05, raising=False)


@pytest.mark.usefixtures("quick_settle")
async def test_navigation_observes_the_page_after_it_settles() -> None:
    """ADR-0130 decision 5: navigation waits for the network and the DOM to go quiet."""

    page = FakeSettlingPage("Loading", settles_to="Exercise 1")
    page.button("Check")

    observation = await settling_runtime(page).navigate("https://site.example/lesson")

    assert observation.text == "Exercise 1"
    assert page.load_waits[:1] == ["networkidle"]


@pytest.mark.usefixtures("quick_settle")
async def test_action_observes_the_page_after_it_settles() -> None:
    """ADR-0130 decision 6: act returns the page after the action's effect settles."""

    page = FakeSettlingPage("Exercise 1")

    def check() -> None:
        page.text = "Checking"
        page.settles_to = "Correct!"

    page.button("Check", on_click=check)
    runtime = settling_runtime(page)
    before = await runtime.navigate("https://site.example/lesson")

    after = await runtime.act(
        BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision=before.revision,
            ref=before.elements[0].ref,
        )
    )

    assert after.text == "Correct!"


@pytest.mark.usefixtures("quick_settle")
async def test_a_page_that_never_settles_is_observed_at_the_bound() -> None:
    page = FakeSettlingPage("Clock 1", never_settles=True)
    page.button("Stay")

    observation = await asyncio.wait_for(
        settling_runtime(page).navigate("https://site.example/lesson"), 1
    )

    assert observation.text == "Clock 1"


@pytest.mark.usefixtures("quick_settle")
async def test_settling_survives_a_navigation_that_destroys_the_context() -> None:
    page = FakeSettlingPage("Old", settles_to="New", navigation_destroys_first_wait=True)
    page.button("Next")

    observation = await settling_runtime(page).navigate("https://site.example/lesson")

    assert observation.text == "New"
    assert "domcontentloaded" in page.load_waits
    assert len(page.quiet_waits) == 2


@pytest.mark.usefixtures("quick_settle")
async def test_act_can_follow_act_without_observe() -> None:
    page = FakeSettlingPage("Exercise 1")
    page.button("Check", on_click=lambda: setattr(page, "text", "Correct!"))
    runtime = settling_runtime(page)
    first = await runtime.navigate("https://site.example/lesson")
    action = BrowserAction(
        kind=BrowserActionKind.CLICK,
        expected_revision=first.revision,
        ref=first.elements[0].ref,
    )

    second = await runtime.act(action)
    third = await runtime.act(
        action.model_copy(
            update={
                "expected_revision": second.revision,
                "ref": second.elements[0].ref,
            }
        )
    )

    assert third.revision not in {first.revision, second.revision}


FACTS_PAGE = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="password" aria-label="Password">
<input autocomplete="cc-number" aria-label="Card number">
<input type="email" aria-label="Email">
<textarea aria-label="Answer"></textarea>
<a href="/settings">Settings</a>
<form action="/lesson/submit"><button formaction="/lesson/../checkout">Check</button></form>
<a href="/files/report.pdf" download>Report</a>
<dialog open aria-label="Try Super free"><button>Keep going</button></dialog>
<button aria-label="Continue">Pay $12.99</button>
<a href="/lesson/2">Next lesson</a>
<a href="#top">Top</a>
<a href="mailto:owner@example.org">Write</a>
</body></html>"""


async def test_element_facts_classify_fields_links_forms_dialogs() -> None:
    """ADR-0129 section 4.5: facts are derived from live attributes, in a real Chromium."""

    require_real_browser()

    async def lesson(request: Request) -> Response:
        del request
        return HTMLResponse(FACTS_PAGE)

    async with local_https_site(Starlette(routes=[Route("/lesson/1", lesson)])) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,))
        try:
            observation = await runtime.navigate(site.url("/lesson/1"))
            facts = runtime.facts(observation.revision)
            stale = runtime.facts("another-revision")
        finally:
            await runtime.close()

    assert facts is not None
    assert stale is None
    assert facts.revision == observation.revision
    by_name = {element.name: facts.elements[element.ref] for element in observation.elements}
    assert by_name["Password"].field_kind is BrowserFieldKind.PASSWORD
    assert by_name["Card number"].field_kind is BrowserFieldKind.PAYMENT
    assert by_name["Email"].field_kind is BrowserFieldKind.EMAIL
    assert by_name["Answer"].field_kind is BrowserFieldKind.MULTILINE
    assert by_name["Settings"].link_target == BrowserTargetFacts(
        same_origin=True, first_segment="settings", sensitive_path=True
    )
    assert by_name["Check"].form_target == BrowserTargetFacts(
        same_origin=True, first_segment="checkout", sensitive_path=True
    )
    assert by_name["Report"].download is True
    assert by_name["Keep going"].context_name == "Try Super free"
    assert by_name["Continue"].labels == {BrowserLabelSource.VISIBLE_TEXT: "Pay $12.99"}
    assert by_name["Next lesson"].link_target == BrowserTargetFacts(
        same_origin=True, first_segment="lesson", sensitive_path=False
    )
    assert by_name["Top"].link_target is None
    assert by_name["Write"].link_target is not None
    assert by_name["Write"].link_target.same_origin is False
    rendered = facts.model_dump_json()
    assert "/settings" not in rendered and "checkout" in rendered
    assert "report.pdf" not in rendered and "mailto" not in rendered
