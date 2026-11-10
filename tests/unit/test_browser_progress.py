"""Live browser phases reach the owner before an operation completes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from agent_core.browser_control_plane.api import _diagnosed_response
from agent_core.domain.browser_diagnostics import browser_phase
from tests.unit.test_browser_dispatch_constraint import SERVICE_AUTH, _app, leased

PROGRESS_MEDIA_TYPE = "application/vnd.veetbot.browser-progress+ndjson"


def request() -> Request:
    return Request({"type": "http", "headers": [(b"accept", PROGRESS_MEDIA_TYPE.encode())]})


async def test_hosted_phase_arrives_before_result_and_disconnect_cancels_operation() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def operation() -> dict[str, str]:
        try:
            with browser_phase("navigation"):
                entered.set()
                await asyncio.Event().wait()
            return {"revision": "one"}
        finally:
            cancelled.set()

    async with asyncio.timeout(0.2):
        response = await _diagnosed_response(request(), operation(), lambda value: value)
    assert isinstance(response, StreamingResponse), "browser phases wait for the final response"
    iterator = cast(AsyncGenerator[bytes, None], response.body_iterator)
    async with asyncio.timeout(1):
        frame = json.loads(await anext(iterator))
        await entered.wait()
    assert frame["type"] == "progress"
    assert frame["progress"]["phase"] in {"operation", "navigation"}
    assert not cancelled.is_set()
    await iterator.aclose()
    assert cancelled.is_set()


@pytest.mark.parametrize("missing", [False, True])
async def test_hosted_progress_retains_final_observation_or_refusal(
    tmp_path: Path, missing: bool
) -> None:
    sessions, _runtime, lease = await leased(tmp_path)
    async with _app(sessions) as client:
        response = await client.post(
            "/v1/browser-sessions:observe",
            headers={"Authorization": f"Bearer {SERVICE_AUTH}", "Accept": PROGRESS_MEDIA_TYPE},
            json={"lease_ref": "missing-lease-" * 4 if missing else lease},
        )
    assert response.headers["content-type"].startswith(PROGRESS_MEDIA_TYPE)
    frames = [json.loads(line) for line in response.text.splitlines()]
    assert frames[-1]["type"] == "result"
    assert frames[-1]["status"] == (409 if missing else 200)
    assert "browser_diagnostics" in frames[-1]["body"]
    assert all(lease not in json.dumps(frame) for frame in frames[:-1])
    if not missing:
        assert frames[-1]["body"]["revision"]


async def test_hosted_stream_maps_unknown_errors_without_leaking_text() -> None:
    async def operation() -> dict[str, str]:
        raise RuntimeError("secret-cookie-canary")

    response = await _diagnosed_response(request(), operation(), lambda value: value)
    assert isinstance(response, StreamingResponse)
    frames = [
        json.loads(chunk) async for chunk in cast(AsyncIterator[bytes], response.body_iterator)
    ]
    assert frames[-1]["status"] == 500
    assert "secret-cookie-canary" not in json.dumps(frames)


async def test_hosted_progress_rejects_unauthenticated_requests_before_execution(
    tmp_path: Path,
) -> None:
    sessions, runtime, lease = await leased(tmp_path)
    before = list(runtime.actions)
    async with _app(sessions) as client:
        response = await client.post(
            "/v1/browser-sessions:observe",
            headers={"Accept": PROGRESS_MEDIA_TYPE},
            json={"lease_ref": lease},
        )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert runtime.actions == before


@pytest.mark.parametrize("ending", ["success", "refusal", "missing", "oversized", "duplicate"])
async def test_hosted_client_reads_progress_before_final_result_and_never_reposts(
    ending: str,
) -> None:
    import httpx

    from agent_core.adapters.browser.hosted_sessions import (
        MAXIMUM_SESSION_RESPONSE_BYTES,
        HostedBrowserSessionControlPlane,
    )
    from agent_core.adapters.credentials import MappingCredentialResolver
    from agent_core.domain.browser import BrowserProviderError
    from agent_core.domain.browser_diagnostics import collect_browser_diagnostics
    from tests.unit.test_browser_tools import FakeBrowserProvider

    seen = asyncio.Event()
    calls = []
    updates = []

    def progress(update: object) -> None:
        updates.append(update)
        seen.set()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield (
                b'{"type":"progress","progress":{"version":1,"phase":"navigation",'
                b'"placement":"runtime","elapsed_ms":10}}\n'
            )
            async with asyncio.timeout(1):
                await seen.wait()
            if ending == "missing":
                return
            if ending == "oversized":
                yield b"x" * (MAXIMUM_SESSION_RESPONSE_BYTES + 100_000)
                return
            body = (
                FakeBrowserProvider()._observation("https://example.org/").model_dump(mode="json")
            )
            status = 200
            if ending == "refusal":
                status = 409
                body = {"error": {"code": "tool.browser.page_changed"}}
            frame = json.dumps({"type": "result", "status": status, "body": body}).encode() + b"\n"
            # Split inside the JSON record to exercise arbitrary transport chunks.
            yield frame[:15]
            yield frame[15:]
            if ending == "duplicate":
                yield frame

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["accept"] == PROGRESS_MEDIA_TYPE
        return httpx.Response(200, headers={"content-type": PROGRESS_MEDIA_TYPE}, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        client = HostedBrowserSessionControlPlane(
            base_url="https://browser.example.org",
            credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
            client=http,
        )
        with collect_browser_diagnostics(
            clock=asyncio.get_running_loop().time, on_progress=progress
        ):
            if ending == "success":
                observation = await client.observe("opaque-lease" * 4)
                assert observation.revision == "revision-1"
            else:
                with pytest.raises(BrowserProviderError) as error:
                    await client.observe("opaque-lease" * 4)
                assert error.value.reason_code == (
                    "tool.browser.page_changed"
                    if ending == "refusal"
                    else "tool.browser.output_invalid"
                )
    assert updates and len(calls) == 1


async def test_local_progress_is_bounded_and_listener_failures_do_not_change_results() -> None:
    from agent_core.domain.browser_diagnostics import collect_browser_diagnostics

    seen: list[object] = []

    def record(update: object) -> None:
        seen.append(update)
        raise RuntimeError("private-listener-error")

    with (
        collect_browser_diagnostics(clock=lambda: 1.0, on_progress=record) as collector,
        browser_phase("operation", "orchestrator"),
    ):
        for _ in range(200):
            with browser_phase("observation"):
                pass
    assert 1 <= len(seen) <= 128
    assert collector.snapshot().truncated
    assert "private-listener-error" not in collector.snapshot().model_dump_json()


async def test_progress_publisher_failure_cannot_change_tool_result() -> None:
    from agent_core.adapters.determinism import SequenceIdFactory
    from agent_core.adapters.live_events import InMemoryLiveEventBroadcaster
    from agent_core.domain.browser import BrowserObservation
    from agent_core.domain.messages import ToolCallItem
    from agent_core.domain.runs import Step
    from agent_core.runtime.cancellation import RunCancellationToken
    from agent_core.tools.browser_observe import BrowserObserveTool
    from agent_core.tools.executor import ToolPipeline
    from agent_core.tools.registry import StaticToolRegistry
    from tests.contract.support import NOW, agent, principal, run
    from tests.unit.test_browser_tools import FakeBrowserProvider
    from tests.unit.test_history_cache_window import _checkpoint, _factory

    class Broadcaster(InMemoryLiveEventBroadcaster):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.stopped = asyncio.Event()

        async def publish(
            self, session_id: UUID, run_id: UUID, event: str, data: dict[str, Any]
        ) -> None:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.set()

    broadcaster = Broadcaster()

    class Provider(FakeBrowserProvider):
        async def observe(self) -> BrowserObservation:
            with browser_phase("observation"):
                async with asyncio.timeout(1):
                    await broadcaster.entered.wait()
                return await super().observe()

    clock, factory = await _factory()
    registry = StaticToolRegistry()
    tool = BrowserObserveTool(Provider())
    registry.register(tool)
    pipeline = ToolPipeline(registry, factory, clock, SequenceIdFactory(), live_events=broadcaster)
    active = run()
    async with factory() as uow:
        await uow.runs.create(active)
    call = ToolCallItem(
        call_id="observe", item_index=0, name=tool.spec.name, arguments={}, raw_arguments="{}"
    )
    results = await pipeline.dispatch(
        run=active,
        checkpoint=_checkpoint(active, []),
        tool_calls=[call],
        principal=principal(),
        step=Step(run_id=active.id, step_number=1, started_at=NOW),
        agent=agent().model_copy(update={"enabled_tools": [tool.spec.name]}),
        token=RunCancellationToken(clock, None),
    )
    assert not results[0].is_error
    assert broadcaster.stopped.is_set()
    async with factory() as uow:
        events = await uow.events.list_after(active.session_id, 0, principal())
    assert all(event.event_type != "tool.call.progress" for event in events)


async def test_progress_publishing_uses_only_the_matching_run_and_call() -> None:
    from uuid import uuid4

    from agent_core.adapters.live_events import InMemoryLiveEventBroadcaster
    from agent_core.domain.browser_diagnostics import BrowserProgress
    from agent_core.tools.browser_progress import publish_browser_progress

    broadcaster = InMemoryLiveEventBroadcaster()
    session, run_id = uuid4(), uuid4()
    async with broadcaster.subscribe(session) as subscription:
        async with publish_browser_progress(
            broadcaster, session_id=session, run_id=run_id, call_id="read", name="browser.observe"
        ) as offer:
            assert offer is not None
            for elapsed in range(100):
                offer(BrowserProgress(phase="observation", placement="runtime", elapsed_ms=elapsed))
            notification = await subscription.receive(1)
            assert notification is not None and notification.kind == "transient"
            assert notification.sequence is None and notification.run_id == run_id
            assert notification.event == "tool.call.progress"
            assert notification.data == {
                "run_id": str(run_id),
                "call_id": "read",
                "name": "browser.observe",
                "browser_progress": {
                    "version": 1,
                    "phase": "observation",
                    "placement": "runtime",
                    "elapsed_ms": 99,
                },
            }
        assert await subscription.receive(0.01) is None


@pytest.mark.parametrize("streaming", [False, True])
async def test_invalid_progress_is_ignored_and_old_json_services_still_work(
    streaming: bool,
) -> None:
    import httpx

    from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
    from agent_core.adapters.credentials import MappingCredentialResolver
    from agent_core.domain.browser_diagnostics import collect_browser_diagnostics
    from tests.unit.test_browser_tools import FakeBrowserProvider

    seen: list[object] = []
    observation = FakeBrowserProvider()._observation("https://example.org/").model_dump(mode="json")

    def serve(_request: httpx.Request) -> httpx.Response:
        if not streaming:
            return httpx.Response(200, json=observation)
        frames = [
            {
                "type": "progress",
                "progress": {
                    "version": 1,
                    "phase": "secret-cookie-canary",
                    "placement": "runtime",
                    "elapsed_ms": 10,
                },
            },
            {"type": "result", "status": 200, "body": observation},
        ]
        return httpx.Response(
            200,
            headers={"content-type": PROGRESS_MEDIA_TYPE},
            content="".join(json.dumps(frame) + "\n" for frame in frames),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        client = HostedBrowserSessionControlPlane(
            base_url="https://browser.example.org",
            credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
            client=http,
        )
        with collect_browser_diagnostics(clock=lambda: 0.0, on_progress=seen.append):
            result = await client.observe("opaque-lease" * 4)
            assert result.revision == "revision-1"
    assert not seen


async def test_streamed_action_still_fences_repeated_dispatch(tmp_path: Path) -> None:
    import hashlib

    from tests.unit.test_browser_dispatch_constraint import CLICK

    sessions, runtime, lease = await leased(tmp_path)
    digest = hashlib.sha256(lease.encode()).hexdigest()[:24]
    headers = {
        "Authorization": f"Bearer {SERVICE_AUTH}",
        "Accept": PROGRESS_MEDIA_TYPE,
        "Idempotency-Key": f"browser-session:{digest}:act:1",
    }
    payload = {"lease_ref": lease, "action": CLICK.model_dump(mode="json"), "sequence": 1}
    async with _app(sessions) as client:
        first = await client.post("/v1/browser-sessions:act", headers=headers, json=payload)
        repeated = await client.post("/v1/browser-sessions:act", headers=headers, json=payload)
    assert json.loads(first.text.splitlines()[-1])["status"] == 200
    assert json.loads(repeated.text.splitlines()[-1])["status"] == 409
    assert runtime.actions == [CLICK]


@pytest.mark.parametrize("blocked_send", [False, True])
async def test_http_disconnect_waits_for_operation_cleanup(blocked_send: bool) -> None:
    from starlette.types import Message

    entered = asyncio.Event()
    cleaned = asyncio.Event()
    sending = asyncio.Event()

    async def operation() -> dict[str, str]:
        try:
            entered.set()
            await asyncio.Event().wait()
            return {}
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    response = await _diagnosed_response(request(), operation(), lambda value: value)
    assert isinstance(response, StreamingResponse)

    async def receive() -> Message:
        await entered.wait()
        if blocked_send:
            await sending.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if blocked_send and message["type"] == "http.response.body":
            sending.set()
            await asyncio.Event().wait()

    async with asyncio.timeout(1):
        await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
    assert cleaned.is_set(), "the hosted response returned before cancelling its browser operation"
