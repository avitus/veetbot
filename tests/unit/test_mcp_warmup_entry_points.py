"""The API and the interactive worker warm MCP discovery; other roles never do (ADR-0131)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn

from agent_core.cli import main
from agent_core.cli.main import WorkerRole


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


async def test_the_api_starts_the_warm_up_before_it_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    composition = _Composition()
    _serve(monkeypatch, composition)
    warmups_when_serving: list[int] = []

    class Server:
        def __init__(self, config: object) -> None:
            del config

        async def serve(self) -> None:
            warmups_when_serving.append(composition.warmups)

    monkeypatch.setattr(main, "create_app", lambda *arguments: object())
    monkeypatch.setattr(uvicorn, "Config", lambda *arguments, **options: object())
    monkeypatch.setattr(uvicorn, "Server", Server)

    await main._serve_api()

    assert warmups_when_serving == [1]
