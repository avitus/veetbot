"""Milestone 29 end-to-end: the composed pass proposes, the owner resolves."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from agent_core.adapters.judgment import FakeJudgmentProvider
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.judgment import (
    ChoiceAnswer,
    ChoiceQuestion,
    JudgmentAnswer,
    JudgmentFailure,
    JudgmentProviderError,
    JudgmentRequest,
    JudgmentResult,
)
from agent_core.domain.messages import FakeModelScript, ModelUsage, ScriptedTurn
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.runtime.worker import MaintenanceWorker
from tests.integration.m2_support import memory_settings
from tests.unit import test_folder_judgment_matching as matching_contract

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


def _choosing(option: str, probability: float) -> Any:
    """A scripted judge that picks one option, by key, for every question it is asked."""

    def respond(request: JudgmentRequest) -> JudgmentResult:
        answers: dict[str, JudgmentAnswer] = {}
        for key, question in request.questions.items():
            assert isinstance(question, ChoiceQuestion)
            keys = [item.key for item in question.options]
            chosen = option if option in keys else "none"
            rest = (1.0 - probability) / (len(keys) - 1)
            answers[key] = ChoiceAnswer(
                choice=chosen,
                probabilities={item: probability if item == chosen else rest for item in keys},
                confidence=probability,
            )
        return JudgmentResult(
            answers=answers, usage=ModelUsage(input_tokens=300, provider="fake", model="scripted")
        )

    return respond


@asynccontextmanager
async def _matching_composition(tmp_path: Path, judge: FakeJudgmentProvider) -> Any:
    overlay = tmp_path / "folders" / "profiles.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "schema_version: 1\nproposals:\n  judgment_matching_enabled: true\n", encoding="utf-8"
    )
    settings = replace(memory_settings(), thread_folders_api_enabled=True, config_dir=tmp_path)
    script = FakeModelScript(
        turns=[ScriptedTurn(text=json.dumps({"groups": []}))], on_exhausted="repeat_last"
    )
    async with build(
        settings=settings,
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
        script=script,
        judgment_provider_override=judge,
    ) as composition:
        yield composition


async def _pass_audit(composition: Composition) -> dict[str, Any]:
    async with composition.uow_factory() as uow:
        events = await uow.process_events.list()
    for event in events:
        rendered = repr(event.payload).lower()
        assert "carburetor" not in rendered and "motorcycle" not in rendered
        assert "lisbon" not in rendered
    return dict([e for e in events if e.event_type == "folder.proposal.pass"][-1].payload)


async def test_judgment_matching(tmp_path: Path) -> None:
    """Gate 13: the matcher only proposes, is cleaned before egress, and falls back whole."""

    judge = FakeJudgmentProvider(_choosing("f0", 0.94))
    async with _matching_composition(tmp_path / "on", judge) as composition:
        async with _client(composition) as client:
            created = await client.post("/v1/folders", json={"name": "Motorcycle restoration"})
            assert created.status_code == 201, created.text
            folder_id = created.json()["id"]
            (filed,) = await _seed(composition, client, ["Rebuilding the front forks"])
            moved = await client.put(f"/v1/sessions/{filed}/folder", json={"folder_id": folder_id})
            assert moved.status_code == 200, moved.text
            (unfiled,) = await _seed(composition, client, ["Fixing the carburetor"], offset=5)

            worker = composition.maintenance_factory()
            assert isinstance(worker, MaintenanceWorker)
            await worker.run_once()

            # Only ever a proposal: nothing is filed until the owner accepts.
            index = (await client.get("/v1/sessions")).json()["items"]
            assert {row["folder_id"] for row in index if row["id"] == str(unfiled)} == {None}
            (proposal,) = (await client.get("/v1/folders/proposals")).json()["items"]
            assert proposal["kind"] == "add_to_folder"
            assert proposal["target_folder_id"] == folder_id
            assert proposal["member_session_ids"] == [str(unfiled)]
            accepted = await client.post(f"/v1/folders/proposals/{proposal['id']}/accept")
            assert accepted.status_code == 200, accepted.text
            index = (await client.get("/v1/sessions")).json()["items"]
            assert {row["folder_id"] for row in index if row["id"] == str(unfiled)} == {folder_id}

        audit = await _pass_audit(composition)
        assert audit["judgment_provider"] == "fake"
        assert audit["judgment_requests"] == 1
        assert audit["judgment_matched"] == 1
        assert audit["judgment_fallback_used"] is False
        # What left the process: cleaned text in named fields, opaque keys, no identifiers.
        (request,) = judge.requests
        assert request.state == {
            "conversation": {
                "title": "Fixing the carburetor",
                "first_message": "Fixing the carburetor.",
            }
        }
        assert folder_id not in request.model_dump_json()
        assert str(unfiled) not in request.model_dump_json()

    # A provider failure discards every judgment and leaves the inner grouping unchanged.
    failing = FakeJudgmentProvider(
        [JudgmentProviderError(JudgmentFailure.PROVIDER_UNAVAILABLE, retryable=True)]
    )
    async with _matching_composition(tmp_path / "failing", failing) as composition:
        async with _client(composition) as client:
            created = await client.post("/v1/folders", json={"name": "Motorcycle restoration"})
            assert created.status_code == 201, created.text
            lisbon = await _seed(composition, client, LISBON_TITLES)
            assert composition.folder_proposals is not None
            assert await composition.folder_proposals.run_once() == 1
            (proposal,) = (await client.get("/v1/folders/proposals")).json()["items"]
            assert proposal["kind"] == "new_folder"
            assert proposal["proposed_name"] == "Lisbon Trip"
            assert set(proposal["member_session_ids"]) == {str(item) for item in lisbon}
        audit = await _pass_audit(composition)
        assert audit["judgment_fallback_used"] is True
        assert audit["judgment_error_class"] == "judgment.provider_unavailable"
        assert audit["judgment_matched"] == 0
        assert len(failing.requests) == 1

    await matching_contract.test_no_match_and_a_match_below_the_threshold_add_nothing()
    await matching_contract.test_a_conversation_with_a_hazardous_title_is_never_judged()
    await matching_contract.test_hazardous_snippets_member_titles_and_folders_are_omitted()
    await matching_contract.test_a_late_failure_discards_the_judgments_already_made()
    await matching_contract.test_a_deadline_discards_every_judgment()
    await matching_contract.test_a_cost_breach_discards_every_judgment()
    await matching_contract.test_placed_conversations_leave_inner_additions()
