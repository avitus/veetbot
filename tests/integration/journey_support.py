"""Shared plumbing for end-to-end journeys over the HTTP API and PostgreSQL.

A journey builds the real composition on the disposable database, calls the
public routes through the ASGI application the way a client would, and drives
the durable worker by hand so every step is deterministic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from agent_core.api import create_app
from agent_core.bootstrap import Composition
from agent_core.domain.agents import Principal
from agent_core.runtime.worker import DurableWorker


@asynccontextmanager
async def api_client(
    composition: Composition, *, principal: Principal | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """Serve the composition's public API in process, as ``principal`` when given."""

    app = create_app(
        composition.services,
        composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(
        app=app, client=("127.0.0.1", 43105), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def run_worker_once(composition: Composition, worker_id: str) -> bool:
    """Let one durable worker claim and execute at most one queued run."""

    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=composition.clock,
        worker_id=worker_id,
    )
    return await worker.run_once()
