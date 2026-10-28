"""Owner records in a chat run with the shipped default agent, on PostgreSQL.

docs/plan/owner-records.md, "The six tools", "Classification", "Approval by
class" and the deferral note under "Decisions": the default agent defines
`records.list`, `records.query` and `records.append` and defers
`records.create_series` and `records.delete` to the index; a create or delete
never executes before its approval resolves, a denied approval writes
nothing, and `list`, `query` and `append` run without a card.

Guards, through the public API and the durable worker together, the
production fixes the gate and contract partitions reach only in isolation:
the default agent enabling the tools at all (``ac1af574``, when the owner's
blood-pressure request ended in a CSV because no records tool was callable),
six-place value canonicalization (``a1738ee0``) and the normalized series
name (``d5b1b037``). Day-precision instants (``351293fb``, ``7926c1e5``) are
exact-precision here and stay with the gate partition.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from agent_core.adapters.determinism import FixedClock, SystemClock
from agent_core.bootstrap import Composition, build
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.records import Order, Window, canonical_value
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings

COMPONENTS = [{"name": "systolic", "unit": "mmHg"}, {"name": "diastolic", "unit": "mmHg"}]
READING = {"systolic": 120, "diastolic": 80}


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
        app=app, client=("127.0.0.1", 43105), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _run_worker(composition: Composition, worker_id: str) -> None:
    """Run one claim whose heartbeat waits use real time.

    FixedClock.sleep returns at once, so heartbeats on the domain clock
    exhaust a tool's deadline during database I/O; domain time stays fixed.
    """
    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=SystemClock(),
        worker_id=worker_id,
    )
    assert await worker.run_once()


def _deferred(name: str, arguments: dict[str, Any], call_id: str) -> ScriptedToolCall:
    """A call to an index entry goes through `tool.call` (ADR-0123)."""
    return ScriptedToolCall(
        name="tool.call", arguments={"name": name, "arguments": arguments}, call_id=call_id
    )


def _calls(*calls: ScriptedToolCall) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=list(calls), stop_reason=StopReason.TOOL_USE)


async def _send(client: httpx.AsyncClient, session_id: UUID, text: str, key: str) -> UUID:
    submitted = await client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"Idempotency-Key": key},
        json={"content": [{"type": "text", "text": text}]},
    )
    assert submitted.status_code == 202, submitted.text
    return UUID(submitted.json()["run_id"])


async def _resolve_pending(
    client: httpx.AsyncClient, run_id: UUID, tool_name: str, decision: str
) -> None:
    listing = await client.get("/v1/approvals", params={"run_id": str(run_id)})
    assert listing.status_code == 200, listing.text
    [approval] = listing.json()["items"]
    assert approval["tool_name"] == tool_name
    resolved = await client.post(
        f"/v1/approvals/{approval['id']}/resolve", json={"decision": decision}
    )
    assert resolved.status_code == 200, resolved.text


async def _approved_tool_names(
    client: httpx.AsyncClient, composition: Composition, run_id: UUID
) -> list[str]:
    """The tools behind every `approval.requested` event of the run."""
    names: list[str] = []
    for event in await composition.runs.events(run_id):
        if event.event_type == "approval.requested":
            read = await client.get(f"/v1/approvals/{event.payload['approval_id']}")
            assert read.status_code == 200, read.text
            names.append(read.json()["tool_name"])
    return names


def _names(events: list[Any], event_type: str) -> list[str]:
    return [str(event.payload["name"]) for event in events if event.event_type == event_type]


async def test_default_agent_tracks_a_series_through_the_api_and_the_worker() -> None:
    script = FakeModelScript(
        turns=[
            _calls(
                _deferred(
                    "records.create_series",
                    {"series": "Blood pressure", "components": COMPONENTS},
                    "g1-create",
                ),
            ),
            _calls(
                ScriptedToolCall(
                    name="records.append",
                    arguments={"series": "blood pressure", "values": READING},
                    call_id="g1-append",
                ),
            ),
            _calls(
                ScriptedToolCall(
                    name="records.query",
                    arguments={"series": "Blood pressure"},
                    call_id="g1-query",
                ),
            ),
            ScriptedTurn(text="Your blood pressure is 120 over 80."),
            _calls(
                ScriptedToolCall(name="records.list", arguments={}, call_id="g1-list"),
            ),
            _calls(
                _deferred("records.delete", {"series": "Blood pressure"}, "g1-delete"),
            ),
            ScriptedTurn(text="I left your blood pressure series in place."),
        ]
    )
    settings = replace(database_settings(), records_enabled=True)
    async with (
        build(
            settings=settings, storage="postgres", script=script, clock=FixedClock(NOW)
        ) as composition,
        _client(composition) as client,
    ):
        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])

        # First message: declare (approved over HTTP), append, query, answer.
        first = await _send(
            client, session_id, "Start tracking my blood pressure, 120 over 80 now.", "g1-first"
        )
        await _run_worker(composition, "g1-parker")
        parked = await client.get(f"/v1/runs/{first}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.text
        async with composition.uow_factory() as uow:
            assert (
                await uow.records.list_series(composition.principal, Sensitivity.RESTRICTED) == []
            )
        await _resolve_pending(client, first, "records.create_series", "approve_once")
        await _run_worker(composition, "g1-resumer")

        first_run = await composition.runs.get(first)
        assert first_run.status is RunStatus.COMPLETED, first_run.failure
        assert first_run.final_message == "Your blood pressure is 120 over 80."
        first_events = await composition.runs.events(first)
        assert _names(first_events, "tool.call.failed") == []
        assert _names(first_events, "tool.call.completed") == [
            "records.create_series",
            "records.append",
            "records.query",
        ]
        assert await _approved_tool_names(client, composition, first) == ["records.create_series"]

        async with composition.uow_factory() as uow:
            [summary] = await uow.records.list_series(composition.principal, Sensitivity.RESTRICTED)
            entries = await uow.records.query(
                composition.principal,
                summary.series.id,
                Window(until=NOW + timedelta(seconds=1)),  # half-open [since, until)
                Order.DESC,
                10,
            )
        assert summary.series.display_name == "Blood pressure"
        assert summary.series.name == "blood pressure"
        assert [c.name for c in summary.series.components] == ["systolic", "diastolic"]
        assert summary.entry_count == 1
        [entry] = entries
        assert entry.values == {"systolic": Decimal("120"), "diastolic": Decimal("80")}
        assert {k: canonical_value(v) for k, v in entry.values.items()} == {
            "systolic": "120.000000",
            "diastolic": "80.000000",
        }
        assert entry.observed_at == NOW
        assert entry.source_session_id == session_id

        # The shipped roster: reads and the append are definitions, create
        # and delete wait in the index.
        plan = await composition.executor._context_planner.current(session_id)
        assert plan is not None
        assert {"records.append", "records.query", "records.list"} <= set(plan.tool_names)
        assert {"records.create_series", "records.delete"} <= set(plan.deferred_tool_names)
        assert not {"records.create_series", "records.delete"} & set(plan.tool_names)

        # Second message in the same chat: list, then a delete the owner denies.
        second = await _send(client, session_id, "What am I tracking? Drop it.", "g1-second")
        await _run_worker(composition, "g1-lister")
        parked = await client.get(f"/v1/runs/{second}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.text
        await _resolve_pending(client, second, "records.delete", "deny")
        await _run_worker(composition, "g1-denied")

        second_run = await composition.runs.get(second)
        assert second_run.status is RunStatus.COMPLETED, second_run.failure
        assert second_run.final_message == "I left your blood pressure series in place."
        second_events = await composition.runs.events(second)
        assert _names(second_events, "tool.call.failed") == []
        assert _names(second_events, "tool.call.completed") == ["records.list"]
        assert _names(second_events, "tool.call.denied") == ["records.delete"]
        assert await _approved_tool_names(client, composition, second) == ["records.delete"]

        async with composition.uow_factory() as uow:
            [kept] = await uow.records.list_series(composition.principal, Sensitivity.RESTRICTED)
            assert kept.series.id == summary.series.id
            assert await uow.records.count_entries(composition.principal, kept.series.id) == 1
