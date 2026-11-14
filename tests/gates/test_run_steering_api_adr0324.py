"""A follow-up to a working run is held for that run, not refused (ADR-0324)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx

from agent_core.bootstrap import Composition, build
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
)
from tests.gates.test_api_m5 import _client, _create_session, _settings

PARKS_ON_APPROVAL = FakeModelScript(
    turns=[
        ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name="demo.external_write",
                    arguments={"destination": "demo", "content": "hello"},
                )
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
)


def _text(text: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}]}


@asynccontextmanager
async def _parked(
    tmp_path: Path, *, steering: bool
) -> AsyncIterator[tuple[Composition, httpx.AsyncClient, UUID, UUID]]:
    """Yield a chat whose only run is parked on an approval card."""

    settings = replace(_settings(tmp_path), run_steering_enabled=steering)
    async with (
        build(settings=settings, sequential_ids=True, script=PARKS_ON_APPROVAL) as composition,
        _client(composition) as client,
    ):
        session_id = await _create_session(client)
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages", json=_text("Write the note.")
        )
        assert submitted.status_code == 202
        # A new run's body is unchanged by the proposal.
        assert set(submitted.json()) == {"run_id", "status"}
        run_id = UUID(submitted.json()["run_id"])
        parked = await client.get(f"/v1/runs/{run_id}")
        assert parked.json()["status"] == "WAITING_FOR_APPROVAL"
        assert parked.json()["pending_inputs"] == []
        yield composition, client, session_id, run_id


async def test_steering_off_keeps_the_active_run_conflict(tmp_path: Path) -> None:
    async with _parked(tmp_path, steering=False) as (composition, client, session_id, run_id):
        refused = await client.post(
            f"/v1/sessions/{session_id}/messages", json=_text("Also add a date.")
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["details"] == {
            "reason": "active_run_exists",
            "run_id": str(run_id),
            "run_status": "WAITING_FOR_APPROVAL",
        }
        async with composition.uow_factory() as uow:
            assert await uow.pending_inputs.list_for_run(run_id, composition.principal) == []


async def test_a_follow_up_is_held_for_the_active_run(tmp_path: Path) -> None:
    async with _parked(tmp_path, steering=True) as (composition, client, session_id, run_id):
        before = await composition.runs.events(run_id)
        held = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json=_text("Also add a date."),
            headers={"Idempotency-Key": "follow-up-1"},
        )
        assert held.status_code == 202
        body = held.json()
        assert body["run_id"] == str(run_id)
        assert body["status"] == "WAITING_FOR_APPROVAL"
        pending_input_id = UUID(body["pending_input_id"])
        async with composition.uow_factory() as uow:
            inbox = await uow.pending_inputs.list_for_run(run_id, composition.principal)
            assert await uow.runs.active_for_session(session_id, composition.principal)
        assert [(item.id, item.content) for item in inbox] == [
            (pending_input_id, [TextPart(text="Also add a date.")])
        ]
        # Holding a follow-up neither answers the card nor writes to the run's log.
        view = (await client.get(f"/v1/runs/{run_id}")).json()
        assert view["status"] == "WAITING_FOR_APPROVAL"
        # A second device reads what is waiting from the run itself.
        assert view["pending_inputs"] == [
            {
                "id": str(pending_input_id),
                "content": [{"type": "text", "text": "Also add a date."}],
                "created_at": inbox[0].created_at.isoformat().replace("+00:00", "Z"),
            }
        ]
        assert await composition.runs.events(run_id) == before

        replayed = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json=_text("Also add a date."),
            headers={"Idempotency-Key": "follow-up-1"},
        )
        assert replayed.status_code == 200
        assert replayed.json() == body
        reused = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json=_text("Something else."),
            headers={"Idempotency-Key": "follow-up-1"},
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["details"]["reason"] == "idempotency_key_reused"
        async with composition.uow_factory() as uow:
            assert len(await uow.pending_inputs.list_for_run(run_id, composition.principal)) == 1


async def test_a_ninth_pending_follow_up_is_refused(tmp_path: Path) -> None:
    async with _parked(tmp_path, steering=True) as (_composition, client, session_id, run_id):
        for number in range(8):
            held = await client.post(
                f"/v1/sessions/{session_id}/messages", json=_text(f"Follow-up {number}.")
            )
            assert held.status_code == 202
        refused = await client.post(
            f"/v1/sessions/{session_id}/messages", json=_text("One too many.")
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["details"] == {
            "reason": "pending_input_limit",
            "run_id": str(run_id),
        }


async def test_a_follow_up_is_text_only(tmp_path: Path) -> None:
    async with _parked(tmp_path, steering=True) as (composition, client, session_id, run_id):
        refused = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={
                "content": [
                    {"type": "text", "text": "See this."},
                    {
                        "type": "image",
                        "artifact_id": str(UUID(int=77)),
                        "media_type": "image/png",
                    },
                ]
            },
        )
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "malformed_request"
        async with composition.uow_factory() as uow:
            assert await uow.pending_inputs.list_for_run(run_id, composition.principal) == []
