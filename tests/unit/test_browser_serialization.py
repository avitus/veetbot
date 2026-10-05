"""One ephemeral browser must not share mutable page state across dispatches."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

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
from agent_core.domain.browser_upload import BrowserImageFile
from agent_core.domain.media import MediaImage


async def test_overlapping_navigations_cannot_share_beforeunload_cancellation() -> None:
    """Only the navigation that dismissed its draft dialog receives cancellation."""
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://example.org",)
    page = Mock(spec=Page)
    page.url = "https://example.org/compose"
    page.is_closed.return_value = False
    runtime._page = page
    entered = asyncio.Event()
    release = asyncio.Event()
    dismissed = asyncio.Event()
    second_attempted = asyncio.Event()

    async def goto(url: str, **kwargs: object) -> None:
        """Overlap a draft cancellation with an unrelated aborted navigation."""
        if url.endswith("/first"):
            entered.set()
            await release.wait()
            await runtime._dismiss_dialog(Mock(type="beforeunload", dismiss=AsyncMock()))
            dismissed.set()
        else:
            await dismissed.wait()
        raise PlaywrightError("net::ERR_ABORTED")

    page.goto.side_effect = goto
    provider = PlaywrightBrowserProvider(
        tenant_id="tenant-a", allowed_origins=runtime._allowed_origins, runtime=runtime
    )
    provider._started = True

    async def second_navigation() -> BrowserObservation:
        """Signal that the second dispatch has attempted to enter the provider."""
        second_attempted.set()
        return await provider.navigate("https://example.org/second")

    first = asyncio.create_task(provider.navigate("https://example.org/first"))
    await entered.wait()
    second = asyncio.create_task(second_navigation())
    await second_attempted.wait()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, BrowserProviderError) for result in results)
    assert [
        result.reason_code for result in results if isinstance(result, BrowserProviderError)
    ] == [
        "tool.browser.navigation_cancelled",
        "tool.browser.provider_unavailable",
    ]


@pytest.mark.parametrize("operation", ["navigate", "observe", "act", "upload", "close"])
async def test_page_operations_wait_for_an_in_flight_navigation(operation: str) -> None:
    """Reads, writes, and shutdown cannot mutate the page during navigation."""
    runtime = Mock(spec=PythonPlaywrightRuntime)
    entered = asyncio.Event()
    release = asyncio.Event()
    attempted = asyncio.Event()
    other_entered = asyncio.Event()
    observation = BrowserObservation(url="https://example.org/compose", revision="current")

    async def other(*args: object, **kwargs: object) -> BrowserObservation:
        """Record when another operation actually reaches the browser runtime."""
        other_entered.set()
        return observation

    async def navigate(url: str) -> BrowserObservation:
        """Hold the initial navigation open while another dispatch is attempted."""
        if url.endswith("/first"):
            entered.set()
            await release.wait()
            return observation
        return await other()

    runtime.navigate = AsyncMock(side_effect=navigate)
    for name in ("observe", "act", "upload", "close"):
        setattr(runtime, name, AsyncMock(side_effect=other))
    provider = PlaywrightBrowserProvider(
        tenant_id="tenant-a", allowed_origins=("https://example.org",), runtime=runtime
    )
    provider._started = True
    action = BrowserAction(
        kind=BrowserActionKind.CLICK, expected_revision="current", ref="current:0"
    )
    image = BrowserImageFile(
        "00000000-0000-0000-0000-000000000001.png",
        MediaImage("image/png", b"\x89PNG\r\n\x1a\nsynthetic-image"),
    )

    async def dispatch() -> None:
        """Attempt the selected public operation against the busy provider."""
        attempted.set()
        if operation == "navigate":
            await provider.navigate("https://example.org/other")
        elif operation == "observe":
            await provider.observe()
        elif operation == "act":
            await provider.act(action)
        elif operation == "upload":
            await provider.upload(action, image)
        else:
            await provider.close()

    first = asyncio.create_task(provider.navigate("https://example.org/first"))
    await entered.wait()
    second = asyncio.create_task(dispatch())
    try:
        await attempted.wait()
        assert not other_entered.is_set(), "another dispatch entered the busy browser"
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert other_entered.is_set()
