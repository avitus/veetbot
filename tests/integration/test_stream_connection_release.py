"""A dropped stream gives its dedicated PostgreSQL connection back (2026-11-03 outage).

Each run and Cockpit stream holds one `LISTEN` connection. When a client drops,
the web server cancels the request's scope, and that cancellation is delivered
again at every later await, so a cleanup that awaits never finishes. One long
chat reconnecting every 0.4 seconds used up PostgreSQL's 100 connections in
about three minutes. Only a real connection shows this: the in-memory broadcaster
has nothing to await while it cleans up.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import anyio
import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy.engine import make_url
from starlette.types import ASGIApp, Message

from agent_core.adapters.live_events import PostgresLiveEventBroadcaster
from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.views import TextContentBlock
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.integration.disposable_database import disposable_database_url

pytestmark = pytest.mark.integration

SESSION_ID = UUID("00000000-0000-0000-0000-000000000521")
REPLAY_EVENTS = 40
REPLAY_EVENT_BYTES = 2_000


@pytest.fixture
def named_url() -> str:
    """A database URL whose connections can be counted by their own application name."""
    return (
        make_url(disposable_database_url())
        .update_query_dict({"application_name": f"stream-release-{uuid4().hex[:12]}"})
        .render_as_string(hide_password=False)
    )


async def _open_connections(url: str) -> int:
    """Count this test's connections by the application name its URL carries."""
    parsed = make_url(url)
    name = parsed.query["application_name"]
    connection = await asyncpg.connect(
        parsed.difference_update_query(["application_name"])
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    try:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1", name
            )
        )
    finally:
        await connection.close()


async def _settles_at(url: str, expected: int, *, seconds: float = 5.0) -> int:
    """Return the connection count once it reaches `expected`, or the last count seen."""
    count = await _open_connections(url)
    deadline = asyncio.get_running_loop().time() + seconds
    while count != expected and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        count = await _open_connections(url)
    return count


async def test_a_cancelled_subscription_closes_its_connection(named_url: str) -> None:
    """Scope cancellation repeats at every await, so cleanup cannot depend on one."""
    broadcaster = PostgresLiveEventBroadcaster(named_url)
    with anyio.CancelScope() as scope:
        async with broadcaster.subscribe(SESSION_ID):
            assert await _open_connections(named_url) == 1
            scope.cancel()
            await anyio.sleep(0)
    assert scope.cancelled_caught
    assert await _settles_at(named_url, 0) == 0


async def test_an_ordinary_exit_closes_its_connection(named_url: str) -> None:
    """Terminating instead of closing must not leave the ordinary path behind."""
    broadcaster = PostgresLiveEventBroadcaster(named_url)
    async with broadcaster.subscribe(SESSION_ID):
        assert await _open_connections(named_url) == 1
    assert await _settles_at(named_url, 0) == 0


def _settings(tmp_path: Path) -> Settings:
    """Settings for an in-memory composition; only the broadcaster here is real."""
    return Settings(
        database_url="postgresql+asyncpg://unused/agent",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
    )


@asynccontextmanager
async def _a_long_running_stream(tmp_path: Path, url: str) -> AsyncIterator[tuple[ASGIApp, str]]:
    """Serve one running run with a long history over real uvicorn."""
    script = FakeModelScript(turns=[ScriptedTurn(text="answer")])
    async with build(
        settings=_settings(tmp_path), sequential_ids=True, script=script
    ) as composition:
        principal = composition.principal
        session = await composition.services.sessions.create(principal, "general", {})
        runs: Any = composition.services.runs
        submitted = await runs.submit(
            principal, session.id, [TextContentBlock(text="hello")], None, None
        )
        async with composition.uow_factory() as uow:
            run = await uow.runs.get(submitted.run_id, principal)
            events = await uow.events.list_after(session.id, 0, principal)
        running = run.model_copy(update={"status": RunStatus.RUNNING})
        sample = next(event for event in events if event.run_id == run.id)
        history = [
            sample.model_copy(
                update={"sequence": sequence, "payload": {"padding": "x" * REPLAY_EVENT_BYTES}}
            )
            for sequence in range(1, REPLAY_EVENTS + 1)
        ]

        async def events_after(
            _principal: object, _run_id: UUID, after: int
        ) -> tuple[Run, list[EventEnvelope]]:
            return running, [event for event in history if event.sequence > after]

        runs._events_after = events_after
        runs._live_events = PostgresLiveEventBroadcaster(url)
        app = create_app(
            composition.services,
            composition.settings,
            principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        yield app, f"/v1/runs/{run.id}/events"


async def _drop_client_after(app: ASGIApp, path: str, body_chunks: int) -> None:
    """Run one request the way uvicorn does, with the client leaving after `body_chunks`."""
    delivered = 0
    client_left = asyncio.Event()
    request_sent = False

    async def receive() -> Message:
        """Deliver the request, then wait for the client to leave."""
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await client_left.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        """Count body chunks and drop the client after the chosen one."""
        nonlocal delivered
        if message["type"] == "http.response.body":
            delivered += 1
            if delivered >= body_chunks:
                client_left.set()
                # A real transport yields here; the disconnect listener runs next.
                await asyncio.sleep(0.01)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"test"), (b"accept", b"text/event-stream")],
            "client": ("127.0.0.1", 40000),
            "server": ("test", 80),
        },
        receive,
        send,
    )


@pytest.mark.parametrize(
    "body_chunks",
    [1, REPLAY_EVENTS // 2, REPLAY_EVENTS],
    ids=["at-the-first-frame", "mid-replay", "replay-finished"],
)
async def test_a_dropped_client_leaves_no_connection_behind(
    tmp_path: Path, named_url: str, body_chunks: int
) -> None:
    """The outage's shape: a client reconnects repeatedly and each stream ends early."""
    async with _a_long_running_stream(tmp_path, named_url) as (app, path):
        for _ in range(5):
            await asyncio.wait_for(_drop_client_after(app, path, body_chunks), timeout=20)
        assert await _settles_at(named_url, 0) == 0
