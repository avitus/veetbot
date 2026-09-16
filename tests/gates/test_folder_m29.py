"""Milestone 29 end-to-end: the composed pass proposes, the owner resolves."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx

from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.runtime.worker import MaintenanceWorker
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)
TENANT = "local"
PRINCIPAL_ID = "local-user"
LISBON_TITLES = [
    "Lisbon trip flights",
    "Lisbon trip hotel booking",
    "Lisbon trip itinerary museums",
    "Lisbon trip restaurant list",
]
KITCHEN_TITLES = [
    "Kitchen renovation budget",
    "Kitchen renovation contractor quotes",
    "Kitchen renovation tile choices",
    "Kitchen renovation lighting plan",
]


def _principal() -> Principal:
    return Principal(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        roles={"user"},
        scopes={"session.read", "session.write", "run.write"},
    )


@asynccontextmanager
async def _composition() -> Any:
    settings = replace(memory_settings(), thread_folders_api_enabled=True)
    script = FakeModelScript(
        turns=[ScriptedTurn(text=json.dumps({"groups": []}))], on_exhausted="repeat_last"
    )
    async with build(
        settings=settings,
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
        script=script,
    ) as composition:
        yield composition


@asynccontextmanager
async def _client(composition: Composition) -> Any:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _seed(
    composition: Composition,
    client: httpx.AsyncClient,
    titles: list[str],
    *,
    offset: int = 0,
) -> list[UUID]:
    """Seed titled chat sessions with a first owner message, as a run would leave them."""

    created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert created.status_code == 201, created.text
    template = created.json()
    ids: list[UUID] = []
    async with composition.uow_factory() as uow:
        for number, title in enumerate(titles, start=offset):
            session_id = uuid4()
            await uow.sessions.create(
                Session(
                    id=session_id,
                    tenant_id=TENANT,
                    principal_id=PRINCIPAL_ID,
                    agent_id=UUID(template["agent_id"]),
                    agent_version=template["agent_version"],
                    status=SessionStatus.ACTIVE,
                    title=title,
                    created_at=NOW + timedelta(seconds=number),
                    updated_at=NOW + timedelta(seconds=number),
                )
            )
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    payload_schema_version=1,
                    actor_type="principal",
                    actor_id=PRINCIPAL_ID,
                    payload={"content": f"{title}."},
                )
            )
            ids.append(session_id)
    return ids


async def test_the_composed_pass_proposes_and_the_owner_resolves() -> None:
    async with _composition() as composition, _client(composition) as client:
        assert composition.folder_proposals is not None
        lisbon = await _seed(composition, client, LISBON_TITLES)
        worker = composition.maintenance_factory()
        assert isinstance(worker, MaintenanceWorker)
        await worker.run_once()
        proposals = (await client.get("/v1/folders/proposals")).json()["items"]
        assert [row["proposed_name"] for row in proposals] == ["Lisbon Trip"]
        assert set(proposals[0]["member_session_ids"]) == {str(item) for item in lisbon}

        accepted = await client.post(f"/v1/folders/proposals/{proposals[0]['id']}/accept")
        assert accepted.status_code == 200, accepted.text
        folder_id = accepted.json()["resulting_folder_id"]
        index = (await client.get("/v1/sessions")).json()["items"]
        assert {row["folder_id"] for row in index if UUID(row["id"]) in lisbon} == {folder_id}
        folders = (await client.get("/v1/folders")).json()["items"]
        assert [(row["name"], row["thread_count"]) for row in folders] == [("Lisbon Trip", 4)]

        # A second group is proposed, declined, and never offered again.
        kitchen = await _seed(composition, client, KITCHEN_TITLES, offset=10)
        assert await composition.folder_proposals.run_once() == 1
        (kitchen_proposal,) = (await client.get("/v1/folders/proposals")).json()["items"]
        assert set(kitchen_proposal["member_session_ids"]) == {str(item) for item in kitchen}
        declined = await client.post(f"/v1/folders/proposals/{kitchen_proposal['id']}/decline")
        assert declined.status_code == 200
        assert await composition.folder_proposals.run_once() == 0
        assert (await client.get("/v1/folders/proposals")).json()["items"] == []

        # Deleting a conversation unfiles it; deleting the folder unfiles the rest.
        assert (await client.delete(f"/v1/sessions/{lisbon[0]}")).status_code == 204
        assert (await client.get(f"/v1/folders/{folder_id}")).json()["thread_count"] == 3
        assert (await client.delete(f"/v1/folders/{folder_id}")).status_code == 204
        index = (await client.get("/v1/sessions")).json()["items"]
        assert all(row["folder_id"] is None for row in index)

        async with composition.uow_factory() as uow:
            events = await uow.process_events.list()
        pass_events = [event for event in events if event.event_type == "folder.proposal.pass"]
        assert pass_events, "the pass records a content-free audit event"
        for event in events:
            assert "Lisbon" not in repr(event.payload) and "Kitchen" not in repr(event.payload)


async def test_the_pass_is_absent_without_the_flag() -> None:
    async with build(settings=memory_settings(), storage="memory", sequential_ids=True) as off:
        assert off.folder_proposals is None
