"""Ephemeral Playwright browser provider with audited proxy egress."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol, cast
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Download,
    ElementHandle,
    Page,
    Playwright,
    Route,
    StorageState,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

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
from agent_core.execution.proxy import start_worker_egress_proxy

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


def _origin_allowed(url: str, allowed_origins: tuple[str, ...]) -> bool:
    try:
        return browser_origin(url) in allowed_origins
    except ValueError:
        return False


class PythonPlaywrightRuntime:
    """Own a headless Chromium process and one non-persistent browser context."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._temporary_home: tempfile.TemporaryDirectory[str] | None = None
        self._allowed_origins: tuple[str, ...] = ()
        self._revision: str | None = None
        self._elements: dict[str, ElementHandle] = {}

    async def start(
        self,
        proxy_url: str,
        allowed_origins: tuple[str, ...],
        *,
        storage_state: dict[str, object] | None = None,
        interactive: bool = False,
    ) -> None:
        if self._browser is not None:
            return
        # Authentication uses screenshots and synthetic input, both supported
        # by the same headless context, so no alternate launch mode is needed.
        del interactive
        self._allowed_origins = allowed_origins
        self._temporary_home = tempfile.TemporaryDirectory(prefix="veetbot-browser-")
        temporary_home = self._temporary_home.name
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            proxy={"server": proxy_url},
            args=["--proxy-bypass-list=<-loopback>"],
            env={
                "HOME": temporary_home,
                "PATH": os.defpath,
                "TMPDIR": temporary_home,
            },
        )
        self._context = await self._browser.new_context(
            accept_downloads=False,
            service_workers="block",
            storage_state=(None if storage_state is None else cast(StorageState, storage_state)),
        )
        await self._context.route("**/*", self._route)
        self._page = await self._context.new_page()
        self._page.on("dialog", self._dismiss_dialog)
        self._page.on("download", self._cancel_download)
        self._context.on("page", self._close_popup)

    async def _route(self, route: Route) -> None:
        if _origin_allowed(route.request.url, self._allowed_origins):
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
        page = self._current_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
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
        count = min(await locator.count(), MAXIMUM_ELEMENTS)
        elements: list[BrowserElement] = []
        handles: dict[str, ElementHandle] = {}
        for index in range(count):
            element = locator.nth(index)
            if not await element.is_visible():
                continue
            handle = await element.element_handle()
            if handle is None:
                continue
            role = await element.get_attribute("role")
            tag = await element.evaluate("node => node.tagName.toLowerCase()")
            input_type = await element.get_attribute("type")
            resolved_role = role or _default_role(str(tag), input_type)
            name = (
                await element.get_attribute("aria-label")
                or await element.get_attribute("title")
                or await element.get_attribute("placeholder")
                or (await element.inner_text(timeout=2_000))
            )
            checked: bool | None = None
            if str(tag) == "input" and input_type in {"checkbox", "radio"}:
                checked = await element.is_checked()
            ref = f"{revision}:{index}"
            elements.append(
                BrowserElement(
                    ref=ref,
                    role=resolved_role,
                    name=name[:1024],
                    disabled=await element.is_disabled(),
                    checked=checked,
                )
            )
            handles[ref] = handle
        self._revision = revision
        self._elements = handles
        return BrowserObservation(
            url=page.url,
            title=await page.title(),
            revision=revision,
            text=text,
            elements=tuple(elements),
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
            if self._temporary_home is not None:
                with suppress(OSError):
                    self._temporary_home.cleanup()
        finally:
            self._context = None
            self._browser = None
            self._playwright = None
            self._page = None
            self._temporary_home = None
            self._revision = None
            self._elements = {}


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
        proxy_factory: ProxyFactory = start_worker_egress_proxy,
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
