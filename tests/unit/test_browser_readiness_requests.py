"""Finite readiness snapshots stay bounded and do not wait for later polling."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import cast

from playwright.async_api import Request, Response

from agent_core.adapters.browser.playwright import _ReadinessRequests as ReadinessRequests


class RequestStub:
    resource_type = "fetch"


def request() -> Request:
    return cast(Request, RequestStub())


async def test_snapshot_excludes_later_requests() -> None:
    tracker = ReadinessRequests()
    initial, later = request(), request()
    tracker.began(initial)
    waiting = asyncio.create_task(tracker.drain(1))
    await asyncio.sleep(0)
    tracker.began(later)
    tracker.ended(initial)
    assert await waiting
    assert not await tracker.drain(0.001)
    tracker.ended(later)
    assert await tracker.drain(1)


async def test_overflow_remains_explicit_until_a_new_document() -> None:
    tracker = ReadinessRequests()
    requests = [request() for _ in range(257)]
    for item in requests:
        tracker.began(item)
    for item in requests:
        tracker.ended(item)
    assert not await tracker.drain(1)
    tracker.reset()
    assert await tracker.drain(1)


async def test_established_event_stream_is_not_a_pending_finite_response() -> None:
    tracker = ReadinessRequests()
    item = request()
    tracker.began(item)
    response = SimpleNamespace(
        request=item, status=200, headers={"content-type": "text/event-stream; charset=utf-8"}
    )
    tracker.responded(cast(Response, response))
    assert await tracker.drain(1)


async def test_cancellation_does_not_forget_a_pending_request() -> None:
    tracker = ReadinessRequests()
    item = request()
    tracker.began(item)
    waiting = asyncio.create_task(tracker.drain(10))
    await asyncio.sleep(0)
    waiting.cancel()
    with suppress(asyncio.CancelledError):
        await waiting
    assert not await tracker.drain(0.001)
    tracker.ended(item)
    assert await tracker.drain(1)
