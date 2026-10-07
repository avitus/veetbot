"""Ephemeral Playwright browser provider with audited proxy egress."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
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
    FilePayload,
    Frame,
    JSHandle,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    StorageState,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from agent_core.adapters.browser.draft_confirmation import CONFIRM_DRAFT_SCRIPT
from agent_core.adapters.browser.extraction import EXTRACTION_SCRIPT, extracted_observation
from agent_core.adapters.browser.page_authentication import AUTHENTICATION_SCRIPT
from agent_core.adapters.browser.page_structure import REGION_CAPTURE_SCRIPT
from agent_core.adapters.browser.page_text import READABLE_TEXT_SCRIPT
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
    BrowserObservationCoverage,
    BrowserObservationExpansion,
    BrowserObservationFacts,
    BrowserObservationFocus,
    BrowserPageEvidence,
    BrowserProviderError,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
    BrowserTargetFacts,
    BrowserVerificationStage,
    browser_origin,
    ignore_verification_stage,
    normalize_browser_origin,
)
from agent_core.domain.browser_classification import (
    dispatch_constraint_coverage,
    label_reads_as,
    path_is_sensitive,
    path_is_within_prefix,
)
from agent_core.domain.browser_diagnostics import browser_phase, browser_phase_call
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.domain.browser_upload import BrowserImageFile
from agent_core.domain.execution import EgressDestination, EgressMode, EgressPolicy
from agent_core.domain.web import is_public_https_url
from agent_core.execution.proxy import start_browser_egress_proxy
from agent_core.ports.browser import (
    browser_action_interruption,
    check_browser_interruption,
    expand_browser_observation,
    extract_browser_observation,
)
from agent_core.ports.browser_upload import upload_browser_image

MAXIMUM_ELEMENTS = 256
# Candidates scanned for visibility before the element cap applies (ADR-0130).
MAXIMUM_SCANNED_ELEMENTS = 4_096
MAXIMUM_CANDIDATE_OFFSET = 65_536
# Return a bounded handle set in an isolated world, including open shadow roots.
# The numeric offset is supplied only by this runtime, never by a model.
_CONTROL_SELECTOR = """(() => {
    const queryAll = (root, selector) => {
        const offset = Number(selector);
        if (!Number.isInteger(offset) || offset < 0 || offset > 65536) return [];
        let dialog = null;
        if (root.nodeType === 9) {
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let candidate, visited = 0;
            while (visited++ < 8192 && (candidate = walker.nextNode())) {
                if (!candidate.matches('dialog[open],[role="dialog"],[role="alertdialog"]'))
                    continue;
                const style = getComputedStyle(candidate), box = candidate.getBoundingClientRect();
                if (style.visibility === 'visible' && style.display !== 'none'
                    && style.opacity !== '0' && box.width > 0 && box.height > 0
                    && !candidate.closest('[hidden],[aria-hidden="true"]')) {
                    dialog = candidate; break;
                }
            }
        }
        const result = [], pending = [];
        if (dialog?.firstElementChild) pending.push(root.firstElementChild);
        else dialog = null;
        let node = dialog?.firstElementChild || root.firstElementChild, index = 0;

        while (node) {
            if (node === dialog) { node = node.nextElementSibling || pending.pop(); continue; }
            if (node.matches('a,button,input,select,textarea,[role]')) {
                if (index++ >= offset) result.push(node);
                if (result.length === 4097) break;
            }
            if (node.nextElementSibling) pending.push(node.nextElementSibling);
            if (node.firstElementChild) pending.push(node.firstElementChild);
            node = node.shadowRoot?.firstElementChild || pending.pop();
        }
        return result;
    };
    return {queryAll, query: (root, selector) => queryAll(root, selector)[0] || null};
})()"""
# A page settles for at most this long after navigation or an action (ADR-0130).
SETTLE_SECONDS = 2.0
# The DOM counts as quiet after this long without a mutation.
DOM_QUIET_MILLISECONDS = 300
# How long Playwright waits for an element to become actionable, such as for
# its click point to reach the element itself, before an act fails.
ACTION_TIMEOUT_MILLISECONDS = 30_000
# Leave the outer tool budget for validation, settling, and observation.
CLICK_TIMEOUT_MILLISECONDS = 5_000
# Headed Chromium lacks headless shell's new-window switch. A response policy
# denies auxiliary browsing contexts while retaining normal sign-in capabilities.
_POPUP_DENIAL_POLICY = (
    "sandbox allow-downloads allow-forms allow-modals allow-orientation-lock "
    "allow-pointer-lock allow-presentation allow-same-origin allow-scripts "
    "allow-storage-access-by-user-activation allow-top-navigation"
)
# Full Chromium asks its vendor's services for things no page requested. Each
# feature below is the switch for one of them (ADR-0146).
_VENDOR_REQUEST_FEATURES = (
    "AimEnabled",  # AI Mode eligibility, from www.google.com
    "AutofillServerCommunication",  # field types, from the forms on each page
    "NetworkTimeServiceQuerying",  # the time, from clients2.google.com
    "PreconnectToSearch",  # an idle connection to www.google.com
)
# Chromium honours only its last --disable-features switch, and Playwright
# passes one of its own first, so the runtime's switch repeats Playwright's
# list (chromiumSwitches.ts). A test compares it with the installed driver.
_PLAYWRIGHT_DISABLED_FEATURES = (
    "AvoidUnnecessaryBeforeUnloadCheckSync",
    "BoundaryEventDispatchTracksNodeRemoval",
    "DestroyProfileOnBrowserClose",
    "DialMediaRouteProvider",
    "GlobalMediaControls",
    "HttpsUpgrades",
    "LensOverlay",
    "MediaRouter",
    "PaintHolding",
    "ThirdPartyStoragePartitioning",
    "BlockOriginHeaderModificationOnRedirect",
    "Translate",
    "AutoDeElevate",
    "OptimizationHints",
    "msForceBrowserSignIn",
    "msEdgeUpdateLaunchServicesPreferredVersion",
)
_VENDOR_REQUEST_ARGUMENTS = (
    "--disable-features=" + ",".join((*_PLAYWRIGHT_DISABLED_FEATURES, *_VENDOR_REQUEST_FEATURES)),
    # Chromium has no switch that turns its push-messaging client off. The
    # client registers and connects only after a check-in, and it cannot
    # fetch this address.
    "--gcm-checkin-url=about:blank",
)
_QUIET_SCRIPT = """([quietMs, timeoutMs]) => new Promise(resolve => {
    let quiet = 0;
    let limit = 0;
    const observer = new MutationObserver(() => {
        clearTimeout(quiet);
        quiet = setTimeout(() => finish(true), quietMs);
    });
    function finish(quietReached) {
        observer.disconnect();
        clearTimeout(quiet);
        clearTimeout(limit);
        resolve(quietReached);
    }
    observer.observe(document, {
        subtree: true, childList: true, attributes: true, characterData: true
    });
    quiet = setTimeout(() => finish(true), quietMs);
    limit = setTimeout(() => finish(false), timeoutMs);
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


async def _capture_regions(
    page: Page, root: ElementHandle | None
) -> tuple[dict[str, Any], list[ElementHandle]]:
    capture = await page.evaluate_handle(REGION_CAPTURE_SCRIPT, root)
    nodes = None
    handles: list[ElementHandle] = []
    try:
        structure = await capture.evaluate(
            "value => ({regions: value.regions, coverage: value.coverage})"
        )
        nodes = await capture.get_property("nodes")
        for _, handle in sorted(
            (await nodes.get_properties()).items(), key=lambda item: int(item[0])
        ):
            element = handle.as_element()
            if element is None:
                await handle.dispose()
                raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
            handles.append(element)
        return structure, handles
    except BaseException:
        await _dispose(handles, keep=())
        raise
    finally:
        if nodes is not None:
            await nodes.dispose()
        await capture.dispose()


class _ReadinessRequests:
    """Wait only for a snapshot of finite requests; never read response bodies."""

    def __init__(self) -> None:
        self._pending: set[Request] = set()
        self._changed = asyncio.Event()
        self._overflow = False

    def reset(self) -> None:
        """A new document or a closed runtime cannot retain the old request set."""
        self._pending.clear()
        self._overflow = False
        self._changed.set()

    def began(self, request: Request) -> None:
        if request.resource_type in {"document", "script", "stylesheet", "xhr", "fetch"}:
            if len(self._pending) >= 256:
                self._overflow = True
            else:
                self._pending.add(request)

    def ended(self, request: Request) -> None:
        self._pending.discard(request)
        self._changed.set()

    def responded(self, response: Response) -> None:
        request = response.request
        if (
            request in self._pending
            and request.resource_type in {"xhr", "fetch"}
            and response.status == 200
            and response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            == "text/event-stream"
        ):
            self.ended(request)

    async def drain(self, timeout: float) -> bool:
        """Drain the initial set within the caller's shared deadline."""
        if self._overflow:
            return False
        initial = self._pending.copy()
        try:
            async with asyncio.timeout(timeout):
                while initial & self._pending:
                    self._changed.clear()
                    await self._changed.wait()
        except TimeoutError:
            return False
        return True


class PythonPlaywrightRuntime:
    """Own one Chromium process, headed for every hosted use, and one non-persistent context."""

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
        self._headed_popup_policy = False
        self._revision: str | None = None
        self._elements: dict[str, ElementHandle] = {}
        self._regions: dict[str, ElementHandle] = {}
        self._focus_root: ElementHandle | None = None
        self._focus_text: str | None = None
        self._text_cursor: str | None = None
        self._facts: BrowserObservationFacts | None = None
        self._element_offsets: dict[str, int] = {}
        self._continuation: tuple[str, int] | None = None
        self._observation_document = 0
        self._known_draft: tuple[JSHandle, str, int] | None = None
        self._readiness_requests = _ReadinessRequests()
        self._disallowed_navigation = False
        self._dismissed_beforeunload = False
        self._document_session: CDPSession | None = None
        self._main_frame_id: str | None = None
        self._sign_in_entered = False
        self._main_frame_navigations = 0
        # While a task-grant act runs and settles, the origin and path prefix
        # every document load and ping must stay inside, and whether the fence
        # refused the page's own document (ADR-0129).
        self._document_fence: tuple[str, str] | None = None
        self._fence_refused_page = False

    @browser_phase_call("launch")
    async def start(
        self,
        proxy_url: str,
        allowed_origins: tuple[str, ...],
        *,
        storage_state: dict[str, object] | None = None,
        headed: bool = False,
    ) -> None:
        """Launch the isolated browser context with origin interception and audited egress."""
        if self._browser is not None:
            return
        self._allowed_origins = allowed_origins
        self._headed_popup_policy = headed
        self._temporary_home = tempfile.TemporaryDirectory(prefix="veetbot-browser-")
        temporary_home = self._temporary_home.name
        environment: dict[str, str | float | bool] = {
            "HOME": temporary_home,
            "PATH": os.defpath,
            "TMPDIR": temporary_home,
        }
        # Websites refuse a browser that reports itself headless, at login and
        # at any page, so a hosted browser is headed and never falls back
        # (ADR-0106, ADR-0145).
        if headed:
            display_name = await self._start_virtual_display()
            if display_name is not None:
                environment["DISPLAY"] = display_name
        self._playwright = await async_playwright().start()
        await self._playwright.selectors.register(
            "veetbot_controls", script=_CONTROL_SELECTOR, content_script=True
        )
        self._browser = await self._playwright.chromium.launch(
            headless=not headed,
            proxy={"server": proxy_url},
            args=[
                "--proxy-bypass-list=<-loopback>",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                # Headless shell can deny web-created windows before they exist.
                # Closing them later can release a pending navigation request.
                # It makes no vendor requests; full Chromium does.
                *(_VENDOR_REQUEST_ARGUMENTS if headed else ["--block-new-web-contents"]),
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
        fenced_out = not self._inside_document_fence(url)
        allowed = (
            bool(frame_id)
            and is_public_https_url(url)
            and (not main_frame or _origin_allowed(url, self._allowed_origins))
            and not fenced_out
        )
        if not allowed and main_frame:
            self._disallowed_navigation = True
            self._fence_refused_page = self._fence_refused_page or fenced_out
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
        page.on("request", self._readiness_requests.began)
        page.on("response", self._readiness_requests.responded)
        page.on("requestfinished", self._readiness_requests.ended)
        page.on("requestfailed", self._readiness_requests.ended)
        page.on("framenavigated", self._count_main_frame_navigation)
        self._page = page

    def _count_main_frame_navigation(self, frame: Frame) -> None:
        """Count committed main-frame documents so an action knows it loaded a new one."""
        if frame.parent_frame is None:
            self._main_frame_navigations += 1
            self._readiness_requests.reset()

    def _track_navigation(self, request: Request) -> None:
        """Remember refused navigation origins for stable browser failure classification."""
        if not request.is_navigation_request():
            return
        frame = _request_frame(request)
        if (frame is None or frame.parent_frame is None) and not _origin_allowed(
            request.url, self._allowed_origins
        ):
            self._disallowed_navigation = True

    async def _route(self, route: Route) -> None:
        """Enforce navigation boundaries before dispatch and sandbox headed documents."""
        request = route.request
        allowed = is_public_https_url(request.url)
        navigation = request.is_navigation_request()
        # A new window's first navigation is issued before its frame exists, so
        # it has no frame; it is top-level and never this runtime's page.
        frame = _request_frame(request) if navigation else None
        top_level = navigation and (frame is None or frame.parent_frame is None)
        own_page = (
            top_level and frame is not None and (self._page is None or frame.page is self._page)
        )
        if top_level:
            allowed = allowed and _origin_allowed(request.url, self._allowed_origins)
            if not own_page:
                allowed = False
        fenced = self._document_fence is not None and (
            navigation or request.resource_type == "ping"
        )
        if fenced and not self._inside_document_fence(request.url):
            allowed = False
            if own_page:
                self._fence_refused_page = True
        if allowed and navigation and self._headed_popup_policy:
            await self._route_headed_document(route)
        elif allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def _route_headed_document(self, route: Route) -> None:
        """Preserve one document response through the same proxy, adding popup denial."""
        try:
            request_headers = await route.request.all_headers()
            # Keep Chromium's cookie decision, including an intentionally empty
            # selection. The API request jar otherwise adds same-site cookies.
            request_headers.setdefault("cookie", "")
            response = await route.fetch(headers=request_headers, max_redirects=0, max_retries=0)
        except PlaywrightError:
            await route.abort("failed")
            return
        try:
            headers = response.headers
            existing = headers.get("content-security-policy")
            headers["content-security-policy"] = (
                f"{existing}, {_POPUP_DENIAL_POLICY}" if existing else _POPUP_DENIAL_POLICY
            )
            await route.fulfill(response=response, headers=headers)
        finally:
            with suppress(PlaywrightError):
                await response.dispose()

    def _inside_document_fence(self, url: str) -> bool:
        """Whether a document or ping may be requested at ``url`` now (ADR-0129).

        Outside a task-grant act there is no fence. During one, a document in
        any frame, or a hyperlink-auditing ping, goes only to the grant's
        origin, inside its path prefix, with no sensitive segment: whatever
        the page hid from the facts, the act cannot submit or navigate
        anywhere else.
        """
        fence = self._document_fence
        if fence is None:
            return True
        origin, path_prefix = fence
        if _origin_or_none(url) != origin:
            return False
        path = urlsplit(url).path
        return path_is_within_prefix(path, path_prefix) and not path_is_sensitive(path)

    async def _dismiss_dialog(self, dialog: Dialog) -> None:
        """Dismiss browser dialogs while recording unsaved-draft navigation cancellation."""
        if dialog.type == "beforeunload":
            self._dismissed_beforeunload = True
        await dialog.dismiss()

    async def _cancel_download(self, download: Download) -> None:
        await download.cancel()

    async def _close_popup(self, page: Page) -> None:
        """Destroy a popup with interception intact, or end its browser session."""
        if page is self._page or page.is_closed():
            return
        # Page.close marks the page as closing before Chromium closes it. That
        # makes Playwright skip our context route, which can release a popup's
        # pending form submission. Close the target directly so interception
        # stays active until Chromium has destroyed the popup.
        session: CDPSession | None = None
        try:
            session = await page.context.new_cdp_session(page)
            target = await session.send("Target.getTargetInfo")
            await session.send("Target.closeTarget", {"targetId": target["targetInfo"]["targetId"]})
            if not page.is_closed():
                await page.wait_for_event("close", timeout=5_000)
        except asyncio.CancelledError:
            if not page.is_closed():
                await self.close()
            raise
        except Exception:
            if not page.is_closed():
                await self.close()
                raise
        finally:
            if session is not None:
                with suppress(PlaywrightError):
                    await session.detach()

    def _current_page(self) -> Page:
        if self._page is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return self._page

    @browser_phase_call("navigation")
    async def navigate(self, url: str) -> BrowserObservation:
        """Navigate within the bound origin policy and preserve stable failure codes."""
        page = self._current_page()
        self._disallowed_navigation = False
        self._dismissed_beforeunload = False
        previous_url = page.url
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightError as exc:
            if self._disallowed_navigation:
                raise BrowserProviderError("tool.browser.url_disallowed", retryable=False) from exc
            if (
                self._dismissed_beforeunload
                and "net::ERR_ABORTED" in str(exc)
                and not page.is_closed()
                and page.url == previous_url
                and _origin_allowed(page.url, self._allowed_origins)
            ):
                # Dismissing an unsaved-change dialog cancels goto, not the
                # connection. Keep the draft and its lease; never accept or retry.
                raise BrowserProviderError(
                    "tool.browser.navigation_cancelled", retryable=False
                ) from exc
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=True,
            ) from exc
        if not _origin_allowed(page.url, self._allowed_origins):
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        readiness = await self._settle(page, after_document=True)
        if not _origin_allowed(page.url, self._allowed_origins):
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        observation = await self._observation(page)
        return observation.model_copy(update={"readiness": readiness})

    async def _settle(
        self,
        page: Page,
        *,
        after_document: bool,
    ) -> Literal["dom_quiet", "bound_expired"]:
        with browser_phase("readiness") as phase:
            quiet = await self._settle_page(page, after_document=after_document)
            if not quiet:
                phase.outcome = "bound_expired"
            return "dom_quiet" if quiet else "bound_expired"

    async def _settle_page(self, page: Page, *, after_document: bool) -> bool:
        """ADR-0159: bounded DOM quiet; background network traffic need not stop."""
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
            await load_state("domcontentloaded")
        if not await self._readiness_requests.drain(remaining()):
            return False
        for attempt in range(2):
            left = remaining()
            if left <= 0:
                return False
            try:
                async with asyncio.timeout(left + 0.25):
                    quiet = await page.evaluate(
                        _QUIET_SCRIPT, [DOM_QUIET_MILLISECONDS, max(1, int(left * 1000))]
                    )
                return quiet is True
            except TimeoutError:
                return False
            except PlaywrightError:
                if attempt:
                    return False
                await load_state("domcontentloaded")

        return False

    def facts(self, revision: str) -> BrowserObservationFacts | None:
        """The element facts of ``revision``, while it is the current observation."""
        if self._facts is None or self._facts.revision != revision:
            return None
        return self._facts

    async def load_page_evidence(
        self,
        url: str,
        *,
        on_stage: Callable[[BrowserVerificationStage], None] = ignore_verification_stage,
    ) -> BrowserPageEvidence:
        """Load one page and report where it landed and whether it asks to sign in.

        ADR-0128 verifies a device handoff by loading the page the owner
        confirmed with and without the session. A navigation the origin guard
        refuses is evidence (the page left the allowed origins), not a failure;
        any other load failure is ``provider_unavailable``. Nothing from the
        page or Playwright's message leaves this method but the evidence.

        ``on_stage`` hears each stage as it begins. The service's deadline
        arrives here as a cancellation, which cannot say where it landed, so
        the stage reported last is where an unfinished load stopped.
        """
        page = self._current_page()
        self._disallowed_navigation = False
        with _verification_requests(page) as wait_for_application:
            on_stage(BrowserVerificationStage.NAVIGATE)
            try:
                # A full "load" also waits for passive images and subframes.
                # Those must not spend the whole verification budget, but the
                # document and requests that can decide sign-in must finish.
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightError as exc:
                if self._disallowed_navigation:
                    return BrowserPageEvidence(
                        on_allowed_origin=False, path="/", challenge_visible=False
                    )
                raise BrowserProviderError(
                    "tool.browser.provider_unavailable",
                    retryable=True,
                ) from exc
            on_stage(BrowserVerificationStage.IDLE)
            with suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=5_000)
            try:
                on_stage(BrowserVerificationStage.INSPECT)
                challenge_visible = await self._sign_in_challenge_visible(page)
                confirmed_path = (urlsplit(url).path or "/").removesuffix("/") or "/"
                current_path = (urlsplit(page.url).path or "/").removesuffix("/") or "/"
                if (
                    _origin_allowed(page.url, self._allowed_origins)
                    and (
                        current_path == confirmed_path
                        or current_path.startswith(confirmed_path + "/")
                    )
                    and not challenge_visible
                ):
                    # A challenge or a different path is already negative
                    # evidence. Background work on that page must not hold
                    # the signed-out control open. A possible positive result
                    # must still wait for its application's session decision.
                    on_stage(BrowserVerificationStage.SETTLE)
                    await wait_for_application()
                    on_stage(BrowserVerificationStage.REINSPECT)
                    challenge_visible = await self._sign_in_challenge_visible(page)
            except (PlaywrightError, TimeoutError) as exc:
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

    async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
        page = self._current_page()
        if not _origin_allowed(page.url, self._allowed_origins):
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        if self._observation_document != self._main_frame_navigations:
            await self._forget_observation()
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        if request.region_ref is not None:
            root = self._regions.get(request.region_ref)
            if request.expected_revision != self._revision or root is None:
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            prior_text = None
            if request.text_offset:
                if self._focus_root is None or not await root.evaluate(
                    "(node, previous) => node === previous", self._focus_root
                ):
                    raise BrowserProviderError("tool.browser.page_changed", retryable=False)
                prior_text = self._focus_text
            return await self._observation(
                page, root=root, text_offset=request.text_offset, prior_text=prior_text
            )
        if self._text_cursor is not None and request.cursor is not None:
            token, separator, value = request.cursor.partition(".")
            if token == self._text_cursor and separator and value.isascii() and value.isdecimal():
                text_offset = int(value)
                if 0 < text_offset <= 262_144 and self._focus_root is not None:
                    return await self._observation(
                        page,
                        root=self._focus_root,
                        text_offset=text_offset,
                        prior_text=self._focus_text,
                    )
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        if request.after is not None:
            handle = self._elements.get(request.after)
            offset = self._element_offsets.get(request.after)
            if handle is None or offset is None:
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            return await self._observation(
                page, offset=offset, anchor=handle, root=self._focus_root
            )
        if self._continuation is None or request.cursor != self._continuation[0]:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        return await self._observation(page, offset=self._continuation[1], root=self._focus_root)

    async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
        page = self._current_page()
        if not _origin_allowed(page.url, self._allowed_origins):
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        if (
            self._revision != request.expected_revision
            or self._observation_document != self._main_frame_navigations
        ):
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        return await self._observation(page, extraction=request)

    @browser_phase_call("observation")
    async def _observation(
        self,
        page: Page,
        *,
        offset: int = 0,
        anchor: ElementHandle | None = None,
        extraction: BrowserExtractionRequest | None = None,
        root: ElementHandle | None = None,
        text_offset: int = 0,
        prior_text: str | None = None,
    ) -> BrowserObservation:
        revision = secrets.token_hex(16)
        found: list[ElementHandle] = []
        captures: list[asyncio.Task[tuple[BrowserElement, BrowserElementFacts] | None]] = []
        region_handles: list[ElementHandle] = []
        try:
            if root is not None and not await root.evaluate("""node => {
                if (!node.isConnected || node.ownerDocument !== document) return false;
                for (let p = node, count = 0; p; p = p.parentElement) {
                    if (++count > 8192 || p.hidden || p.getAttribute('aria-hidden') === 'true'
                        || p.isContentEditable) return false;
                    const style = getComputedStyle(p);
                    if (style.display === 'none' || style.visibility !== 'visible'
                        || style.opacity === '0') return false;
                }
                return true;
            }"""):
                await self._forget_observation()
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            document = self._main_frame_navigations
            readable = await page.evaluate(READABLE_TEXT_SCRIPT, root)
            known = await self._confirmed_draft(root)
            if known and len((known + readable["text"]).encode("utf-8")) <= 262_144:
                readable["text"] = known + readable["text"]
            full_text = readable["text"]
            if prior_text is not None and full_text != prior_text:
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            encoded = full_text.encode("utf-8")
            try:
                if text_offset > len(encoded):
                    raise ValueError("offset outside capture")
                readable["text"] = encoded[text_offset:].decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise BrowserProviderError("tool.browser.page_changed", retryable=False) from exc
            if root is None:
                found = await page.locator(f"veetbot_controls={offset}").element_handles()
            else:
                found = await root.query_selector_all(f"veetbot_controls={offset}")
            start = 0
            if anchor is not None:
                if not found or not await page.evaluate(
                    "([anchor, current]) => anchor === current && anchor.isConnected",
                    [anchor, found[0]],
                ):
                    raise BrowserProviderError("tool.browser.page_changed", retryable=False)
                start = 1
            stop = min(MAXIMUM_SCANNED_ELEMENTS, MAXIMUM_CANDIDATE_OFFSET - offset)
            candidates = found[start:stop]
            candidate_offset = offset + start
            # Hidden controls never take an element slot (ADR-0130 decision 8).
            flags = await page.evaluate(_VISIBLE_SCRIPT, candidates)
            selected = [
                (candidate_offset + index, handle)
                for index, (handle, visible) in enumerate(zip(candidates, flags, strict=True))
                if visible
            ][:MAXIMUM_ELEMENTS]
            snapshot = [handle for _, handle in selected]
            next_offset = (
                selected[-1][0] + 1
                if len(selected) == MAXIMUM_ELEMENTS
                else candidate_offset + len(candidates)
            )
            has_more = next_offset < offset + len(found)
            scan_limit = has_more and next_offset >= MAXIMUM_CANDIDATE_OFFSET
            continuation = (
                (secrets.token_hex(16), next_offset) if has_more and not scan_limit else None
            )
            slots = asyncio.Semaphore(8)
            page_url = page.url

            async def capture(
                index: int, handle: ElementHandle
            ) -> tuple[BrowserElement, BrowserElementFacts] | None:
                async with slots:
                    return await self._element_observation(
                        handle, f"{revision}:{index}", page_url=page_url
                    )

            captures = [
                asyncio.create_task(capture(index, handle)) for index, handle in enumerate(snapshot)
            ]
            captured = await asyncio.gather(*captures)
            structure, region_handles = await _capture_regions(page, root)
            extracted = None
            if extraction is not None:
                raw = await page.evaluate(EXTRACTION_SCRIPT, extraction.model_dump(mode="json"))
                extracted = extracted_observation(raw, extraction, revision)
            elements: list[BrowserElement] = []
            handles: dict[str, ElementHandle] = {}
            facts: dict[str, BrowserElementFacts] = {}
            for handle, observed in zip(snapshot, captured, strict=True):
                if observed is not None:
                    element, element_facts = observed
                    elements.append(element)
                    handles[element.ref] = handle
                    facts[element.ref] = element_facts
            observation = BrowserObservation(
                url=page.url,
                title=await page.title(),
                revision=revision,
                text=readable["text"],
                text_coverage=readable["coverage"],
                focus=(
                    BrowserObservationFocus(
                        region_ref=f"{revision}:focus",
                        text_offset=text_offset,
                        text_total_bytes=len(encoded),
                        text_cursor=secrets.token_hex(16),
                    )
                    if root is not None
                    else None
                ),
                elements=tuple(elements),
                regions=tuple(
                    BrowserSemanticRegion.model_validate(
                        {**region, "ref": f"{revision}:region:{index}"}
                    )
                    for index, region in enumerate(structure["regions"])
                ),
                region_coverage=BrowserRegionCoverage.model_validate(structure["coverage"]),
                extraction=extracted,
                coverage=BrowserObservationCoverage(
                    candidate_offset=candidate_offset,
                    scanned_candidates=len(candidates),
                    next_cursor=None if continuation is None else continuation[0],
                    scan_limit_reached=scan_limit,
                ),
            )
            observation_facts = BrowserObservationFacts(revision=revision, elements=facts)
            regions = {f"{revision}:region:{i}": h for i, h in enumerate(region_handles)}
            if root is not None:
                regions[f"{revision}:focus"] = root
            await _dispose(
                [*self._elements.values(), *self._regions.values(), *found],
                keep=[*handles.values(), *regions.values()],
            )
            if document != self._main_frame_navigations:
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        except BaseException:
            # gather does not cancel sibling work on an ordinary capture error.
            # Join it before disposing its handles, including on cancellation.
            for task in captures:
                task.cancel()
            await asyncio.gather(*captures, return_exceptions=True)
            released = [*self._elements.values(), *self._regions.values(), *found, *region_handles]
            self._revision = None
            self._elements = {}
            self._regions = {}
            self._focus_root = None
            self._focus_text = None
            self._text_cursor = None
            self._facts = None
            self._element_offsets = {}
            self._continuation = None
            await _dispose(released, keep=())
            raise
        self._revision = revision
        self._regions = regions
        self._focus_root = root
        self._focus_text = full_text if root is not None else None
        self._text_cursor = observation.focus.text_cursor if observation.focus is not None else None
        self._elements = handles
        self._facts = observation_facts
        self._element_offsets = {
            element.ref: candidate_index
            for (candidate_index, _), observed in zip(selected, captured, strict=True)
            if observed is not None
            for element, _ in [observed]
        }
        self._continuation = continuation
        self._observation_document = document
        return observation

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
        try:
            if tag == "input" and input_type in {"checkbox", "radio"}:
                checked = await handle.is_checked()
            disabled = await handle.is_disabled()
        except PlaywrightError:
            # A re-rendering page can remove the node after its visibility
            # check; like a hidden one, it takes no slot. Any other failure
            # still fails the observation.
            if await handle.evaluate("node => node.isConnected"):
                raise
            return None
        role = metadata["role"] or _default_role(tag, input_type)
        element = BrowserElement(
            ref=ref,
            role=role,
            name=str(metadata["name"])[:1024],
            disabled=disabled,
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
        # The observation offered only visible elements and reported each
        # one's disabled state, so the worker covers neither case (ADR-0129).
        if not await handle.is_visible() or not await handle.is_enabled():
            raise BrowserProviderError("tool.browser.element_not_found", retryable=False)

        tag = str(await handle.evaluate("node => node.tagName.toLowerCase()"))
        input_type = (await handle.get_attribute("type") or "").lower()
        autocomplete = (await handle.get_attribute("autocomplete") or "").lower()
        # Editable regions need individual approval; grant coverage stays native-only.
        approved_editable = (
            action.kind is BrowserActionKind.TYPE
            and constraint is None
            and tag not in {"input", "textarea"}
            and await handle.evaluate("node => node.isContentEditable") is True
        )
        if action.kind is BrowserActionKind.TYPE and (
            (tag not in {"input", "textarea"} and not approved_editable)
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

        guards: list[JSHandle] = []
        if constraint is not None:
            assert now is not None
            guards.append(
                await self._require_live_coverage(page, handle, action, constraint, now=now)
            )
            if action.kind in _KEYBOARD_KINDS:
                try:
                    guards.append(await self._hold_focus(handle))
                except BaseException:
                    await _release_guards(guards)
                    raise

        documents_before = self._main_frame_navigations
        # A refusal belongs to the act whose fence made it: every act starts
        # without one, and one that ends early, by an error or cancellation,
        # takes its refusal with it.
        self._fence_refused_page = False
        if constraint is not None and constraint.path_prefix is not None:
            self._document_fence = (constraint.origins[0], constraint.path_prefix)
        draft_binding = None
        if approved_editable:
            # Chromium can split inherited editable descendants while typing newlines.
            # Bind the existing editing host before dispatch, without reading its value.
            draft_binding = await handle.evaluate_handle("""node => {
                let count = 0;
                while (node.parentElement?.isContentEditable) {
                    if (++count > 256) return null;
                    node = node.parentElement;
                }
                return new WeakRef(node);
            }""")
        try:
            await self._dispatch(page, handle, action, guards)
            if draft_binding is not None and self._main_frame_navigations == documents_before:
                await self._forget_draft()
                self._known_draft = (draft_binding, action.value or "", documents_before)
                draft_binding = None
            # The action was sent; settling never turns it into a failure (ADR-0130).
            readiness = await self._settle(
                page, after_document=self._main_frame_navigations != documents_before
            )
        finally:
            if draft_binding is not None:
                with suppress(PlaywrightError):
                    await draft_binding.dispose()
            self._document_fence = None
            refused_page = self._fence_refused_page
            self._fence_refused_page = False
        if refused_page:
            # The action sent the page toward a document outside the grant,
            # which never loaded; the page now shows the browser's error page.
            await self._forget_observation()
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
        observation = await self._observation(page)
        return observation.model_copy(update={"readiness": readiness})

    async def upload(self, action: BrowserAction, image: BrowserImageFile) -> BrowserObservation:
        """Select approved bytes in one main-document file control (ADR-0148)."""
        page = self._current_page()
        if action.expected_revision != self._revision:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        handle = self._elements.get(action.ref)
        if handle is None or not await handle.is_visible() or not await handle.is_enabled():
            raise BrowserProviderError("tool.browser.element_not_found", retryable=False)
        if action.kind is not BrowserActionKind.CLICK or not _origin_allowed(
            page.url, self._allowed_origins
        ):
            raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
        if await handle.owner_frame() != page.main_frame:
            raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
        payload: FilePayload = {
            "name": image.filename,
            "mimeType": image.image.media_type,
            "buffer": image.image.data,
        }
        documents_before = self._main_frame_navigations
        try:
            native_file = await handle.evaluate(
                "node => node.localName === 'input' && node.type === 'file'"
            )
            if native_file:
                await handle.set_input_files(payload, timeout=ACTION_TIMEOUT_MILLISECONDS)
            else:
                async with page.expect_file_chooser(timeout=ACTION_TIMEOUT_MILLISECONDS) as pending:
                    await handle.click(timeout=ACTION_TIMEOUT_MILLISECONDS)
                chooser = await pending.value
                # The approved click may have navigated or opened an embedded chooser.
                if (
                    self._main_frame_navigations != documents_before
                    or not _origin_allowed(page.url, self._allowed_origins)
                    or await chooser.element.owner_frame() != page.main_frame
                ):
                    raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
                await chooser.set_files(payload, timeout=ACTION_TIMEOUT_MILLISECONDS)
        except PlaywrightError as exc:
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False) from exc
        await self._settle(page, after_document=self._main_frame_navigations != documents_before)
        return await self._observation(page)

    @browser_phase_call("dispatch")
    async def _dispatch(
        self,
        page: Page,
        handle: ElementHandle,
        action: BrowserAction,
        guards: list[JSHandle],
    ) -> None:
        """Check click actionability, then send one action without automatic replay.

        ``guards`` are a grant-constrained act's click guard and, for a key or
        text, its focus guard, released once the action is sent. Only a trial
        click timeout is a definite refusal; dispatch failures remain uncertain.
        """
        try:
            if action.kind is BrowserActionKind.CLICK:
                try:
                    # Visibility alone includes controls covered by a modal. Trial
                    # performs actionability checks without dispatching the click.
                    await handle.click(trial=True, timeout=CLICK_TIMEOUT_MILLISECONDS)
                except PlaywrightTimeoutError as exc:
                    raise BrowserProviderError(
                        "tool.browser.element_not_found", retryable=False
                    ) from exc
                await handle.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
            elif action.kind is BrowserActionKind.TYPE:
                await handle.fill(action.value or "", timeout=ACTION_TIMEOUT_MILLISECONDS)
            elif action.kind is BrowserActionKind.SELECT:
                await handle.select_option(action.value or "", timeout=ACTION_TIMEOUT_MILLISECONDS)
            elif action.kind is BrowserActionKind.CHECK:
                await handle.check(timeout=ACTION_TIMEOUT_MILLISECONDS)
            elif action.kind is BrowserActionKind.PRESS:
                key = action.key.value if action.key is not None else ""
                if not guards:
                    await handle.press(key, timeout=ACTION_TIMEOUT_MILLISECONDS)
                else:
                    # Under a constraint the focus guard focused the element and
                    # verified it holds focus; the keyboard sends to it.
                    await page.keyboard.press(key)
            else:
                await handle.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MILLISECONDS)
                await page.mouse.wheel(0, action.delta_y or 0)
        except PlaywrightError as exc:
            await _release_guards(guards)
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False) from exc
        except BaseException:
            # Cancelled while Playwright waits for the element, or failed any
            # other way: a guard left on the page would stop the next act's
            # input. The release is shielded so a second cancellation cannot
            # cut it short.
            if guards:
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.shield(_release_guards(guards))
            raise
        if await _release_guards(guards):
            # A click, key or text went to another element and was stopped
            # there; what the page's own listeners did with it is unknown.
            await self._forget_observation()
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)

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
    ) -> JSHandle:
        """Recheck a grant-authorized act against the live page before dispatch (ADR-0129).

        The worker decided from the observation it was shown; this reads the
        live URL, every live label source at full length and live facts, and
        runs the same coverage rules. A constraint can only narrow. The read
        arms the click guard, which is returned armed for dispatch to release.
        """
        guard: JSHandle | None = None
        try:
            guard = await handle.evaluate_handle(_GUARDED_READ_SCRIPT)
            metadata = await guard.evaluate("guard => guard.metadata")
            covered = await self._live_coverage(page, handle, action, constraint, metadata, now=now)
        except PlaywrightError:
            covered = False
        except BaseException:
            await _release_guards([guard] if guard is not None else [])
            raise
        if not covered or guard is None:
            await _release_guards([guard] if guard is not None else [])
            await self._refuse_grant()
        return guard

    async def _live_coverage(
        self,
        page: Page,
        handle: ElementHandle,
        action: BrowserAction,
        constraint: BrowserDispatchConstraint,
        metadata: dict[str, Any],
        *,
        now: datetime,
    ) -> bool:
        if metadata.get("overlong") is not False:
            # A label source past what the live check reads could hold an
            # excluded word in its unread tail.
            return False
        option_texts = await self._live_option_texts(handle, action)
        if option_texts is None:
            return False
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
            option_texts=option_texts,
            runtime_origins=self._allowed_origins,
            now=now,
            # ``act`` refused a disabled element before this check.
            disabled=False,
        )
        return coverage.covered

    async def _live_option_texts(
        self, handle: ElementHandle, action: BrowserAction
    ) -> list[str] | None:
        """For a selection, every label of every option it could choose.

        Playwright chooses the first option whose value or label, with white
        space collapsed, is the requested text, among all the options. Every
        option that matches by any of those, or by its text, is read, with its
        label, value, text, accessible name, title and its group's label. A
        select with too many options, or too many that match, to read whole
        has none that can be read (None), and is refused.
        """
        if action.kind is not BrowserActionKind.SELECT or action.value is None:
            return []
        texts = await handle.evaluate(_OPTION_SCRIPT, action.value)
        if not isinstance(texts, list):
            return None
        return [action.value, *(text for text in texts if isinstance(text, str))]

    async def _refuse_grant(self) -> NoReturn:
        """Refuse before dispatch and forget the observation (ADR-0129 D16).

        The model must observe again, so a stale reference can neither burn
        grant uses nor, once the owner approves it, act on a changed element.
        """
        await self._forget_observation()
        raise BrowserProviderError("tool.browser.grant_not_applicable", retryable=False)

    async def _forget_observation(self) -> None:
        released = [*self._elements.values(), *self._regions.values()]
        self._regions = {}
        self._focus_root = None
        self._focus_text = None
        self._text_cursor = None
        self._revision = None
        self._elements = {}
        self._facts = None
        self._element_offsets = {}
        self._continuation = None
        await _dispose(released, keep=())

    async def _confirmed_draft(self, root: ElementHandle | None) -> str:
        """Echo our last approved draft only while its bounded visible editor still matches."""
        if self._known_draft is None:
            return ""
        weak, submitted, document = self._known_draft
        if document == self._main_frame_navigations:
            with suppress(PlaywrightError):
                if await weak.evaluate(CONFIRM_DRAFT_SCRIPT, [submitted, root]) is True:
                    return "Confirmed submitted draft:\n" + submitted + "\n"
        # A changed document or value cannot later resurrect an old draft receipt.
        await self._forget_draft()
        return ""

    async def _forget_draft(self) -> None:
        if self._known_draft is not None:
            weak, _submitted, _document = self._known_draft
            self._known_draft = None
            with suppress(PlaywrightError):
                await weak.dispose()

    async def storage_state(self) -> dict[str, object]:
        if self._context is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return cast(dict[str, object], await self._context.storage_state(indexed_db=True))

    async def check_automation_ready(self) -> None:
        if await self._current_page().evaluate(AUTHENTICATION_SCRIPT):
            await self._forget_observation()
            raise BrowserProviderError("tool.browser.needs_user", retryable=False)

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

    @browser_phase_call("cleanup")
    async def close(self) -> None:
        """Release the browser and proxy resources owned by this session."""
        try:
            await self._forget_draft()
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
            self._regions = {}
            self._focus_root = None
            self._focus_text = None
            self._text_cursor = None
            self._facts = None
            self._element_offsets = {}
            self._continuation = None
            self._disallowed_navigation = False
            self._headed_popup_policy = False
            self._document_session = None
            self._main_frame_id = None
            self._sign_in_entered = False
            self._main_frame_navigations = 0
            self._readiness_requests.reset()
            self._document_fence = None
            self._fence_refused_page = False


@contextmanager
def _verification_requests(page: Page) -> Iterator[Callable[[], Awaitable[None]]]:
    """Keep application requests in the verification budget, even behind a slow image.

    The five-second generic network wait may expire on passive resources.
    That is not permission to accept a shell whose session-check fetch is
    still in flight. Track documents (including frames), scripts, styles and
    finite application requests until none remain for 500 ms. An HTTP 200
    event stream is established at its headers, as with native EventSource;
    its continuing body is not a pending session check (ADR-0147).
    The service's overall thirty-second deadline still bounds startup, both
    loads and capture.
    """
    pending: set[Request] = set()
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    last_change = loop.time()

    def began(request: Request) -> None:
        """Track finite-request candidates until completion or stream establishment."""
        nonlocal last_change
        if request.resource_type in {"document", "script", "stylesheet", "xhr", "fetch"}:
            pending.add(request)
            last_change = loop.time()
            changed.set()

    def ended(request: Request) -> None:
        """Restart the quiet interval when a tracked request finishes."""
        nonlocal last_change
        if request in pending:
            pending.remove(request)
            last_change = loop.time()
            changed.set()

    def responded(response: Response) -> None:
        """Recognize an established event stream without reading its continuing body."""
        request = response.request
        if (
            request in pending
            and request.resource_type in {"xhr", "fetch"}
            and response.status == 200
            and response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            == "text/event-stream"
        ):
            ended(request)

    async def wait_until_quiet() -> None:
        """Wait for finite requests to finish and remain quiet for 500 ms."""
        # Also bound direct callers; the service's enclosing deadline expires
        # sooner because it includes browser startup and document navigation.
        async with asyncio.timeout(30):
            while True:
                quiet_left = max(0.0, last_change + 0.5 - loop.time())
                if not pending and quiet_left == 0:
                    return
                changed.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(changed.wait(), None if pending else quiet_left)

    page.on("request", began)
    page.on("response", responded)
    page.on("requestfinished", ended)
    page.on("requestfailed", ended)
    try:
        yield wait_until_quiet
    finally:
        page.remove_listener("request", began)
        page.remove_listener("response", responded)
        page.remove_listener("requestfinished", ended)
        page.remove_listener("requestfailed", ended)
        pending.clear()


def _request_frame(request: Request) -> Frame | None:
    """The frame a request belongs to, or None while a new window's frame does
    not exist yet, when Playwright's accessor raises."""

    try:
        return request.frame
    except PlaywrightError:
        return None


async def _release_guards(guards: list[JSHandle]) -> bool:
    """Remove each guard; true when any stopped a click, key or text aimed elsewhere.

    A guard whose document is gone, because the action navigated, stopped
    nothing that could still act.
    """

    redirected = False
    for guard in guards:
        with suppress(PlaywrightError):
            redirected = bool(await guard.evaluate("guard => guard()")) or redirected
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


# One read of an element: its bounded label-derived name, and every
# live attribute its facts are derived from (ADR-0129 section 4.5). Facts follow
# the flat tree the browser renders and dispatches events through: a slotted
# node's parent is its slot, a shadow root's is its host, and a slot's children
# include what is assigned to it. A click lands on whatever lies at the
# element's centre, which may be a descendant, so descendants' targets count
# too. Past the walk's bounds, or with an embedded document, an image map or an
# SVG <use> whose copy could hold a link inside, the element is opaque: its
# targets cannot be listed. Beside the metadata it returns every
# element it read: the node, its ancestors and descendants, the controls of
# labels among them, and the default button of its form.
_READ_ELEMENT_SCRIPT = """node => {
    const collapse = value => Array.from(String(value || '').replace(/\\s+/g, ' ').trim());
    const clean = value => collapse(value).slice(0, 1024).join('');
    const byId = (element, id) => {
        const tree = element.getRootNode();
        return (tree.getElementById ? tree.getElementById(id) : null)
            || document.getElementById(id);
    };
    const referenced = (element, ids) => String(ids || '').split(/\\s+/).filter(Boolean)
        .slice(0, 8).map(id => {
            const target = byId(element, id);
            return target ? target.textContent : '';
        }).join(' ');
    const resolve = value => {
        try { return new URL(value, document.baseURI).href; } catch (error) { return ''; }
    };
    const parentOf = current => {
        if (current.assignedSlot) {
            return current.assignedSlot;
        }
        const parent = current.parentNode;
        if (parent && parent.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
            return parent.host || null;
        }
        return parent && parent.nodeType === Node.ELEMENT_NODE ? parent : null;
    };
    const childrenOf = current => [
        ...current.children,
        ...(current.shadowRoot ? current.shadowRoot.children : []),
        ...(typeof current.assignedElements === 'function'
            ? current.assignedElements({flatten: true}) : []),
    ];
    let opaque = false;
    const ancestors = [];
    let current = node;
    while (current) {
        if (ancestors.length === 256) {
            opaque = true;
            break;
        }
        ancestors.push(current);
        current = parentOf(current);
    }
    const descendants = [];
    const seen = new Set([node]);
    const pending = childrenOf(node);
    while (pending.length) {
        const next = pending.pop();
        if (seen.has(next)) {
            continue;
        }
        if (descendants.length === 2048) {
            opaque = true;
            break;
        }
        seen.add(next);
        descendants.push(next);
        pending.push(...childrenOf(next));
    }
    const reached = [node, ...descendants];
    const embedders = ['iframe', 'frame', 'object', 'embed'];
    // An SVG <use> renders a copy of what it references in a tree page script
    // cannot read. A copy that could hold a link or embedded content, or of
    // something in another document, is opaque; a plain icon is not.
    const copyUnsafe = [...embedders, 'a', 'use', 'foreignObject'];
    const useOpaque = use => [use.getAttribute('href'),
        use.getAttributeNS('http://www.w3.org/1999/xlink', 'href'),
        use.href instanceof SVGAnimatedString ? use.href.animVal : null]
        .filter(value => typeof value === 'string' && value.trim()).some(value => {
            const reference = value.trim();
            if (!reference.startsWith('#')) {
                return true;
            }
            let id = reference.slice(1);
            try { id = decodeURIComponent(id); } catch (error) { return true; }
            const tree = use.getRootNode();
            return [tree.getElementById ? tree.getElementById(id) : null,
                document.getElementById(id)].filter(Boolean).some(target => {
                    const copied = [target, ...target.querySelectorAll('*')];
                    return copied.length > 2048 || copied.some(element =>
                        copyUnsafe.includes(element.localName) || element.hasAttribute('usemap'));
                });
        });
    if (reached.some(element => embedders.includes(element.localName)
            || element.hasAttribute('usemap')
            || (element.localName === 'use' && useOpaque(element)))) {
        opaque = true;
    }
    const flat = [...ancestors, ...descendants];
    const anchor = element => ['a', 'area'].includes(element.localName);
    // An SVG link follows its animated value, which an animation can set away
    // from the attribute, so both are targets.
    const hrefsOf = element => {
        if (!anchor(element)) {
            return [];
        }
        const written = element.getAttribute('href')
            ?? element.getAttributeNS('http://www.w3.org/1999/xlink', 'href');
        const animated = element.href instanceof SVGAnimatedString ? element.href.animVal : null;
        return [...new Set([written, animated])].filter(value => typeof value === 'string');
    };
    const links = flat.flatMap(element => hrefsOf(element))
        .map(href => ({linkHref: href, link: resolve(href)}));
    const submits = element => !!element && !!element.form && (
        (element.localName === 'button' && element.type === 'submit')
        || (element.localName === 'input' && ['submit', 'image'].includes(element.type)));
    const controlsOf = elements => elements
        .filter(element => element.localName === 'label' && element.control)
        .map(element => element.control);
    const ownControls = controlsOf(ancestors);
    // The submit control a click activates: the node, a button or input
    // around it, or its label's control. Otherwise Enter or a click submits
    // its form through the form's default button.
    const own = [...ancestors, ...ownControls].filter(submits);
    const inner = [...descendants, ...controlsOf(descendants)].filter(submits);
    // Check boxes and radios a click changes besides the node: its label's
    // control, and any inside it or reached through a label inside it.
    const toggled = [...new Set([...ownControls, ...descendants, ...controlsOf(descendants)])]
        .filter(element => element !== node && element.localName === 'input'
            && ['checkbox', 'radio'].includes(element.type));
    if (toggled.length > 16) {
        opaque = true;
    }
    const described = toggled.slice(0, 16).map(element => [
        element.getAttribute('aria-label'),
        referenced(element, element.getAttribute('aria-labelledby')),
        Array.from(element.labels || []).map(label => label.innerText).join(' '),
        element.getAttribute('title'),
        element.getAttribute('value'),
    ].filter(Boolean).join(' '));
    const actionOf = submitter => resolve(
        (submitter.hasAttribute('formaction')
            ? submitter.getAttribute('formaction')
            : submitter.form.getAttribute('action')) || document.URL);
    // A form-associated custom element has no form property of its own, and
    // its form attribute names its owner as any control's does.
    const formOf = element => {
        if (element.form instanceof HTMLFormElement) {
            return element.form;
        }
        const named = element.hasAttribute('form') ? byId(element, element.getAttribute('form'))
            : null;
        return named instanceof HTMLFormElement ? named : null;
    };
    const form = own.length ? null
        : (formOf(node) || ownControls.map(formOf).find(Boolean)
            || ancestors.find(element => element.localName === 'form') || null);
    const defaultButton = form
        ? Array.from(form.getRootNode().querySelectorAll('button,input'))
            .find(element => element.form === form && submits(element)) || null
        : null;
    const forms = [
        ...own.map(actionOf),
        ...(form ? [defaultButton ? actionOf(defaultButton)
            : resolve(form.getAttribute('action') || document.URL)] : []),
        ...inner.map(actionOf),
    ];
    if (links.length > 64 || forms.length > 64) {
        opaque = true;
    }
    const dialog = ancestors.find(
        element => element.matches('dialog,[role=dialog],[role=alertdialog]')) || null;
    const heading = dialog ? dialog.querySelector('h1,h2,h3') : null;
    const images = reached.filter(element => element.localName === 'img').slice(0, 8)
        .map(image => image.getAttribute('alt') || '');
    const unrendered = ['style', 'script', 'template', 'noscript'];
    const shadowText = reached.filter(element => element.shadowRoot)
        .flatMap(element => Array.from(element.shadowRoot.children))
        .filter(element => !unrendered.includes(element.localName))
        .map(element => element.innerText ?? element.textContent ?? '');
    const tag = node.tagName.toLowerCase();
    const type = (node.getAttribute('type') || '').toLowerCase();
    const valued = tag === 'button'
        || (tag === 'input' && ['submit', 'button', 'reset'].includes(type));
    // Each label source at full length; one past 1,024 characters cannot be
    // read whole, and the live check refuses it.
    const sources = {
        aria_label: node.getAttribute('aria-label'),
        aria_labelledby: referenced(node, node.getAttribute('aria-labelledby')),
        label: [...Array.from(node.labels || []).map(label => label.innerText), ...described]
            .join(' '),
        title: node.getAttribute('title'),
        placeholder: node.getAttribute('placeholder'),
        alt: [node.getAttribute('alt') || '', ...images].join(' '),
        value: valued ? node.getAttribute('value') : '',
        visible_text: [node.innerText || '', ...shadowText].join(' '),
    };
    const labels = Object.fromEntries(
        Object.entries(sources).map(([source, value]) => [source, clean(value)]));
    // An action's rendered label remains discoverable inside an editor; it is
    // not the editor's field value. The private facts still fence its key use.
    const actionLabel = node.matches('a,button,[role="button"],[role="link"]');
    const privateText = (node.isContentEditable && !actionLabel) ||
        ['textbox', 'searchbox', 'combobox', 'spinbutton'].includes(node.getAttribute('role'));
    const metadata = {
        tag,
        role: node.getAttribute('role'),
        inputType: node.getAttribute('type'),
        name: Array.from(referenced(node, node.getAttribute('aria-labelledby')).trim() ||
            node.getAttribute('aria-label') ||
            Array.from(node.labels || []).map(label => label.innerText).join(' ').trim() ||
            node.getAttribute('title') ||
            node.getAttribute('placeholder') || (privateText ? '' : node.innerText) || '')
            .slice(0, 1024).join(''),
        autocomplete: node.getAttribute('autocomplete') || '',
        editable: node.isContentEditable === true,
        labels,
        overlong: Object.values(sources).some(value => collapse(value).length > 1024),
        links: links.slice(0, 64),
        forms: forms.slice(0, 64),
        opaque,
        download: flat.some(element => anchor(element) && element.hasAttribute('download')),
        context: dialog ? clean(dialog.getAttribute('aria-label')
            || referenced(dialog, dialog.getAttribute('aria-labelledby'))
            || (heading ? heading.textContent : '')) : '',
    };
    const read = [...ancestors, ...descendants, ...ownControls, ...controlsOf(descendants),
        ...(defaultButton ? [defaultButton] : [])];
    return {metadata, read};
}"""
_ELEMENT_SCRIPT = "node => (" + _READ_ELEMENT_SCRIPT + ")(node).metadata"
# The live read of a grant-constrained act, which also arms a click guard in
# the same turn of the page's event loop: until released, a trusted click aimed
# at any element the read did not cover is stopped with its default action.
# Only the act's own input makes trusted clicks, so a page that moves another
# control under the click point, or swaps a label's control, after the read
# cannot redirect it. Returns the release function, which reports whether the
# guard stopped a click, with the metadata on it.
_GUARDED_READ_SCRIPT = (
    "node => { const read = ("
    + _READ_ELEMENT_SCRIPT
    + """)(node);
    const covered = new Set(read.read);
    let stopped = false;
    const guard = event => {
        if (!event.isTrusted || covered.has(event.composedPath()[0])) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        stopped = true;
    };
    window.addEventListener('click', guard, true);
    const release = () => {
        window.removeEventListener('click', guard, true);
        return stopped;
    };
    release.metadata = read.metadata;
    return release;
}"""
)
# The options a selection of ``wanted`` could choose, as flat label texts, or
# null when there are too many options, or matches, to read them all.
_OPTION_SCRIPT = """(node, wanted) => {
    const squeeze = value => String(value ?? '').replace(/\\s+/g, ' ').trim();
    const options = node.localName === 'select' ? Array.from(node.options) : [];
    if (options.length > 4096) {
        return null;
    }
    const chosen = options.filter(option => option.value === wanted || option.label === wanted
        || squeeze(option.label) === squeeze(wanted) || squeeze(option.text) === squeeze(wanted));
    if (chosen.length > 64) {
        return null;
    }
    const referenced = (element, ids) => squeeze(ids).split(' ').filter(Boolean).slice(0, 8)
        .map(id => {
            const tree = element.getRootNode();
            const target = (tree.getElementById ? tree.getElementById(id) : null)
                || document.getElementById(id);
            return target ? target.textContent : '';
        }).join(' ');
    const sources = element => [element.getAttribute('aria-label'),
        referenced(element, element.getAttribute('aria-labelledby')),
        element.getAttribute('title')];
    return chosen.flatMap(option => {
        const group = option.parentElement && option.parentElement.localName === 'optgroup'
            ? option.parentElement : null;
        return [option.label, option.value, option.text, ...sources(option),
            ...(group ? [group.label, ...sources(group)] : [])];
    }).map(squeeze).filter(Boolean);
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
    # The runtime types only into an input or a text area, selects only in a
    # select and checks only a native check box or radio (``act``), so a kind
    # never lets the worker cover what the runtime refuses.
    if tag == "input":
        return _INPUT_FIELD_KINDS.get(input_type.lower(), BrowserFieldKind.OTHER)
    if tag == "textarea":
        return BrowserFieldKind.MULTILINE
    if tag == "select":
        return BrowserFieldKind.SELECT
    if editable:
        return BrowserFieldKind.EDITABLE
    if role.lower() in _CHOICE_ROLES:
        return BrowserFieldKind.CUSTOM_CHOICE
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


def _element_facts(metadata: dict[str, Any], *, name: str, page_url: str) -> BrowserElementFacts:
    """Derive one element's facts from its live attributes (ADR-0129 section 4.5)."""

    # A source is left out only when the classifier reads it exactly as the
    # name, which the worker classifies whole; one that merely normalizes to
    # the name, such as "Continue $" beside "Continue", is kept, because the
    # runtime's live check classifies every source as written.
    kept = {
        source: value
        for source, value in _live_labels(metadata).items()
        if not label_reads_as(value, name)
    }
    labels = {source: value[:MAXIMUM_FACT_LABEL_CHARACTERS] for source, value in kept.items()}
    # A source past what the facts carry, or past what the runtime reads, may
    # hold an excluded word in the part left out; a task grant never covers it.
    truncated = metadata.get("overlong") is not False or any(
        len(value) > MAXIMUM_FACT_LABEL_CHARACTERS for value in kept.values()
    )
    links = metadata.get("links")
    forms = metadata.get("forms")
    link_targets = [
        target
        for link in (links if isinstance(links, list) else [])
        if isinstance(link, dict) and (target := _link_target(link, page_url=page_url))
    ]
    if metadata.get("opaque") is not False:
        # An embedded document or a walk past its bounds: the element may
        # reach a target no one listed.
        link_targets.append(_OUTSIDE_EVERY_ORIGIN)
    form_targets = [
        _target_facts(form, page_url=page_url)
        for form in (forms if isinstance(forms, list) else [])
        if isinstance(form, str)
    ]
    return BrowserElementFacts(
        field_kind=_field_kind(
            tag=str(metadata.get("tag") or ""),
            input_type=str(metadata.get("inputType") or ""),
            autocomplete=str(metadata.get("autocomplete") or ""),
            role=str(metadata.get("role") or ""),
            editable=metadata.get("editable") is True,
        ),
        labels=labels,
        labels_truncated=truncated,
        link_target=_meet(link_targets),
        form_target=_meet(form_targets),
        download=metadata.get("download") is True,
        context_name=str(metadata.get("context") or "")[:MAXIMUM_CONTEXT_NAME_CHARACTERS],
    )


def _meet(targets: list[BrowserTargetFacts]) -> BrowserTargetFacts | None:
    """One target that is inside an origin and prefix only when every target is.

    Same-origin only when all are, a first segment only when all share it, and
    sensitive when any is.
    """

    if not targets:
        return None
    first = targets[0].first_segment
    return BrowserTargetFacts(
        same_origin=all(target.same_origin for target in targets),
        first_segment=(first if all(target.first_segment == first for target in targets) else None),
        sensitive_path=any(target.sensitive_path for target in targets),
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
    """Serialize access to one ephemeral browser and its mutable page state."""

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
        """Bind an isolated runtime, its origin policy, and operation serialization."""
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
        self._operation_lock = asyncio.Lock()

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
        """Navigate serially so another dispatch cannot consume cancellation state."""
        if not self.allows(url):
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        async with self._operation_lock:
            await self._start()
            try:
                observation = await self._runtime.navigate(url)
                await check_browser_interruption(self._runtime)
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
        """Refresh the page only after the preceding browser operation finishes."""
        async with self._operation_lock:
            await self._start()
            try:
                observation = await self._runtime.observe()
                await check_browser_interruption(self._runtime)
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

    async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
        async with self._operation_lock:
            await self._start()
            try:
                observation = await expand_browser_observation(self._runtime, request)
                await check_browser_interruption(self._runtime)
            except BrowserProviderError:
                raise
            except Exception as exc:
                raise BrowserProviderError(
                    "tool.browser.provider_unavailable", retryable=True
                ) from exc
            if not self.allows(observation.url):
                raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
            return observation

    async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
        async with self._operation_lock:
            await self._start()
            try:
                observation = await extract_browser_observation(self._runtime, request)
                await check_browser_interruption(self._runtime)
            except BrowserProviderError:
                raise
            except Exception as exc:
                raise BrowserProviderError(
                    "tool.browser.provider_unavailable", retryable=True
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
        """Dispatch one revision-bound action without concurrent page mutation."""
        async with self._operation_lock:
            await self._start()
            try:
                await check_browser_interruption(self._runtime)
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
            return await browser_action_interruption(self._runtime, observation)

    async def upload(self, action: BrowserAction, image: BrowserImageFile) -> BrowserObservation:
        """Transfer an approved image while exclusively holding the browser page."""
        async with self._operation_lock:
            await self._start()
            try:
                await check_browser_interruption(self._runtime)
                observation = await upload_browser_image(self._runtime, action, image)
            except BrowserProviderError:
                raise
            except Exception as exc:
                raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False) from exc
            if not self.allows(observation.url):
                raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
            return await browser_action_interruption(self._runtime, observation)

    async def close(self) -> None:
        """Wait for the active operation before releasing runtime and proxy resources."""
        async with self._operation_lock:
            try:
                await self._runtime.close()
            finally:
                if self._proxy is not None:
                    await self._proxy.close()
                self._proxy = None
                self._started = False
