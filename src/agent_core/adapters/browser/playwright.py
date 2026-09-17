"""Ephemeral Playwright browser provider with audited proxy egress."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Dialog,
    Download,
    ElementHandle,
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
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationStatus,
    BrowserElement,
    BrowserInteractiveEvent,
    BrowserObservation,
    BrowserProviderError,
    browser_origin,
    normalize_browser_origin,
)
from agent_core.domain.execution import EgressDestination, EgressMode, EgressPolicy
from agent_core.domain.web import is_public_https_url
from agent_core.execution.proxy import start_browser_egress_proxy

MAXIMUM_ELEMENTS = 256
MAXIMUM_TEXT_CHARACTERS = 262_144


class BrowserRuntime(Protocol):
    async def start(self, proxy_url: str, allowed_origins: tuple[str, ...]) -> None: ...

    async def navigate(self, url: str) -> BrowserObservation: ...

    async def observe(self) -> BrowserObservation: ...

    async def act(self, action: BrowserAction) -> BrowserObservation: ...

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
        self._disallowed_navigation = False
        self._document_session: CDPSession | None = None
        self._main_frame_id: str | None = None

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
        self._context = await self._browser.new_context(
            accept_downloads=False,
            service_workers="block",
            storage_state=(None if storage_state is None else cast(StorageState, storage_state)),
        )
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

    async def _start_virtual_display(self) -> str | None:
        """Start the ceremony's private display, or use the platform's native one."""
        display = (self._virtual_display_factory or platform_virtual_display)()
        if display is None:
            return None
        try:
            display_name = await display.start()
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
        self._page = page

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
        return await self._observation(page)

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
        snapshot = (await locator.element_handles())[:MAXIMUM_ELEMENTS]
        slots = asyncio.Semaphore(8)

        async def capture(index: int, handle: ElementHandle) -> BrowserElement | None:
            async with slots:
                return await self._element_observation(handle, f"{revision}:{index}")

        captured = await asyncio.gather(
            *(capture(index, handle) for index, handle in enumerate(snapshot))
        )
        elements: list[BrowserElement] = []
        handles: dict[str, ElementHandle] = {}
        for handle, element in zip(snapshot, captured, strict=True):
            if element is not None:
                elements.append(element)
                handles[element.ref] = handle
        self._revision = revision
        self._elements = handles
        return BrowserObservation(
            url=page.url,
            title=await page.title(),
            revision=revision,
            text=text,
            elements=tuple(elements),
        )

    @staticmethod
    async def _element_observation(handle: ElementHandle, ref: str) -> BrowserElement | None:
        """Read one captured node without resolving a selector that may have changed."""
        if not await handle.is_visible():
            return None
        metadata = await handle.evaluate(
            """node => ({
                tag: node.tagName.toLowerCase(),
                role: node.getAttribute('role'),
                inputType: node.getAttribute('type'),
                name: Array.from(node.getAttribute('aria-label') || node.getAttribute('title') ||
                    node.getAttribute('placeholder') || node.innerText || '')
                    .slice(0, 1024).join('')
            })"""
        )
        tag = str(metadata["tag"])
        input_type = metadata["inputType"]
        checked: bool | None = None
        if tag == "input" and input_type in {"checkbox", "radio"}:
            checked = await handle.is_checked()
        return BrowserElement(
            ref=ref,
            role=metadata["role"] or _default_role(tag, input_type),
            name=str(metadata["name"])[:1024],
            disabled=await handle.is_disabled(),
            checked=checked,
        )

    async def act(self, action: BrowserAction) -> BrowserObservation:
        page = self._current_page()
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
                await handle.press(
                    action.key.value if action.key is not None else "", timeout=30_000
                )
            else:
                await handle.scroll_into_view_if_needed(timeout=30_000)
                await page.mouse.wheel(0, action.delta_y or 0)
        except PlaywrightError as exc:
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False) from exc
        return await self._observation(page)

    async def storage_state(self) -> dict[str, object]:
        if self._context is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return cast(dict[str, object], await self._context.storage_state(indexed_db=True))

    async def authentication_status(self) -> BrowserAuthenticationStatus:
        page = self._current_page()
        if not _origin_allowed(page.url, self._allowed_origins):
            return BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED
        intervention = page.locator(
            "input[type=password],input[autocomplete=one-time-code],"
            "iframe[src*='captcha' i],iframe[title*='captcha' i],"
            "[class*='captcha' i],[id*='captcha' i]"
        )
        for index in range(await intervention.count()):
            if await intervention.nth(index).is_visible():
                return BrowserAuthenticationStatus.NEEDS_USER
        interactive_text = page.get_by_text(
            re.compile(
                r"(?:use\s+(?:a\s+)?passkey|verification\s+code|"
                r"multi-factor|two-factor|consent\s+required)",
                re.IGNORECASE,
            )
        )
        if await interactive_text.count():
            return BrowserAuthenticationStatus.NEEDS_USER
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
            await page.keyboard.insert_text(event.text)
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
            self._disallowed_navigation = False
            self._document_session = None
            self._main_frame_id = None


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

    async def act(self, action: BrowserAction) -> BrowserObservation:
        await self._start()
        try:
            observation = await self._runtime.act(action)
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
