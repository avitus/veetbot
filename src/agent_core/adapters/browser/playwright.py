"""Ephemeral Playwright browser provider with audited proxy egress."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from datetime import datetime
from typing import Any, Literal, NoReturn, Protocol, cast
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Dialog,
    Download,
    ElementHandle,
    Frame,
    JSHandle,
    Page,
    Playwright,
    Request,
    Route,
    StorageState,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from agent_core.adapters.browser.virtual_display import platform_virtual_display
from agent_core.domain.browser import (
    MAXIMUM_FACT_LABEL_CHARACTERS,
    TASK_GRANT_PATH_SEGMENT,
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationStatus,
    BrowserDispatchConstraint,
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserInteractiveEvent,
    BrowserLabelSource,
    BrowserObservation,
    BrowserObservationFacts,
    BrowserPageEvidence,
    BrowserProviderError,
    BrowserTargetFacts,
    browser_origin,
    normalize_browser_origin,
)
from agent_core.domain.browser_classification import (
    dispatch_constraint_coverage,
    normalize_text,
    path_is_sensitive,
)
from agent_core.domain.execution import EgressDestination, EgressMode, EgressPolicy
from agent_core.domain.web import is_public_https_url
from agent_core.execution.proxy import start_browser_egress_proxy

MAXIMUM_ELEMENTS = 256
# Candidates scanned for visibility before the element cap applies (ADR-0130).
MAXIMUM_SCANNED_ELEMENTS = 4_096
MAXIMUM_TEXT_CHARACTERS = 262_144
# A page settles for at most this long after navigation or an action (ADR-0130).
SETTLE_SECONDS = 2.0
# The DOM counts as quiet after this long without a mutation.
DOM_QUIET_MILLISECONDS = 300
_QUIET_SCRIPT = """([quietMs, timeoutMs]) => new Promise(resolve => {
    let quiet = 0;
    let limit = 0;
    const observer = new MutationObserver(() => {
        clearTimeout(quiet);
        quiet = setTimeout(finish, quietMs);
    });
    function finish() {
        observer.disconnect();
        clearTimeout(quiet);
        clearTimeout(limit);
        resolve(null);
    }
    observer.observe(document, {
        subtree: true, childList: true, attributes: true, characterData: true
    });
    quiet = setTimeout(finish, quietMs);
    limit = setTimeout(finish, timeoutMs);
})"""
# Playwright's is_visible in one round trip, ported from its computeBox: a
# display:contents node is visible when a child element or text is; any other
# node needs checkVisibility(), visibility:visible and a non-empty box.
_VISIBLE_SCRIPT = """nodes => {
    const visible = node => {
        const view = node.ownerDocument.defaultView;
        const style = view ? view.getComputedStyle(node) : null;
        if (!style) {
            return true;
        }
        if (style.display === 'contents') {
            for (let child = node.firstChild; child; child = child.nextSibling) {
                if (child.nodeType === 1 && visible(child)) {
                    return true;
                }
                if (child.nodeType === 3) {
                    const range = child.ownerDocument.createRange();
                    range.selectNode(child);
                    const box = range.getBoundingClientRect();
                    if (box.width > 0 && box.height > 0) {
                        return true;
                    }
                }
            }
            return false;
        }
        if (Element.prototype.checkVisibility) {
            if (!node.checkVisibility()) {
                return false;
            }
        } else {
            const details = node.closest('details,summary');
            if (details !== node && details && details.nodeName === 'DETAILS' && !details.open) {
                return false;
            }
        }
        if (style.visibility !== 'visible') {
            return false;
        }
        const box = node.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };
    return nodes.map(visible);
}"""


class BrowserRuntime(Protocol):
    async def start(self, proxy_url: str, allowed_origins: tuple[str, ...]) -> None: ...

    async def navigate(self, url: str) -> BrowserObservation: ...

    async def observe(self) -> BrowserObservation: ...

    async def act(
        self,
        action: BrowserAction,
        *,
        constraint: BrowserDispatchConstraint | None = None,
        now: datetime | None = None,
    ) -> BrowserObservation: ...

    async def close(self) -> None: ...


class BrowserProxy(Protocol):
    url: str

    async def close(self) -> None: ...


ProxyFactory = Callable[..., Awaitable[BrowserProxy]]


class VirtualDisplay(Protocol):
    async def start(self) -> str: ...

    async def close(self) -> None: ...


VirtualDisplayFactory = Callable[[], VirtualDisplay | None]


def _origin_allowed(url: str, allowed_origins: tuple[str, ...]) -> bool:
    try:
        return browser_origin(url) in allowed_origins
    except ValueError:
        return False


class PythonPlaywrightRuntime:
    """Own one Chromium process, headed only for a ceremony, and one non-persistent context."""

    def __init__(
        self,
        *,
        virtual_display_factory: VirtualDisplayFactory | None = None,
    ) -> None:
        """Initialize the scoped state used by this adapter."""
        self._virtual_display_factory = virtual_display_factory
        self._virtual_display: VirtualDisplay | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._temporary_home: tempfile.TemporaryDirectory[str] | None = None
        self._allowed_origins: tuple[str, ...] = ()
        self._revision: str | None = None
        self._elements: dict[str, ElementHandle] = {}
        self._facts: BrowserObservationFacts | None = None
        self._disallowed_navigation = False
        self._document_session: CDPSession | None = None
        self._main_frame_id: str | None = None
        self._sign_in_entered = False
        self._main_frame_navigations = 0

    async def start(
        self,
        proxy_url: str,
        allowed_origins: tuple[str, ...],
        *,
        storage_state: dict[str, object] | None = None,
        interactive: bool = False,
    ) -> None:
        """Launch the isolated browser context with origin interception and audited egress."""
        if self._browser is not None:
            return
        self._allowed_origins = allowed_origins
        self._temporary_home = tempfile.TemporaryDirectory(prefix="veetbot-browser-")
        temporary_home = self._temporary_home.name
        environment: dict[str, str | float | bool] = {
            "HOME": temporary_home,
            "PATH": os.defpath,
            "TMPDIR": temporary_home,
        }
        # Websites refuse a login from a browser that reports itself headless,
        # so the user's ceremony is headed and never falls back (ADR-0106).
        if interactive:
            display_name = await self._start_virtual_display()
            if display_name is not None:
                environment["DISPLAY"] = display_name
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=not interactive,
            proxy={"server": proxy_url},
            args=[
                "--proxy-bypass-list=<-loopback>",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ],
            env=environment,
        )
        self._context = await self._browser.new_context(**self._context_options(storage_state))
        await self._context.route("**/*", self._route)
        self._attach_page(await self._context.new_page())
        self._document_session = await self._context.new_cdp_session(self._current_page())
        tree = await self._document_session.send("Page.getFrameTree")
        self._main_frame_id = str(tree["frameTree"]["frame"]["id"])
        self._document_session.on("Fetch.requestPaused", self._guard_document_request)
        await self._document_session.send(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}
                ]
            },
        )
        self._context.on("page", self._close_popup)

    def _context_options(self, storage_state: dict[str, object] | None) -> dict[str, Any]:
        """The arguments of the one browser context; a test subclass may extend them."""

        return {
            "accept_downloads": False,
            "service_workers": "block",
            "storage_state": (None if storage_state is None else cast(StorageState, storage_state)),
        }

    async def _start_virtual_display(self) -> str | None:
        """Start the ceremony's private display, or use the platform's native one."""
        display = (self._virtual_display_factory or platform_virtual_display)()
        if display is None:
            return None
        try:
            display_name = await display.start()
        except asyncio.CancelledError:
            # The runtime does not own the display yet, so its close() cannot.
            with suppress(Exception):
                await display.close()
            raise
        except Exception as exc:
            with suppress(Exception):
                await display.close()
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=True,
            ) from exc
        self._virtual_display = display
        return display_name

    async def _guard_document_request(self, event: dict[str, Any]) -> None:
        """Check document redirects before dispatch, even when a CDN tunnel exists."""
        session = self._document_session
        if session is None:
            return
        url = str(event.get("request", {}).get("url", ""))
        frame_id = event.get("frameId")
        main_frame = frame_id == self._main_frame_id
        allowed = (
            bool(frame_id)
            and is_public_https_url(url)
            and (not main_frame or _origin_allowed(url, self._allowed_origins))
        )
        if not allowed and main_frame:
            self._disallowed_navigation = True
        parameters = {"requestId": event["requestId"]}
        if not allowed:
            parameters["errorReason"] = "BlockedByClient"
        with suppress(PlaywrightError):
            await session.send(
                "Fetch.continueRequest" if allowed else "Fetch.failRequest", parameters
            )

    def _attach_page(self, page: Page) -> None:
        """Install request tracking and page guards before navigation begins."""
        page.on("dialog", self._dismiss_dialog)
        page.on("download", self._cancel_download)
        # Record refused navigation for failure classification; the CDP document
        # guard enforces redirect hops before dispatch.
        page.on("request", self._track_navigation)
        page.on("framenavigated", self._count_main_frame_navigation)
        self._page = page

    def _count_main_frame_navigation(self, frame: Frame) -> None:
        """Count committed main-frame documents so an action knows it loaded a new one."""
        if frame.parent_frame is None:
            self._main_frame_navigations += 1

    def _track_navigation(self, request: Request) -> None:
        """Remember refused navigation origins for stable browser failure classification."""
        if (
            request.is_navigation_request()
            and request.frame.parent_frame is None
            and not _origin_allowed(request.url, self._allowed_origins)
        ):
            self._disallowed_navigation = True

    async def _route(self, route: Route) -> None:
        request = route.request
        allowed = is_public_https_url(request.url)
        if request.is_navigation_request() and request.frame.parent_frame is None:
            allowed = allowed and _origin_allowed(request.url, self._allowed_origins)
            if self._page is not None and request.frame.page is not self._page:
                allowed = False
        if allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def _dismiss_dialog(self, dialog: Dialog) -> None:
        await dialog.dismiss()

    async def _cancel_download(self, download: Download) -> None:
        await download.cancel()

    async def _close_popup(self, page: Page) -> None:
        if page is not self._page:
            await page.close()

    def _current_page(self) -> Page:
        if self._page is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return self._page

    async def navigate(self, url: str) -> BrowserObservation:
        """Navigate within the bound origin policy and preserve stable failure codes."""
        page = self._current_page()
        self._disallowed_navigation = False
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightError as exc:
            if self._disallowed_navigation:
                raise BrowserProviderError("tool.browser.url_disallowed", retryable=False) from exc
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=True,
            ) from exc
        if not _origin_allowed(page.url, self._allowed_origins):
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        await self._settle(page, after_document=True)
        if not _origin_allowed(page.url, self._allowed_origins):
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        return await self._observation(page)

    async def _settle(self, page: Page, *, after_document: bool) -> None:
        """Wait, at most SETTLE_SECONDS, for the page to stop loading and changing.

        ADR-0130 decisions 5 and 6: after a new document, first wait for the
        network to go idle; then wait in the page until the DOM has not
        changed for DOM_QUIET_MILLISECONDS. A page that never settles is
        observed at the bound. Nothing here fails: a context destroyed by a
        navigation waits for the new document and tries the quiet wait once
        more within the same deadline.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_SECONDS

        def remaining() -> float:
            return max(0.0, deadline - loop.time())

        async def load_state(state: Literal["domcontentloaded", "networkidle"]) -> None:
            left = remaining()
            if left <= 0:
                return
            with suppress(PlaywrightError, TimeoutError):
                async with asyncio.timeout(left):
                    await page.wait_for_load_state(state, timeout=max(1.0, left * 1000))

        if after_document:
            await load_state("networkidle")
        for attempt in range(2):
            left = remaining()
            if left <= 0:
                return
            try:
                async with asyncio.timeout(left + 0.25):
                    await page.evaluate(
                        _QUIET_SCRIPT, [DOM_QUIET_MILLISECONDS, max(1, int(left * 1000))]
                    )
                return
            except TimeoutError:
                return
            except PlaywrightError:
                if attempt:
                    return
                await load_state("domcontentloaded")

    def facts(self, revision: str) -> BrowserObservationFacts | None:
        """The element facts of ``revision``, while it is the current observation."""
        if self._facts is None or self._facts.revision != revision:
            return None
        return self._facts

    async def load_page_evidence(self, url: str) -> BrowserPageEvidence:
        """Load one page and report where it landed and whether it asks to sign in.

        ADR-0128 verifies a device handoff by loading the page the owner
        confirmed with and without the session. A navigation the origin guard
        refuses is evidence (the page left the allowed origins), not a failure;
        any other load failure is ``provider_unavailable``. Nothing from the
        page or Playwright's message leaves this method but the evidence.
        """
        page = self._current_page()
        self._disallowed_navigation = False
        try:
            await page.goto(url, wait_until="load", timeout=20_000)
        except PlaywrightError as exc:
            if self._disallowed_navigation:
                return BrowserPageEvidence(
                    on_allowed_origin=False, path="/", challenge_visible=False
                )
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=True,
            ) from exc
        with suppress(PlaywrightError):
            await page.wait_for_load_state("networkidle", timeout=5_000)
        try:
            challenge_visible = await self._sign_in_challenge_visible(page)
        except PlaywrightError as exc:
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=True,
            ) from exc
        return BrowserPageEvidence(
            on_allowed_origin=_origin_allowed(page.url, self._allowed_origins),
            path=(urlsplit(page.url).path or "/")[:4096],
            challenge_visible=challenge_visible,
        )

    async def observe(self) -> BrowserObservation:
        page = self._current_page()
        if not _origin_allowed(page.url, self._allowed_origins):
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return await self._observation(page)

    async def _observation(self, page: Page) -> BrowserObservation:
        revision = secrets.token_hex(16)
        body = page.locator("body")
        text = (await body.inner_text(timeout=5_000))[:MAXIMUM_TEXT_CHARACTERS]
        locator = page.locator("a,button,input,select,textarea,[role]")
        found = await locator.element_handles()
        candidates = found[:MAXIMUM_SCANNED_ELEMENTS]
        # Hidden controls never take an element slot (ADR-0130 decision 8).
        flags = await page.evaluate(_VISIBLE_SCRIPT, candidates)
        snapshot = [handle for handle, visible in zip(candidates, flags, strict=True) if visible][
            :MAXIMUM_ELEMENTS
        ]
        slots = asyncio.Semaphore(8)
        page_url = page.url

        async def capture(
            index: int, handle: ElementHandle
        ) -> tuple[BrowserElement, BrowserElementFacts] | None:
            async with slots:
                return await self._element_observation(
                    handle, f"{revision}:{index}", page_url=page_url
                )

        captured = await asyncio.gather(
            *(capture(index, handle) for index, handle in enumerate(snapshot))
        )
        elements: list[BrowserElement] = []
        handles: dict[str, ElementHandle] = {}
        facts: dict[str, BrowserElementFacts] = {}
        for handle, observed in zip(snapshot, captured, strict=True):
            if observed is not None:
                element, element_facts = observed
                elements.append(element)
                handles[element.ref] = handle
                facts[element.ref] = element_facts
        released = [*self._elements.values(), *found]
        self._revision = revision
        self._elements = handles
        self._facts = BrowserObservationFacts(revision=revision, elements=facts)
        await _dispose(released, keep=handles.values())
        return BrowserObservation(
            url=page.url,
            title=await page.title(),
            revision=revision,
            text=text,
            elements=tuple(elements),
        )

    @staticmethod
    async def _element_observation(
        handle: ElementHandle, ref: str, *, page_url: str
    ) -> tuple[BrowserElement, BrowserElementFacts] | None:
        """Read one captured node without resolving a selector that may have changed.

        The same read gives the element's facts (ADR-0129 section 4.5): its
        field kind, every label source, its link and form targets reduced to
        facts, whether it downloads, and its dialog's name. Raw target URLs
        stay in this process.
        """
        if not await handle.is_visible():
            return None
        metadata = await handle.evaluate(_ELEMENT_SCRIPT)
        tag = str(metadata["tag"])
        input_type = metadata["inputType"]
        checked: bool | None = None
        if tag == "input" and input_type in {"checkbox", "radio"}:
            checked = await handle.is_checked()
        role = metadata["role"] or _default_role(tag, input_type)
        element = BrowserElement(
            ref=ref,
            role=role,
            name=str(metadata["name"])[:1024],
            disabled=await handle.is_disabled(),
            checked=checked,
        )
        return element, _element_facts(metadata, name=element.name, page_url=page_url)

    async def act(
        self,
        action: BrowserAction,
        *,
        constraint: BrowserDispatchConstraint | None = None,
        now: datetime | None = None,
    ) -> BrowserObservation:
        page = self._current_page()
        if constraint is not None and (
            now is None
            or now >= constraint.not_after
            or not set(constraint.origins) <= set(self._allowed_origins)
        ):
            await self._refuse_grant()
        if action.expected_revision != self._revision:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        handle = self._elements.get(action.ref)
        if handle is None:
            raise BrowserProviderError("tool.browser.element_not_found", retryable=False)
        if not _origin_allowed(page.url, self._allowed_origins):
            raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
        if not await handle.is_visible() or not await handle.is_enabled():
            raise BrowserProviderError("tool.browser.element_not_found", retryable=False)

        tag = str(await handle.evaluate("node => node.tagName.toLowerCase()"))
        input_type = (await handle.get_attribute("type") or "").lower()
        autocomplete = (await handle.get_attribute("autocomplete") or "").lower()
        if action.kind is BrowserActionKind.TYPE and (
            tag not in {"input", "textarea"}
            or input_type == "password"
            or autocomplete in {"current-password", "new-password", "one-time-code"}
        ):
            raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
        if action.kind is BrowserActionKind.SELECT and tag != "select":
            raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
        if action.kind is BrowserActionKind.CHECK and (
            tag != "input" or input_type not in {"checkbox", "radio"}
        ):
            raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)

        focus_guard: JSHandle | None = None
        if constraint is not None:
            assert now is not None
            await self._require_live_coverage(page, handle, action, constraint, now=now)
            if action.kind in _KEYBOARD_KINDS:
                focus_guard = await self._hold_focus(handle)

        documents_before = self._main_frame_navigations
        try:
            if action.kind is BrowserActionKind.CLICK:
                await handle.click(timeout=30_000)
            elif action.kind is BrowserActionKind.TYPE:
                await handle.fill(action.value or "", timeout=30_000)
            elif action.kind is BrowserActionKind.SELECT:
                await handle.select_option(action.value or "", timeout=30_000)
            elif action.kind is BrowserActionKind.CHECK:
                await handle.check(timeout=30_000)
            elif action.kind is BrowserActionKind.PRESS:
                key = action.key.value if action.key is not None else ""
                if focus_guard is None:
                    await handle.press(key, timeout=30_000)
                else:
                    # The guard focused the element and verified it holds focus;
                    # the keyboard sends to the focused element.
                    await page.keyboard.press(key)
            else:
                await handle.scroll_into_view_if_needed(timeout=30_000)
                await page.mouse.wheel(0, action.delta_y or 0)
        except PlaywrightError as exc:
            if focus_guard is not None:
                await _release_focus(focus_guard)
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False) from exc
        if focus_guard is not None and await _release_focus(focus_guard):
            # A key or text went to another element and was stopped there;
            # what the page's own listeners did with it is unknown.
            await self._forget_observation()
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
        # The action was sent; settling never turns it into a failure (ADR-0130).
        await self._settle(page, after_document=self._main_frame_navigations != documents_before)
        return await self._observation(page)

    async def _hold_focus(self, handle: ElementHandle) -> JSHandle:
        """Focus the classified element and guard the keyboard until released.

        ADR-0129: a key or typed text goes to whatever holds focus, not to the
        element the classifier read. The element must hold focus itself,
        resolved through open shadow roots, or the act is refused before
        dispatch. Until released, a key or text event aimed at any other
        element is stopped with its default action, so focus moved by the page
        between this check and the keyboard cannot redirect the action.
        """
        try:
            guard = await handle.evaluate_handle(_FOCUS_GUARD_SCRIPT)
            held = bool(await guard.evaluate("guard => typeof guard === 'function'"))
        except PlaywrightError:
            await self._refuse_grant()
        if not held:
            with suppress(PlaywrightError):
                await guard.dispose()
            await self._refuse_grant()
        return guard

    async def _require_live_coverage(
        self,
        page: Page,
        handle: ElementHandle,
        action: BrowserAction,
        constraint: BrowserDispatchConstraint,
        *,
        now: datetime,
    ) -> None:
        """Recheck a grant-authorized act against the live page before dispatch (ADR-0129).

        The worker decided from the observation it was shown; this reads the
        live URL, every live label source at full length and live facts, and
        runs the same coverage rules. A constraint can only narrow.
        """
        try:
            metadata = await handle.evaluate(_ELEMENT_SCRIPT)
        except PlaywrightError:
            await self._refuse_grant()
        name = str(metadata.get("name") or "")[:1024]
        role = str(metadata.get("role") or "") or _default_role(
            str(metadata.get("tag") or ""), metadata.get("inputType")
        )
        coverage = dispatch_constraint_coverage(
            constraint,
            action=action,
            page_url=page.url,
            role=role,
            labels=[name, *_live_labels(metadata).values()],
            facts=_element_facts(metadata, name=name, page_url=page.url),
            option_texts=_option_texts(metadata, action),
            runtime_origins=self._allowed_origins,
            now=now,
        )
        if not coverage.covered:
            await self._refuse_grant()

    async def _refuse_grant(self) -> NoReturn:
        """Refuse before dispatch and forget the observation (ADR-0129 D16).

        The model must observe again, so a stale reference can neither burn
        grant uses nor, once the owner approves it, act on a changed element.
        """
        await self._forget_observation()
        raise BrowserProviderError("tool.browser.grant_not_applicable", retryable=False)

    async def _forget_observation(self) -> None:
        released = list(self._elements.values())
        self._revision = None
        self._elements = {}
        self._facts = None
        await _dispose(released, keep=())

    async def storage_state(self) -> dict[str, object]:
        if self._context is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return cast(dict[str, object], await self._context.storage_state(indexed_db=True))

    @staticmethod
    async def _sign_in_challenge_visible(page: Page) -> bool:
        """Report a password, one-time-code, CAPTCHA, passkey, MFA, or consent prompt."""
        intervention = page.locator(
            "input[type=password],input[autocomplete=one-time-code],"
            "iframe[src*='captcha' i],iframe[title*='captcha' i],"
            "[class*='captcha' i],[id*='captcha' i]"
        )
        for index in range(await intervention.count()):
            if await intervention.nth(index).is_visible():
                return True
        interactive_text = page.get_by_text(
            re.compile(
                r"(?:use\s+(?:a\s+)?passkey|verification\s+code|"
                r"multi-factor|two-factor|consent\s+required)",
                re.IGNORECASE,
            )
        )
        for index in range(await interactive_text.count()):
            if await interactive_text.nth(index).is_visible():
                return True
        return False

    async def authentication_status(self) -> BrowserAuthenticationStatus:
        page = self._current_page()
        if not _origin_allowed(page.url, self._allowed_origins):
            return BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED
        if await self._sign_in_challenge_visible(page):
            return BrowserAuthenticationStatus.NEEDS_USER
        if not self._sign_in_entered:
            # A signed-out page holds analytics and consent storage of its own,
            # so storage state alone never shows that anyone signed in.
            return BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED
        storage = await self.storage_state()
        if storage.get("cookies") or storage.get("origins"):
            return BrowserAuthenticationStatus.READY
        return BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED

    async def interactive_frame(self) -> bytes:
        return await self._current_page().screenshot(
            type="png",
            animations="disabled",
            caret="initial",
            scale="css",
        )

    async def interactive_event(self, event: BrowserInteractiveEvent) -> None:
        page = self._current_page()
        if event.kind == "click":
            assert event.x is not None and event.y is not None
            await page.mouse.click(event.x, event.y)
        elif event.kind == "text":
            assert event.text is not None
            # Checked before the text lands so a failed check loses no input.
            # Only the fact of entry is kept, never the text itself.
            if await self._sign_in_challenge_visible(page):
                self._sign_in_entered = True
            # The user typed each character, so each arrives as a key press;
            # a field filled with no keyboard events is scored as automated.
            await page.keyboard.type(event.text)
        else:
            assert event.key is not None
            await page.keyboard.press(event.key)

    async def close(self) -> None:
        """Release the browser and proxy resources owned by this session."""
        try:
            if self._context is not None:
                with suppress(Exception):
                    await self._context.close()
            if self._browser is not None:
                with suppress(Exception):
                    await self._browser.close()
            if self._playwright is not None:
                with suppress(Exception):
                    await self._playwright.stop()
            if self._virtual_display is not None:
                with suppress(Exception):
                    await self._virtual_display.close()
            if self._temporary_home is not None:
                with suppress(OSError):
                    self._temporary_home.cleanup()
        finally:
            self._virtual_display = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._page = None
            self._temporary_home = None
            self._revision = None
            self._elements = {}
            self._facts = None
            self._disallowed_navigation = False
            self._document_session = None
            self._main_frame_id = None
            self._sign_in_entered = False
            self._main_frame_navigations = 0


async def _release_focus(guard: JSHandle) -> bool:
    """Remove the focus guard; true when it stopped a key or text aimed elsewhere.

    A guard whose document is gone, because the action navigated, stopped
    nothing that could still act.
    """

    redirected = False
    with suppress(PlaywrightError):
        redirected = bool(await guard.evaluate("guard => guard()"))
    with suppress(PlaywrightError):
        await guard.dispose()
    return redirected


async def _dispose(handles: Iterable[ElementHandle], *, keep: Iterable[ElementHandle]) -> None:
    """Release page-side handles the runtime no longer references (ADR-0130 decision 8)."""

    kept = {id(handle) for handle in keep}
    released = {id(handle): handle for handle in handles if id(handle) not in kept}

    async def release(handle: ElementHandle) -> None:
        with suppress(PlaywrightError):
            await handle.dispose()

    await asyncio.gather(*(release(handle) for handle in released.values()))


# One read of an element: the model-visible name exactly as before, and every
# live attribute its facts are derived from (ADR-0129 section 4.5).
_ELEMENT_SCRIPT = """node => {
    const clean = value => Array.from(String(value || '').replace(/\\s+/g, ' ').trim())
        .slice(0, 1024).join('');
    const referenced = ids => String(ids || '').split(/\\s+/).filter(Boolean).slice(0, 8)
        .map(id => {
            const target = document.getElementById(id);
            return target ? target.textContent : '';
        }).join(' ');
    const resolve = value => {
        try { return new URL(value, document.baseURI).href; } catch (error) { return ''; }
    };
    const tag = node.tagName.toLowerCase();
    const type = (node.getAttribute('type') || '').toLowerCase();
    const link = node.closest('a[href]');
    const submits = element => !!element && !!element.form && (
        (element.tagName === 'BUTTON' && element.type === 'submit')
        || (element.tagName === 'INPUT' && ['submit', 'image'].includes(element.type)));
    const labelled = node.closest('label');
    const control = labelled ? labelled.control : null;
    // The submit control a click activates: the node, its button, or its
    // label's control. Otherwise Enter submits through the default button.
    const submitter = [node.closest('button'), node.closest('input'), control]
        .find(submits) || null;
    const form = submitter ? submitter.form
        : (node.form || (control ? control.form : null) || node.closest('form'));
    const defaultButton = form && !submitter
        ? Array.from(document.querySelectorAll('button,input'))
            .find(element => element.form === form && submits(element)) || null
        : null;
    // Only a submit control's formaction counts; a field's is ignored.
    const through = submitter || defaultButton;
    const formAction = through ? through.getAttribute('formaction') : null;
    const dialog = node.closest('dialog,[role=dialog],[role=alertdialog]');
    const heading = dialog ? dialog.querySelector('h1,h2,h3') : null;
    const images = Array.from(node.querySelectorAll('img')).slice(0, 8)
        .map(image => image.getAttribute('alt') || '');
    const valued = tag === 'button'
        || (tag === 'input' && ['submit', 'button', 'reset'].includes(type));
    return {
        tag,
        role: node.getAttribute('role'),
        inputType: node.getAttribute('type'),
        name: Array.from(node.getAttribute('aria-label') || node.getAttribute('title') ||
            node.getAttribute('placeholder') || node.innerText || '')
            .slice(0, 1024).join(''),
        autocomplete: node.getAttribute('autocomplete') || '',
        editable: node.isContentEditable === true,
        labels: {
            aria_label: clean(node.getAttribute('aria-label')),
            aria_labelledby: clean(referenced(node.getAttribute('aria-labelledby'))),
            label: clean(Array.from(node.labels || []).map(label => label.innerText).join(' ')),
            title: clean(node.getAttribute('title')),
            placeholder: clean(node.getAttribute('placeholder')),
            alt: clean([node.getAttribute('alt') || '', ...images].join(' ')),
            value: valued ? clean(node.getAttribute('value')) : '',
            visible_text: clean(node.innerText),
        },
        linkHref: link ? link.getAttribute('href') : null,
        link: link ? resolve(link.getAttribute('href')) : null,
        form: form
            ? resolve(formAction ? formAction : (form.getAttribute('action') || document.URL))
            : null,
        download: node.closest('a[download]') !== null,
        context: dialog ? clean(dialog.getAttribute('aria-label')
            || referenced(dialog.getAttribute('aria-labelledby'))
            || (heading ? heading.textContent : '')) : '',
        options: tag === 'select'
            ? Array.from(node.options).slice(0, 256).map(option => [option.label, option.value])
            : [],
    };
}"""
# Actions whose effect goes to the focused element rather than to the element.
_KEYBOARD_KINDS = frozenset({BrowserActionKind.PRESS, BrowserActionKind.TYPE})
# Focus the element and, only when it then holds focus itself (resolved through
# open shadow roots), return a function that removes the guard and reports
# whether it stopped a key or text event aimed at any other element. An
# embedded document would take the keys past the guard, so it never holds
# focus here. Key-up events elsewhere are stopped silently: Tab's own key-up
# lands where Tab moved focus.
_FOCUS_GUARD_SCRIPT = """node => {
    const embedded = ['iframe', 'frame', 'object', 'embed'];
    if (embedded.includes(node.localName)) {
        return null;
    }
    const holder = () => {
        let active = document.activeElement;
        while (active && active.shadowRoot && active.shadowRoot.activeElement) {
            active = active.shadowRoot.activeElement;
        }
        return active;
    };
    if (holder() !== node) {
        node.focus();
    }
    if (holder() !== node) {
        return null;
    }
    let redirected = false;
    const types = ['keydown', 'keypress', 'keyup', 'beforeinput'];
    const guard = event => {
        if (event.composedPath()[0] === node) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        if (event.type !== 'keyup') {
            redirected = true;
        }
    };
    types.forEach(type => window.addEventListener(type, guard, true));
    return () => {
        types.forEach(type => window.removeEventListener(type, guard, true));
        return redirected;
    };
}"""
_INPUT_FIELD_KINDS = {
    "": BrowserFieldKind.TEXT,
    "text": BrowserFieldKind.TEXT,
    "search": BrowserFieldKind.SEARCH,
    "email": BrowserFieldKind.EMAIL,
    "tel": BrowserFieldKind.TELEPHONE,
    "url": BrowserFieldKind.URL,
    "number": BrowserFieldKind.NUMBER,
    "date": BrowserFieldKind.DATE,
    "time": BrowserFieldKind.DATE,
    "month": BrowserFieldKind.DATE,
    "week": BrowserFieldKind.DATE,
    "datetime-local": BrowserFieldKind.DATE,
    "password": BrowserFieldKind.PASSWORD,
    "file": BrowserFieldKind.FILE,
    "checkbox": BrowserFieldKind.CHOICE,
    "radio": BrowserFieldKind.CHOICE,
    "submit": BrowserFieldKind.NONE,
    "button": BrowserFieldKind.NONE,
    "reset": BrowserFieldKind.NONE,
    "image": BrowserFieldKind.NONE,
}
_AUTOCOMPLETE_QUALIFIERS = frozenset(
    {"shipping", "billing", "home", "work", "mobile", "fax", "pager"}
)
_IDENTITY_TOKENS = frozenset(
    {
        "name",
        "given-name",
        "additional-name",
        "family-name",
        "nickname",
        "username",
        "street-address",
        "postal-code",
        "sex",
        "email",
        "impp",
        "url",
        "photo",
    }
)
_IDENTITY_PREFIXES = (
    "honorific-",
    "organization",
    "address-line",
    "address-level",
    "country",
    "bday",
    "tel",
)
_CHOICE_ROLES = frozenset({"checkbox", "radio", "option", "menuitemradio"})
MAXIMUM_CONTEXT_NAME_CHARACTERS = 128


def _field_kind(
    *, tag: str, input_type: str, autocomplete: str, role: str, editable: bool
) -> BrowserFieldKind:
    """The closed field kind of one element; its autocomplete token decides first."""

    tokens = [
        token
        for token in autocomplete.lower().split()
        if not token.startswith("section-") and token not in _AUTOCOMPLETE_QUALIFIERS
    ]
    token = tokens[-1] if tokens else ""
    if token and token not in {"on", "off"}:
        if token in {"current-password", "new-password"}:
            return BrowserFieldKind.PASSWORD
        if token == "one-time-code":
            return BrowserFieldKind.ONE_TIME_CODE
        if token.startswith(("cc-", "transaction-")):
            return BrowserFieldKind.PAYMENT
        if token in _IDENTITY_TOKENS or token.startswith(_IDENTITY_PREFIXES):
            return BrowserFieldKind.IDENTITY
        return BrowserFieldKind.OTHER
    if tag == "input":
        return _INPUT_FIELD_KINDS.get(input_type.lower(), BrowserFieldKind.OTHER)
    if tag == "textarea" or editable:
        return BrowserFieldKind.MULTILINE
    if tag == "select" or role.lower() in _CHOICE_ROLES:
        return BrowserFieldKind.CHOICE
    return BrowserFieldKind.NONE


def _origin_or_none(url: str) -> str | None:
    try:
        return browser_origin(url)
    except ValueError:
        return None


# A target on no HTTPS origin: ``javascript:``, whose script can go anywhere,
# or any other scheme. It is inside no origin and no prefix.
_OUTSIDE_EVERY_ORIGIN = BrowserTargetFacts(same_origin=False, first_segment=None)


def _target_facts(url: str, *, page_url: str) -> BrowserTargetFacts:
    """A navigation target reduced to facts; the raw URL never leaves the runtime."""

    origin = _origin_or_none(url)
    if origin is None:
        return _OUTSIDE_EVERY_ORIGIN
    same_origin = origin == _origin_or_none(page_url)
    path = urlsplit(url).path if url else ""
    segments = [segment for segment in path.split("/") if segment]
    first = segments[0] if segments else None
    return BrowserTargetFacts(
        same_origin=same_origin,
        first_segment=(
            first if first is not None and TASK_GRANT_PATH_SEGMENT.fullmatch(first) else None
        ),
        sensitive_path=path_is_sensitive(path),
    )


def _without_fragment(url: str) -> str:
    return urlsplit(url)._replace(fragment="").geturl()


def _link_target(metadata: dict[str, Any], *, page_url: str) -> BrowserTargetFacts | None:
    """A link's target facts; a fragment or empty href that stays on the page has none.

    A ``<base>`` element can send even ``#next`` to another document, so the
    resolved URL decides, not the written one. A ``javascript:`` link is a
    target outside every origin, never "no target".
    """

    raw = metadata.get("linkHref")
    resolved = metadata.get("link")
    if not isinstance(raw, str) or not isinstance(resolved, str):
        return None
    raw = raw.strip()
    if (not raw or raw.startswith("#")) and _without_fragment(resolved) == _without_fragment(
        page_url
    ):
        return None
    return _target_facts(resolved, page_url=page_url)


def _live_labels(metadata: dict[str, Any]) -> dict[BrowserLabelSource, str]:
    labels = metadata.get("labels")
    if not isinstance(labels, dict):
        return {}
    return {
        source: str(labels.get(source.value) or "")
        for source in BrowserLabelSource
        if labels.get(source.value)
    }


def _option_texts(metadata: dict[str, Any], action: BrowserAction) -> list[str]:
    """For a selection, the matched option's label and value, as the page has them."""

    if action.kind is not BrowserActionKind.SELECT or action.value is None:
        return []
    options = metadata.get("options")
    for option in options if isinstance(options, list) else []:
        if isinstance(option, list) and len(option) == 2 and action.value in option:
            return [str(option[0]), str(option[1])]
    return [action.value]


def _element_facts(metadata: dict[str, Any], *, name: str, page_url: str) -> BrowserElementFacts:
    """Derive one element's facts from its live attributes (ADR-0129 section 4.5)."""

    visible_name = normalize_text(name)
    labels = {
        source: value[:MAXIMUM_FACT_LABEL_CHARACTERS]
        for source, value in _live_labels(metadata).items()
        if normalize_text(value) != visible_name
    }
    form = metadata.get("form")
    return BrowserElementFacts(
        field_kind=_field_kind(
            tag=str(metadata.get("tag") or ""),
            input_type=str(metadata.get("inputType") or ""),
            autocomplete=str(metadata.get("autocomplete") or ""),
            role=str(metadata.get("role") or ""),
            editable=metadata.get("editable") is True,
        ),
        labels=labels,
        link_target=_link_target(metadata, page_url=page_url),
        form_target=(_target_facts(form, page_url=page_url) if isinstance(form, str) else None),
        download=metadata.get("download") is True,
        context_name=str(metadata.get("context") or "")[:MAXIMUM_CONTEXT_NAME_CHARACTERS],
    )


def _default_role(tag: str, input_type: str | None) -> str:
    if tag == "a":
        return "link"
    if tag == "button":
        return "button"
    if tag == "select":
        return "combobox"
    if tag == "textarea":
        return "textbox"
    if tag == "input":
        return {
            "checkbox": "checkbox",
            "radio": "radio",
            "submit": "button",
            "button": "button",
        }.get(input_type or "", "textbox")
    return "generic"


class PlaywrightBrowserProvider:
    name = "playwright"

    def __init__(
        self,
        *,
        tenant_id: str,
        allowed_origins: tuple[str, ...],
        runtime: BrowserRuntime | None = None,
        proxy_factory: ProxyFactory = start_browser_egress_proxy,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("browser provider requires a tenant")
        normalized = tuple(normalize_browser_origin(value) for value in allowed_origins)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("browser provider requires unique allowed origins")
        self._tenant_id = tenant_id
        self._allowed_origins = normalized
        self._runtime = runtime or PythonPlaywrightRuntime()
        self._proxy_factory = proxy_factory
        self._now = now
        self._proxy: BrowserProxy | None = None
        self._started = False
        self._start_lock = asyncio.Lock()

    def allows(self, url: str) -> bool:
        return _origin_allowed(url, self._allowed_origins)

    async def _start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            destinations = tuple(
                EgressDestination(
                    host=urlsplit(origin).hostname or "",
                    ports=frozenset({443}),
                )
                for origin in self._allowed_origins
            )
            try:
                self._proxy = await self._proxy_factory(
                    EgressPolicy(EgressMode.ALLOWLIST, destinations),
                    tenant_id=self._tenant_id,
                )
                await self._runtime.start(self._proxy.url, self._allowed_origins)
            except Exception as exc:
                with suppress(Exception):
                    await self._runtime.close()
                if self._proxy is not None:
                    with suppress(Exception):
                        await self._proxy.close()
                    self._proxy = None
                raise BrowserProviderError(
                    "tool.browser.provider_unavailable",
                    retryable=True,
                ) from exc
            self._started = True

    async def navigate(self, url: str) -> BrowserObservation:
        if not self.allows(url):
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        await self._start()
        try:
            observation = await self._runtime.navigate(url)
        except BrowserProviderError:
            raise
        except Exception as exc:
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=True,
            ) from exc
        if not self.allows(observation.url):
            raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
        return observation

    async def observe(self) -> BrowserObservation:
        await self._start()
        try:
            observation = await self._runtime.observe()
        except BrowserProviderError:
            raise
        except Exception as exc:
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=True,
            ) from exc
        if not self.allows(observation.url):
            raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
        return observation

    async def act(
        self,
        action: BrowserAction,
        *,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        await self._start()
        try:
            if constraint is None:
                observation = await self._runtime.act(action)
            else:
                # Without a clock the runtime cannot check the grant's expiry,
                # so it refuses the act.
                observation = await self._runtime.act(
                    action,
                    constraint=constraint,
                    now=None if self._now is None else self._now(),
                )
        except BrowserProviderError:
            raise
        except Exception as exc:
            raise BrowserProviderError(
                "tool.browser.outcome_unknown",
                retryable=False,
            ) from exc
        if not self.allows(observation.url):
            raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
        return observation

    async def close(self) -> None:
        try:
            await self._runtime.close()
        finally:
            if self._proxy is not None:
                await self._proxy.close()
            self._proxy = None
            self._started = False
