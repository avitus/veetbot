"""The API and the interactive worker warm MCP discovery; other roles never do (ADR-0131).

The API listens and answers readiness before its warm-up starts any server.
"""

import asyncio
import re
import socket
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from uuid import UUID

import httpx
import pytest
import uvicorn

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.cli import main
from agent_core.cli.main import WorkerRole
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue
from agent_core.domain.events import EventEnvelope
from agent_core.domain.mcp import MCPServerConfig, ScriptedMCPServer
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.views import TextContentBlock
from tests.gates.test_tool_m8 import _discovery, _server, _settings

ROOT = Path(__file__).resolve().parents[2]


class _Composition:
    """Just enough of a composition for the service entry points."""

    def __init__(self) -> None:
        self.warmups = 0
        self.services = object()
        self.settings = object()
        self.principal = object()
        self.readiness_probe = object()

    def new_request_id(self) -> str:
        return "request"

    def start_mcp_discovery_warmup(self) -> None:
        self.warmups += 1

    def worker_factory(self, worker_id: str) -> object:
        return object()

    def async_worker_factory(self, worker_id: str) -> object:
        return object()

    def maintenance_factory(self) -> object:
        return object()


def _serve(monkeypatch: pytest.MonkeyPatch, composition: _Composition) -> None:
    @asynccontextmanager
    async def build(**options: Any) -> AsyncIterator[_Composition]:
        yield composition

    async def run(service: object) -> None:
        return None

    monkeypatch.setattr(main, "build", build)
    monkeypatch.setattr(main, "_run_worker_service", run)


@pytest.mark.parametrize(
    ("role", "warmups"),
    [
        (WorkerRole.INTERACTIVE, 1),
        (WorkerRole.WORKER, 1),
        (WorkerRole.ASYNC, 0),
        (WorkerRole.MAINTENANCE, 0),
    ],
)
async def test_only_the_workers_that_pin_chat_catalogs_warm_discovery(
    monkeypatch: pytest.MonkeyPatch, role: WorkerRole, warmups: int
) -> None:
    composition = _Composition()
    _serve(monkeypatch, composition)

    await main._serve_worker(role)

    assert composition.warmups == warmups


def _listening(path: str) -> bool:
    with socket.socket(socket.AF_UNIX) as probe:
        try:
            probe.connect(path)
        except OSError:
            return False
    return True


class _UnansweringFactory:
    """Start MCP servers that never finish their handshake, noting whether the API listened."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.listening_at_start: list[bool] = []

    def __call__(
        self, config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        self.listening_at_start.append(_listening(self.path))

        class Unanswering(ScriptedMCPClient):
            async def __aenter__(self) -> Self:
                await asyncio.Event().wait()
                return self

        return Unanswering(
            ScriptedMCPServer(name=config.server_id, discovery=_discovery()),
            credential,
            environment,
        )


async def _eventually(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0.02)


@dataclass(frozen=True, slots=True)
class _ServedAPI:
    """The real entry point serving a real app over the in-memory composition."""

    path: str
    serving: asyncio.Task[None]
    servers: list[uvicorn.Server]
    apps: list[Composition]

    def client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=self.path)
        return httpx.AsyncClient(transport=transport, base_url="http://api")

    def stop(self) -> None:
        self.servers[0].should_exit = True


@asynccontextmanager
async def _served_api(
    monkeypatch: pytest.MonkeyPatch, path: str, **options: Any
) -> AsyncIterator[_ServedAPI]:
    """Run ``_serve_api`` on a Unix socket; static tests may not open TCP sockets."""
    real_config = uvicorn.Config
    servers: list[uvicorn.Server] = []
    apps: list[Composition] = []

    @asynccontextmanager
    async def composition(**ignored: Any) -> AsyncIterator[Composition]:
        async with build(settings=_settings(), **options) as app:
            apps.append(app)
            yield app

    class Server(uvicorn.Server):
        def __init__(self, config: uvicorn.Config) -> None:
            super().__init__(config)
            servers.append(self)

    def loopback_app(*arguments: Any) -> Any:
        """Report the loopback peer DEV authentication admits; a Unix socket has none."""
        api = create_app(*arguments)

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            await api({**scope, "client": ("127.0.0.1", 0)}, receive, send)

        return app

    monkeypatch.setattr(main, "build", composition)
    monkeypatch.setattr(main, "create_app", loopback_app)
    monkeypatch.setattr(
        uvicorn, "Config", lambda app, **settings: real_config(app, **{**settings, "uds": path})
    )
    monkeypatch.setattr(uvicorn, "Server", Server)
    served = _ServedAPI(path, asyncio.create_task(main._serve_api()), servers, apps)
    try:
        await _eventually(lambda: _listening(path))
        yield served
    finally:
        await _eventually(lambda: bool(served.servers))
        served.stop()
        async with asyncio.timeout(15):
            await served.serving


async def test_the_api_answers_readiness_before_it_warms_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = f"{directory}/api.sock"
        factory = _UnansweringFactory(path)
        async with _served_api(
            monkeypatch, path, mcp_servers=(_server("slow"),), mcp_client_factory=factory
        ) as api:
            await _eventually(lambda: bool(factory.listening_at_start))
            async with api.client() as client:
                ready = await client.get("/health/ready")
            warm_up = api.apps[0].mcp._warmup_task
            assert warm_up is not None
            warming = not warm_up.done()

    assert ready.status_code == 200
    assert warming
    assert factory.listening_at_start == [True]


async def test_an_open_run_stream_holds_the_api_only_for_its_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "API_SHUTDOWN_GRACE_SECONDS", 1)
    with tempfile.TemporaryDirectory() as directory:
        async with _served_api(monkeypatch, f"{directory}/api.sock") as api:
            app = api.apps[0]
            session = await app.services.sessions.create(app.principal, "general", {})
            submitted = await app.services.runs.submit(
                app.principal, session.id, [TextContentBlock(text="hold")], None, None
            )
            async with app.uow_factory() as uow:
                run = await uow.runs.get(submitted.run_id, app.principal)

            async def still_running(
                reader: Principal, run_id: UUID, sequence: int
            ) -> tuple[Run, list[EventEnvelope]]:
                """Hold the stream open, as a run parked on an approval does."""
                return run.model_copy(update={"status": RunStatus.RUNNING}), []

            monkeypatch.setattr(app.services.runs, "_events_after", still_running)
            async with (
                api.client() as client,
                client.stream("GET", f"/v1/runs/{submitted.run_id}/events") as stream,
            ):
                assert stream.status_code == 200
                stopping = asyncio.get_running_loop().time()
                api.stop()
                async with asyncio.timeout(10):
                    await asyncio.shield(api.serving)
                stopped = asyncio.get_running_loop().time() - stopping

    assert stopped < 5


def test_the_grace_leaves_most_of_the_stop_timeout_to_teardown() -> None:
    unit = (ROOT / "deploy/systemd/veetbot-api.service").read_text()
    declared = re.search(r"^TimeoutStopSec=(\d+)s$", unit, re.MULTILINE)
    assert declared is not None
    stop_timeout = int(declared[1])

    assert 0 < main.API_SHUTDOWN_GRACE_SECONDS <= stop_timeout / 3
