"""People tools inside a chat run on PostgreSQL (people-and-relationships.md).

Covers the "Agent tools" section (`people-and-relationships.md:594-606`): the
three read-only People tools inside an ordinary chat run, their
`people.context.used` influence rows under the owner fence
(`people-and-relationships.md:1500-1506`), the public history, relationship and
fact routes (`people-and-relationships.md:684`), and formation of an owner-stated
relative from a chat message (`people-and-relationships.md:932-933`).

Production fixes guarded: ea6c4bfa (a relationship or interaction ref passed to
`people.context` as a person id is a correctable `people.not_found` tool failure
with `modify_arguments` remediation, never an internal_error run crash; the
platform message steers the model to `people.search`).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.people import PublicPeopleService
from agent_core.bootstrap import build
from agent_core.domain.memory import BeliefType, MemoryAuthority, Sensitivity
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.people import (
    InteractionParticipant,
    PeopleEndpoint,
    PeopleInteraction,
    PeopleSource,
    RelationshipAssertion,
)
from agent_core.domain.people_views import CreatePerson
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.memory_fixtures import memory
from tests.integration.m2_support import database_settings
from tests.integration.test_golden_journeys import (
    _client,
    _run_worker,
    _run_worker_on_wall_clock,
    _tool_names,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)
CEILING = {"ceiling": "sensitive"}


def _settings() -> Any:
    return replace(database_settings(), people_enabled=True)


async def _new_chat(client: Any) -> UUID:
    created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert created.status_code == 201, created.text
    return UUID(created.json()["id"])


async def _say(client: Any, session_id: UUID, text: str, key: str) -> UUID:
    submitted = await client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"Idempotency-Key": key},
        json={"content": [{"type": "text", "text": text}]},
    )
    assert submitted.status_code == 202, submitted.text
    return UUID(submitted.json()["run_id"])


async def test_postgres_people_tools_in_a_chat_run_recover_from_a_non_person_id() -> None:
    """The run survives a relationship ref in people.context and finishes with
    the person's context, its influence rows and the person's chat history."""
    clock = FixedClock(NOW)
    async with build(settings=_settings(), storage="postgres", clock=clock) as api:
        owner = api.principal
        async with _client(api) as client:
            session_id = await _new_chat(client)
        service = PublicPeopleService(api.uow_factory, api.clock)
        cheryl = await service.create(
            owner,
            CreatePerson(session_id=session_id, display_name="Cheryl"),
            key="seed-cheryl",
            ceiling=Sensitivity.SENSITIVE,
        )
        fields: dict[str, Any] = {
            "tenant_id": owner.tenant_id,
            "principal_id": owner.principal_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
        source_id, edge_id = uuid4(), uuid4()
        async with api.uow_factory() as uow:
            await uow.people.put(
                PeopleSource(
                    id=source_id,
                    session_id=session_id,
                    event_sequence=1,
                    source_kind="owner",
                    evidence_at=NOW,
                    source_revision="a" * 64,
                    **fields,
                ),
                expected_revision=0,
            )
            belief = memory(statement="Cheryl is my mother.").model_copy(
                update={
                    "id": uuid4(),
                    "tenant_id": owner.tenant_id,
                    "principal_id": owner.principal_id,
                    "scope": "user",
                    "origin_scopes": ["user"],
                    "subject": f"person:{cheryl.id}:cheryl",
                    "belief_type": BeliefType.RELATIONSHIP,
                    "authority": MemoryAuthority.USER,
                    "source_session_id": session_id,
                    "store_position": await uow.memories.next_position(),
                }
            )
            await uow.memories.upsert_belief(belief)
            await uow.people.put(
                RelationshipAssertion(
                    id=edge_id,
                    subject=PeopleEndpoint(kind="person", id=cheryl.id),
                    object=PeopleEndpoint(kind="owner"),
                    predicate="parent",
                    qualifier="mother",
                    belief_id=belief.id,
                    support_ids=[source_id],
                    **fields,
                ),
                expected_revision=0,
            )
            await uow.people.put(
                PeopleInteraction(
                    id=uuid4(),
                    channel="chat",
                    interaction_kind="exchange",
                    attribution="owner_reported",
                    direction="reported",
                    summary="Talked with Cheryl about the weekend.",
                    occurred_at=NOW,
                    participants=[InteractionParticipant(person_id=cheryl.id, role="participant")],
                    support_ids=[source_id],
                    **fields,
                ),
                expected_revision=0,
            )

    def context_call(person_id: UUID, call_id: str) -> ScriptedToolCall:
        return ScriptedToolCall(
            name="people.context",
            arguments={
                "person_ids": [str(person_id)],
                "text": "Call my mom, Cheryl at 10am",
                "scope": "user",
            },
            call_id=call_id,
        )

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        arguments={"text": "Cheryl", "scope": "user"},
                        call_id="g4-memory-search",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            # The production mistake: the relationship ref recalled above is
            # not a person id.
            ScriptedTurn(
                tool_calls=[context_call(edge_id, "g4-context-edge")],
                stop_reason=StopReason.TOOL_USE,
            ),
            # The platform message names people.search as the way to a person id.
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="people.search",
                        arguments={"text": "Cheryl"},
                        call_id="g4-people-search",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
                context_contains="people.search",
            ),
            ScriptedTurn(
                tool_calls=[context_call(cheryl.id, "g4-context-person")],
                stop_reason=StopReason.TOOL_USE,
                context_contains=str(cheryl.id),
            ),
            ScriptedTurn(text="I will call Cheryl at 10am.", context_contains="Cheryl"),
        ]
    )
    async with (
        build(settings=_settings(), storage="postgres", script=script, clock=clock) as worker,
        build(settings=_settings(), storage="postgres", clock=clock) as api,
        _client(api) as client,
    ):
        run_id = await _say(client, session_id, "Call my mom, Cheryl at 10am", "g4-call-mom")
        # Tool deadlines heartbeat on the domain clock; a fixed one expires them.
        await _run_worker_on_wall_clock(worker, "g4-people-chat")
        run = await client.get(f"/v1/runs/{run_id}")
        record = await worker.runs.get(run_id)
        events = await worker.runs.events(run_id)
        history = await client.get(f"/v1/people/{cheryl.id}/history", params=CEILING)
        listing = await client.get("/v1/people", params={**CEILING, "text": "Cheryl"})

    assert run.status_code == 200, run.text
    assert run.json()["status"] == RunStatus.COMPLETED.value, run.json()
    assert record.final_message == "I will call Cheryl at 10am."
    assert _tool_names(events, "tool.call.failed") == ["people.context"]
    failed = next(event.payload for event in events if event.event_type == "tool.call.failed")
    assert failed["reason_code"] == "people.not_found"
    outcome = json.loads(failed["result_item"]["content"][0]["text"])
    assert outcome["remediation"] == "modify_arguments"
    assert _tool_names(events, "tool.call.completed") == [
        "memory.search",
        "people.search",
        "people.context",
    ]
    influence = [event.payload for event in events if event.event_type == "people.context.used"]
    assert influence and all(event["record_ids"] == [str(cheryl.id)] for event in influence)
    assert any(event.event_type == "memory.recalled" for event in events)
    assert history.status_code == 200, history.text
    assert [(item["channel"], item["summary"]) for item in history.json()["items"]] == [
        ("chat", "Talked with Cheryl about the weekend.")
    ]
    assert [item["display_name"] for item in listing.json()["items"]] == ["Cheryl"]


async def test_postgres_a_sister_named_in_chat_is_formed_with_her_relationship() -> None:
    """One chat statement becomes a person, a sibling edge and a fact that cites
    the chat, all readable over the public People routes."""
    clock = FixedClock(NOW)
    text = "My sister Maya lives in Lisbon."
    chat_script = FakeModelScript(turns=[ScriptedTurn(text="Noted.")])
    async with (
        build(settings=_settings(), storage="postgres", script=chat_script, clock=clock) as chat,
        _client(chat) as client,
    ):
        session_id = await _new_chat(client)
        run_id = await _say(client, session_id, text, "g4-sister")
        await _run_worker(chat, "g4-sister-chat")
        run = await chat.runs.get(run_id)
        async with chat.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, chat.principal)
    assert run.status is RunStatus.COMPLETED
    said = next(event for event in events if event.event_type == "user.message.created")
    formation = next(event for event in events if event.event_type == "memory.formation.requested")
    clock.advance(datetime.fromisoformat(str(formation.payload["not_before"])) - clock.now())

    start = text.index("Maya")
    candidate = {
        "subject": "Maya",
        "statement": "User has a sister named Maya who lives in Lisbon.",
        "source_event_ids": [said.sequence],
        "sensitivity_guess": "sensitive",
        "claim_kind": "relationship",
        "derivation": "direct",
        "polarity": "assert",
        "evidence_spans": [{"source_event_id": said.sequence, "text": text}],
        "context_anchors": [],
        "people": {
            "organizations": [],
            "mentions": [
                {
                    "key": "maya",
                    "source_event_id": said.sequence,
                    "start": start,
                    "end": start + 4,
                    "text": "Maya",
                    "display_name": "Maya",
                    "identifier_kind": "name",
                    "identifier_value": "Maya",
                    "namespace": "owner",
                    "context": "",
                    "role": "object",
                    "referent_key": None,
                }
            ],
            "relationship": {
                "subject_key": "owner",
                "object_key": "maya",
                "predicate": "sibling",
                "qualifier": "sister",
                "valid_from": None,
                "valid_to": None,
                "precision": "unknown",
                "source_timezone": None,
            },
            "commitment": None,
        },
    }
    replies: list[dict[str, Any]] = [
        {
            "episodes": [
                {
                    "narrative": f"[e:{said.sequence}] {text}",
                    "subjects": ["Maya"],
                    "source_event_ids": [said.sequence],
                }
            ]
        },
        {"predictions": []},
        {
            "candidates": [candidate],
            "interactions": [],
            "coverage": [
                {
                    "coverage_unit_id": f"{said.sequence}:1",
                    "decision": "formed",
                    "candidate_indexes": [0],
                    "prediction_indexes": [],
                }
            ],
        },
    ]
    formation_script = FakeModelScript(
        turns=[ScriptedTurn(text=json.dumps(reply)) for reply in replies]
    )
    async with (
        build(
            settings=_settings(), storage="postgres", script=formation_script, clock=clock
        ) as former,
        _client(former) as client,
    ):
        await cast(MaintenanceWorker, former.maintenance_factory()).run_once()
        listing = await client.get("/v1/people", params={**CEILING, "text": "Maya"})
        assert listing.status_code == 200, listing.text
        people = listing.json()["items"]
        assert [person["display_name"] for person in people] == ["Maya"], people
        maya_id = people[0]["id"]
        relationships = await client.get(f"/v1/people/{maya_id}/relationships", params=CEILING)
        facts = await client.get(f"/v1/people/{maya_id}/facts", params=CEILING)

    assert relationships.status_code == 200, relationships.text
    assert [
        (item["subject"]["kind"], item["object"], item["predicate"], item["qualifier"])
        for item in relationships.json()["items"]
    ] == [("owner", {"kind": "person", "id": maya_id}, "sibling", "sister")]
    assert facts.status_code == 200, facts.text
    assert [(item["statement"], item["source_session_id"]) for item in facts.json()["items"]] == [
        ("User has a sister named Maya who lives in Lisbon.", str(session_id))
    ]
