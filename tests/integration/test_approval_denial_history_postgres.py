"""A denied approval leaves its session open to the next message (PostgreSQL).

Found on 2026-09-29: the approval-denied path appended ``tool.call.denied``
without a ``result_item``, so the session-history projection raised for every
later submission in that session and ``POST /v1/sessions/{id}/messages``
answered 500. Production sessions already hold such events, so the second case
replays one in its stored legacy shape.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

import httpx
from sqlalchemy import select, update

from agent_core.adapters.persistence.sqlalchemy_models import EventRow
from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import Composition, build
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolResultItem,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings

CALL_ID = "denied-then-continued"


def _script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "never written"},
                        call_id=CALL_ID,
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Not written.", stop_reason=StopReason.END_TURN),
            ScriptedTurn(text="Second answer.", stop_reason=StopReason.END_TURN),
        ]
    )


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    from agent_core.api import create_app

    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(
        app=app,
        client=("127.0.0.1", 43105),
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _run_worker(composition: Composition, worker_id: str) -> None:
    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=composition.clock,
        worker_id=worker_id,
    )
    assert await worker.run_once()


async def _deny_first_turn(composition: Composition, client: httpx.AsyncClient) -> UUID:
    created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert created.status_code == 201, created.text
    session_id = UUID(created.json()["id"])
    submitted = await client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": [{"type": "text", "text": "record an external write"}]},
    )
    assert submitted.status_code == 202, submitted.text
    run_id = UUID(submitted.json()["run_id"])

    await _run_worker(composition, "denial-parker")
    parked = await client.get(f"/v1/runs/{run_id}")
    assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value
    listing = await client.get(f"/v1/approvals?run_id={run_id}")
    approval = listing.json()["items"][0]
    resolved = await client.post(
        f"/v1/approvals/{approval['id']}/resolve", json={"decision": "deny"}
    )
    assert resolved.status_code == 200, resolved.text

    await _run_worker(composition, "denial-resumer")
    completed = await client.get(f"/v1/runs/{run_id}")
    assert completed.json()["status"] == RunStatus.COMPLETED.value
    return session_id


async def _continue_session(
    composition: Composition, client: httpx.AsyncClient, session_id: UUID
) -> list[ToolResultItem]:
    """Post the next message, run it, and return the denied call's seeded result."""

    submitted = await client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": [{"type": "text", "text": "then answer this"}]},
    )
    assert submitted.status_code == 202, submitted.text
    run_id = UUID(submitted.json()["run_id"])
    await _run_worker(composition, "denial-continuation")
    completed = await composition.runs.get(run_id)
    assert completed.status is RunStatus.COMPLETED
    assert completed.final_message == "Second answer."
    messages = await client.get(f"/v1/sessions/{session_id}/messages")
    assert messages.status_code == 200, messages.text
    async with composition.uow_factory() as uow:
        history = await uow.history.read(session_id)
    return [
        item
        for item in history.items
        if isinstance(item, ToolResultItem) and item.call_id == CALL_ID
    ]


async def test_denied_approval_leaves_the_session_open_to_the_next_message() -> None:
    async with (
        build(settings=database_settings(), storage="postgres", script=_script()) as composition,
        _client(composition) as client,
    ):
        session_id = await _deny_first_turn(composition, client)
        async with composition.uow_factory() as uow:
            denied = [
                event
                for event in await uow.events.list_after(session_id, 0, composition.principal)
                if event.event_type == "tool.call.denied"
            ]
        seeded = await _continue_session(composition, client, session_id)

    assert len(denied) == 1
    assert denied[0].payload["reason_code"] == "approval.denied"
    assert denied[0].payload["result_item"]["call_id"] == CALL_ID
    assert len(seeded) == 1
    assert seeded[0].is_error
    assert seeded[0].trust is TrustLevel.INTERNAL_TOOL  # demo.external_write's output trust
    assert seeded[0].model_dump(mode="json", exclude={"source_event_sequence"}) == (
        ToolResultItem.model_validate(denied[0].payload["result_item"]).model_dump(
            mode="json", exclude={"source_event_sequence"}
        )
    )


async def test_a_stored_legacy_denial_without_a_result_item_still_projects() -> None:
    """Replay the shape every approval denial stored before the repair."""

    async with (
        build(settings=database_settings(), storage="postgres", script=_script()) as composition,
        _client(composition) as client,
    ):
        session_id = await _deny_first_turn(composition, client)
        async with composition.uow_factory() as uow:
            database_session = cast(PostgresUnitOfWork, uow)._session
            assert database_session is not None
            row = (
                await database_session.scalars(
                    select(EventRow).where(
                        EventRow.session_id == session_id,
                        EventRow.event_type == "tool.call.denied",
                    )
                )
            ).one()
            legacy = {
                "name": "demo.external_write",
                "call_id": CALL_ID,
                "reason_code": "approval.denied",
            }
            await database_session.execute(
                update(EventRow)
                .where(EventRow.id == row.id)
                .values(payload=legacy, payload_schema_version=1)
            )
        seeded = await _continue_session(composition, client, session_id)

    assert len(seeded) == 1
    assert seeded[0].is_error
    # The legacy event never recorded the tool's output trust, so the projection
    # labels the item with the least-trusted level rather than guessing one.
    assert seeded[0].trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert len(seeded[0].content) == 1
    assert isinstance(seeded[0].content[0], TextPart)
    assert json.loads(seeded[0].content[0].text) == {
        "status": "denied",
        "action": "demo.external_write",
        "reason_code": "approval.denied",
    }
