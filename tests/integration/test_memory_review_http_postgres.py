"""The owner corrects and deletes memories over HTTP, and the next chat obeys, on PostgreSQL.

Guards docs/plan/memory-read-api-and-browser.md "Writes" (ADR-0117, ADR-0224
part A): `POST /v1/memories/{id}/review` with `changed` supersedes a belief
with the owner's words at user authority and keeps the predecessor readable; a
stale `expected_updated_at` is a conflict that changes nothing; `DELETE
/v1/memories/{id}` tombstones the statement so it cannot re-form; a repeated
`Idempotency-Key` replays the recorded result, including after a restart; and
both writes need the exact scope `memory.write`. The in-memory gates prove each
route alone. This journey earns its beliefs from real chats through idle
consolidation, writes through the routes, restarts the composition, and then
checks the two consequences only the whole system can show: the next chat's
first model request carries the corrected fact and neither discarded one, and
restating the deleted fact forms nothing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.worker import MaintenanceWorker
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
CAR_MESSAGE = "I drive a BMW X3 to work every day."
WATCH_MESSAGE = "I wear an Apple Watch on every run."
CORRECTED = "The owner drives a Rivian R1S to work every day."
CEILING = {"ceiling": "restricted"}


def _owner(composition: Composition, *, write: bool = True) -> Principal:
    scopes = set(PLATFORM_SCOPES) | {"memory.read"}
    return Principal(
        tenant_id=composition.principal.tenant_id,
        principal_id=composition.principal.principal_id,
        roles={"user"},
        scopes=scopes | {"memory.write"} if write else scopes - {"memory.write"},
    )


async def _chat_then_consolidate(
    composition: Composition, clock: FixedClock, message: str, worker_id: str
) -> UUID:
    """Run one chat and let idle consolidation form whatever it implies."""
    run_id = await composition.runs.submit(message)
    assert await run_worker_once(composition, worker_id)
    run = await composition.runs.get(run_id)
    assert run.status is RunStatus.COMPLETED, run.failure
    async with composition.uow_factory() as uow:
        events = await uow.events.list_after(run.session_id, 0, composition.principal)
    requested = next(event for event in events if event.event_type == "memory.formation.requested")
    clock.advance(datetime.fromisoformat(str(requested.payload["not_before"])) - clock.now())
    await cast(MaintenanceWorker, composition.maintenance_factory()).run_once()
    return run.session_id


async def _live(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    listing = await client.get("/v1/memories", params=CEILING)
    assert listing.status_code == 200, listing.text
    items: list[dict[str, Any]] = listing.json()["items"]
    return items


def _about(items: list[dict[str, Any]], words: str) -> dict[str, Any]:
    matches = [item for item in items if words in item["statement"]]
    assert len(matches) == 1, [item["statement"] for item in items]
    return matches[0]


async def test_owner_correction_and_deletion_reach_the_next_chat_and_block_reformation() -> None:
    """Review `changed`, delete, restart, replay, recall, and a refused re-formation."""
    settings = replace(database_settings(), memory_api_enabled=True)
    clock = FixedClock(NOW)
    first_script = FakeModelScript(
        turns=[ScriptedTurn(text="Noted, thanks."), ScriptedTurn(text="Good to know.")]
    )
    async with build(
        settings=settings, storage="postgres", script=first_script, clock=clock
    ) as app:
        await _chat_then_consolidate(app, clock, CAR_MESSAGE, "memory-review-car")
        await _chat_then_consolidate(app, clock, WATCH_MESSAGE, "memory-review-watch")
        async with api_client(app, principal=_owner(app)) as client:
            formed = await _live(client)
            car = _about(formed, "BMW X3")
            watch = _about(formed, "Apple Watch")

            # A stale read loses and nothing changes.
            stale = await client.post(
                f"/v1/memories/{car['id']}/review",
                params=CEILING,
                headers={"Idempotency-Key": "stale-correction"},
                json={
                    "outcome": "changed",
                    "replacement_statement": "A stale correction.",
                    "expected_updated_at": "2020-01-01T00:00:00Z",
                },
            )
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"]["code"] == "conflict"
            assert _about(await _live(client), "BMW X3")["id"] == car["id"]

            correction = {
                "outcome": "changed",
                "replacement_statement": CORRECTED,
                "expected_updated_at": car["updated_at"],
            }
            changed = await client.post(
                f"/v1/memories/{car['id']}/review",
                params=CEILING,
                headers={"Idempotency-Key": "correct-the-car"},
                json=correction,
            )
            assert changed.status_code == 200, changed.text
            assert changed.headers["cache-control"] == "private, no-store"
            replacement = changed.json()
            assert replacement["id"] != car["id"]
            assert replacement["statement"] == CORRECTED
            assert replacement["authority"] == "user"
            assert replacement["status"] == "active"
            for inherited in ("subject", "scope", "belief_type", "portability", "sensitivity"):
                assert replacement[inherited] == car[inherited], inherited

            deleted = await client.delete(
                f"/v1/memories/{watch['id']}",
                params=CEILING,
                headers={"Idempotency-Key": "forget-the-watch"},
            )
            assert deleted.status_code == 204, deleted.text

        async with api_client(app, principal=_owner(app, write=False)) as reader:
            forbidden = await reader.delete(
                f"/v1/memories/{replacement['id']}",
                params=CEILING,
                headers={"Idempotency-Key": "reader-delete"},
            )
            assert forbidden.status_code == 403, forbidden.text
            assert forbidden.json()["error"]["code"] == "authorization_error"

    second_script = FakeModelScript(
        turns=[
            ScriptedTurn(text="You drive a Rivian R1S.", context_contains="Rivian R1S"),
            ScriptedTurn(text="Understood."),
        ]
    )
    async with build(
        settings=settings, storage="postgres", script=second_script, clock=clock
    ) as restarted:
        async with api_client(restarted, principal=_owner(restarted)) as client:
            # Both receipts survived the restart: the same keys replay, and
            # neither write is applied a second time.
            replayed = await client.post(
                f"/v1/memories/{car['id']}/review",
                params=CEILING,
                headers={"Idempotency-Key": "correct-the-car"},
                json=correction,
            )
            assert replayed.status_code == 200, replayed.text
            assert replayed.json() == replacement
            reused = await client.post(
                f"/v1/memories/{car['id']}/review",
                params=CEILING,
                headers={"Idempotency-Key": "correct-the-car"},
                json={"outcome": "untrue"},
            )
            assert reused.status_code == 409, reused.text
            again = await client.delete(
                f"/v1/memories/{watch['id']}",
                params=CEILING,
                headers={"Idempotency-Key": "forget-the-watch"},
            )
            assert again.status_code == 204, again.text
            fresh = await client.delete(
                f"/v1/memories/{watch['id']}",
                params=CEILING,
                headers={"Idempotency-Key": "forget-the-watch-again"},
            )
            assert fresh.status_code == 404, fresh.text

            predecessor = await client.get(f"/v1/memories/{car['id']}", params=CEILING)
            assert predecessor.status_code == 200, predecessor.text
            assert predecessor.json()["status"] == "superseded"
            assert predecessor.json()["superseded_by"] == replacement["id"]
            assert predecessor.json()["valid_to"] is not None
            gone = await client.get(f"/v1/memories/{watch['id']}", params=CEILING)
            assert gone.status_code == 404, gone.text
            assert [item["statement"] for item in await _live(client)] == [CORRECTED]

            # The next chat's first model request already carries the correction
            # (the scripted turn matches only if it does) and recall returned
            # neither the superseded belief nor the deleted one.
            created = await client.post(
                "/v1/sessions", json={"agent_id": "general", "metadata": {}}
            )
            assert created.status_code == 201, created.text
            session_id = UUID(created.json()["id"])
            asked = await client.post(
                f"/v1/sessions/{session_id}/messages",
                headers={"Idempotency-Key": "ask-about-the-car"},
                json={"content": [{"type": "text", "text": "What car do I drive?"}]},
            )
            assert asked.status_code == 202, asked.text
            run_id = UUID(asked.json()["run_id"])
            assert await run_worker_once(restarted, "memory-review-recall")
            answered = await client.get(f"/v1/runs/{run_id}")
            assert answered.json()["status"] == RunStatus.COMPLETED.value, answered.text
            transcript = await client.get(f"/v1/sessions/{session_id}/messages")
            assert transcript.json()["items"][-1]["content"] == [
                {"type": "text", "text": "You drive a Rivian R1S."}
            ]
            recalled = [
                event
                for event in await restarted.runs.events(run_id)
                if event.event_type == "memory.recalled"
            ]
            assert recalled, "the chat recalled nothing"
            returned = {belief for event in recalled for belief in event.payload["returned"]}
            assert replacement["id"] in returned
            assert not returned & {car["id"], watch["id"]}

            # Deletion tombstones the statement: saying it again forms nothing.
            await _chat_then_consolidate(
                restarted, clock, WATCH_MESSAGE, "memory-review-watch-again"
            )
            after = await _live(client)
            assert not [item for item in after if "Apple Watch" in item["statement"]], after
            assert _about(after, "Rivian R1S")["id"] == replacement["id"]

        async with restarted.uow_factory() as uow:
            car_events = await uow.events.list_after(
                UUID(car["source_session_id"]), 0, restarted.principal
            )
            watch_events = await uow.events.list_after(
                UUID(watch["source_session_id"]), 0, restarted.principal
            )
    corrections = [event for event in car_events if event.event_type == "memory.owner_correction"]
    assert len(corrections) == 1, [event.event_type for event in car_events]
    assert [event.event_type for event in watch_events].count("memory.deleted") == 1
    receipts = [
        event
        for event in [*car_events, *watch_events]
        if event.event_type in {"memory.review_completed", "memory.delete_completed"}
    ]
    assert len(receipts) == 2
    for receipt in receipts:
        assert CORRECTED not in str(receipt.payload)
        assert "Apple Watch" not in str(receipt.payload)
