"""The owner's persona, folders and model settings, saved over HTTP on PostgreSQL.

Each surface has an in-memory HTTP gate and a PostgreSQL store test; nothing
joined the routes to the PostgreSQL stores, nor to the chats the settings are
for. Three journeys do, each across a composition restart:

- Persona (docs/plan/persona-surface.md "The routes" and "The prefix row"): a
  saved entry reaches the open chat's next model request through one
  `persona_changed` rotation, a stale version and refused content leave the
  document untouched, and a new chat after the restart opens with the latest
  version.
- Folders (docs/plan/thread-folders.md "Manual operations", "Proposals" and
  "The routes"): create, the case-insensitive name collision, move, an accepted
  proposal that files its members, rename, and a delete that unfiles
  conversations without deleting them, with content-free process events.
- Model settings (docs/plan/http-api-and-streaming.md "ADR-0119 model settings
  routes"): a saved chat model applies to app chats created afterward and the
  saved effort to later runs, a stale version conflicts, and a repeated save
  adds no version.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.folders import (
    FolderProposal,
    FolderProposalDerivation,
    FolderProposalKind,
)
from agent_core.domain.messages import FakeModelScript, ReasoningEffort, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)
FIRST_ENTRY = "The owner wants answers in two sentences or fewer."
SECOND_ENTRY = "The owner is training for a spring marathon."


async def _new_chat(client: httpx.AsyncClient) -> UUID:
    created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert created.status_code == 201, created.text
    return UUID(created.json()["id"])


async def _say(
    composition: Composition, client: httpx.AsyncClient, session_id: UUID, text: str
) -> str:
    """Post one owner message, run it, and return the reply the transcript shows."""
    posted = await client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"Idempotency-Key": f"say-{uuid4()}"},
        json={"content": [{"type": "text", "text": text}]},
    )
    assert posted.status_code == 202, posted.text
    assert await run_worker_once(composition, f"owner-settings-{uuid4()}")
    run = await client.get(f"/v1/runs/{posted.json()['run_id']}")
    failure = (await composition.runs.get(UUID(posted.json()["run_id"]))).failure
    assert run.json()["status"] == RunStatus.COMPLETED.value, failure
    transcript = await client.get(f"/v1/sessions/{session_id}/messages")
    [part] = transcript.json()["items"][-1]["content"]
    return str(part["text"])


def _entry(text: str) -> dict[str, Any]:
    return {"text": text, "sensitivity": "internal", "source_belief_id": None}


def _scoped(composition: Composition, *scopes: str, principal_id: str | None = None) -> Principal:
    return Principal(
        tenant_id=composition.principal.tenant_id,
        principal_id=principal_id or composition.principal.principal_id,
        roles={"user"},
        scopes=set(scopes),
    )


async def test_a_saved_persona_reaches_the_open_chat_and_the_next_one_after_a_restart() -> None:
    settings = replace(database_settings(), persona_api_enabled=True)
    clock = FixedClock(NOW)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(text="Hello."),
            # Matches only when the request carries the saved entry.
            ScriptedTurn(text="Short answer.", context_contains=FIRST_ENTRY),
        ]
    )
    async with (
        build(settings=settings, storage="postgres", script=script, clock=clock) as app,
        api_client(app) as client,
    ):
        open_chat = await _new_chat(client)
        assert await _say(app, client, open_chat, "Good morning.") == "Hello."

        empty = await client.get("/v1/persona")
        assert empty.status_code == 200, empty.text
        assert empty.headers["cache-control"] == "private, no-store"
        assert empty.json()["version"] == 0 and empty.json()["entries"] == []

        saved = await client.put(
            "/v1/persona", json={"expected_version": 0, "entries": [_entry(FIRST_ENTRY)]}
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 1
        assert saved.json()["entries"][0]["source"] == "user_edit"
        stale = await client.put(
            "/v1/persona", json={"expected_version": 0, "entries": [_entry("A stale edit.")]}
        )
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
        credential = "api_key: " + "value-123"
        refused = await client.put(
            "/v1/persona", json={"expected_version": 1, "entries": [_entry(credential)]}
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["code"] == "malformed_request"
        async with api_client(app, principal=_scoped(app, "persona.read")) as reader:
            assert (await reader.get("/v1/persona")).json()["version"] == 1
            forbidden = await reader.put(
                "/v1/persona", json={"expected_version": 1, "entries": [_entry("No.")]}
            )
            assert forbidden.status_code == 403, forbidden.text

        # The open chat picks the edit up at its next plan: one rotation, one reason.
        assert await _say(app, client, open_chat, "How far should I run today?") == "Short answer."
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(open_chat, 0, app.principal)
        rotations = [event for event in events if event.event_type == "context.epoch.rotated"]
        assert [event.payload["reason"] for event in rotations] == ["persona_changed"]

        grown = await client.put(
            "/v1/persona",
            json={"expected_version": 1, "entries": [_entry(FIRST_ENTRY), _entry(SECOND_ENTRY)]},
        )
        assert grown.status_code == 200 and grown.json()["version"] == 2

    later_script = FakeModelScript(
        turns=[ScriptedTurn(text="Eight easy miles.", context_contains=SECOND_ENTRY)]
    )
    async with (
        build(settings=settings, storage="postgres", script=later_script, clock=clock) as later,
        api_client(later) as client,
    ):
        head = await client.get("/v1/persona")
        assert head.json()["version"] == 2
        assert [entry["text"] for entry in head.json()["entries"]] == [FIRST_ENTRY, SECOND_ENTRY]
        history = await client.get("/v1/persona/history")
        assert [item["version"] for item in history.json()["items"]] == [2, 1]
        new_chat = await _new_chat(client)
        assert await _say(later, client, new_chat, "What is today's run?") == "Eight easy miles."
        # A stranger's document is their own: version 0, not this one.
        stranger = _scoped(later, "persona.read", principal_id="someone-else")
        async with api_client(later, principal=stranger) as other:
            assert (await other.get("/v1/persona")).json()["version"] == 0


async def test_folders_file_rename_and_unfile_conversations_across_a_restart() -> None:
    settings = replace(database_settings(), thread_folders_api_enabled=True)
    async with (
        build(settings=settings, storage="postgres", fixed_clock_at=NOW) as app,
        api_client(app) as client,
    ):
        filed, first, second = [await _new_chat(client) for _ in range(3)]
        created = await client.post("/v1/folders", json={"name": "  Trip   to Lisbon "})
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "private, no-store"
        assert created.json()["name"] == "Trip to Lisbon"
        assert created.json()["thread_count"] == 0
        trip = created.json()["id"]
        duplicate = await client.post("/v1/folders", json={"name": "trip TO lisbon"})
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["error"]["details"]["reason"] == "folder_name_taken"

        moved = await client.put(f"/v1/sessions/{filed}/folder", json={"folder_id": trip})
        assert moved.status_code == 200, moved.text
        assert moved.json()["folder_id"] == trip
        forgotten = await client.put(f"/v1/sessions/{filed}/folder", json={})
        assert forgotten.status_code == 400, forgotten.text
        assert (await client.get(f"/v1/folders/{trip}")).json()["thread_count"] == 1

        # Only the maintenance pass proposes; the review routes resolve what it stored.
        async with app.uow_factory() as uow:
            proposal = await uow.folders.propose(
                FolderProposal(
                    id=uuid4(),
                    tenant_id=app.principal.tenant_id,
                    principal_id=app.principal.principal_id,
                    kind=FolderProposalKind.NEW_FOLDER,
                    proposed_name="House Move",
                    member_session_ids=tuple(sorted((first, second), key=lambda value: value.int)),
                    derivation=FolderProposalDerivation.LEXICAL,
                    created_at=NOW,
                )
            )
        listed = await client.get("/v1/folders/proposals")
        assert [row["id"] for row in listed.json()["items"]] == [str(proposal.id)]
        accepted = await client.post(f"/v1/folders/proposals/{proposal.id}/accept")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["state"] == "accepted"
        house = accepted.json()["resulting_folder_id"]
        replayed = await client.post(f"/v1/folders/proposals/{proposal.id}/accept")
        assert replayed.status_code == 200
        assert replayed.json()["resulting_folder_id"] == house
        late = await client.post(f"/v1/folders/proposals/{proposal.id}/decline")
        assert late.status_code == 409, late.text
        assert late.json()["error"]["details"]["reason"] == "proposal_resolved"

        stranger = _scoped(app, "session.read", "session.write", principal_id="someone-else")
        async with api_client(app, principal=stranger) as other:
            assert (await other.get(f"/v1/folders/{trip}")).status_code == 404
            assert (await other.get("/v1/folders")).json()["items"] == []
            hidden = await other.put(f"/v1/sessions/{filed}/folder", json={"folder_id": None})
            assert hidden.status_code == 404
        async with api_client(app, principal=_scoped(app, "session.read")) as reader:
            assert (await reader.get("/v1/folders")).status_code == 200
            assert (await reader.post("/v1/folders", json={"name": "No"})).status_code == 403

    async with (
        build(settings=settings, storage="postgres", fixed_clock_at=NOW) as restarted,
        api_client(restarted) as client,
    ):
        folders = await client.get("/v1/folders")
        assert [(row["name"], row["thread_count"]) for row in folders.json()["items"]] == [
            ("House Move", 2),
            ("Trip to Lisbon", 1),
        ]
        index = await client.get("/v1/sessions")
        assert {row["id"]: row["folder_id"] for row in index.json()["items"]} == {
            str(filed): trip,
            str(first): house,
            str(second): house,
        }

        clash = await client.patch(f"/v1/folders/{trip}", json={"name": "house MOVE"})
        assert clash.status_code == 409, clash.text
        assert clash.json()["error"]["details"]["reason"] == "folder_name_taken"
        recased = await client.patch(f"/v1/folders/{trip}", json={"name": "TRIP to Lisbon"})
        assert recased.status_code == 200, recased.text
        renamed = await client.patch(f"/v1/folders/{trip}", json={"name": "Lisbon"})
        assert renamed.status_code == 200 and renamed.json()["name"] == "Lisbon"
        # A rename never moves anything.
        assert renamed.json()["thread_count"] == 1

        deleted = await client.delete(f"/v1/folders/{house}")
        assert deleted.status_code == 204, deleted.text
        assert (await client.get(f"/v1/folders/{house}")).status_code == 404
        assert (await client.delete(f"/v1/folders/{house}")).status_code == 404
        # The conversations are unfiled, never deleted.
        for session_id in (first, second):
            survivor = await client.get(f"/v1/sessions/{session_id}")
            assert survivor.status_code == 200, survivor.text
            assert survivor.json()["folder_id"] is None
        assert (await client.get(f"/v1/sessions/{filed}")).json()["folder_id"] == trip

        async with restarted.uow_factory() as uow:
            recorded = await uow.process_events.list()
    kinds = [event.event_type for event in recorded]
    assert kinds.count("folder.created") == 2
    assert kinds.count("folder.renamed") == 2
    assert kinds.count("folder.deleted") == 1
    assert kinds.count("folder.proposal.accepted") == 1
    assert kinds.count("session.folder.changed") >= 1
    folder_events = [
        event for event in recorded if event.event_type.startswith(("folder.", "session.folder."))
    ]
    for event in folder_events:
        for name in ("Lisbon", "House Move"):
            assert name not in str(event.payload), event.event_type


def _model_body(version: int, policy: str, effort: str | None) -> dict[str, Any]:
    return {
        "expected_version": version,
        "chat": {"model_policy": policy, "reasoning_effort": effort},
        "memory": {"model_policy": "balanced", "reasoning_effort": None},
    }


async def test_saved_model_settings_bind_later_chats_and_runs_across_a_restart() -> None:
    settings = replace(
        database_settings(),
        credentials=MappingProxyType(
            {name: SecretStr("test-" + "key") for name in ("openai", "anthropic")}
        ),
    )
    clock = FixedClock(NOW)
    # A composition closes its providers on exit, so each process gets its own.
    before = FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text="First.")]), clock)
    after = FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text="Second.")]), clock)

    async def chat_model(composition: Composition, session_id: UUID) -> tuple[str, str]:
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(session_id, composition.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)
        return session.agent_version, agent.model_policy

    async with (
        build(
            settings=settings,
            storage="postgres",
            model_policy="astra",
            clock=clock,
            model_provider_overrides={"openai": before, "anthropic": before},
        ) as app,
        api_client(app) as client,
    ):
        defaults = await client.get("/v1/settings/models")
        assert defaults.status_code == 200, defaults.text
        assert defaults.headers["cache-control"] == "private, no-store"
        assert defaults.json()["version"] == 0
        assert defaults.json()["chat"] == {"model_policy": "astra", "reasoning_effort": "high"}
        offered = {option["model_policy"] for option in defaults.json()["chat_options"]}
        assert {"astra", "fable"} <= offered

        earlier = await _new_chat(client)
        assert await _say(app, client, earlier, "Hello.") == "First."

        unoffered = await client.put("/v1/settings/models", json=_model_body(0, "missing", "high"))
        assert unoffered.status_code == 400, unoffered.text
        saved = await client.put("/v1/settings/models", json=_model_body(0, "fable", "low"))
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 1
        assert saved.json()["chat"] == {"model_policy": "fable", "reasoning_effort": "low"}
        retried = await client.put("/v1/settings/models", json=_model_body(0, "fable", "low"))
        assert retried.status_code == 200 and retried.json() == saved.json()
        stale = await client.put("/v1/settings/models", json=_model_body(0, "astra", "low"))
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
        async with api_client(app, principal=_scoped(app, "settings.read")) as reader:
            assert (await reader.get("/v1/settings/models")).json()["version"] == 1
            denied = await reader.put("/v1/settings/models", json=_model_body(1, "astra", "low"))
            assert denied.status_code == 403, denied.text

    async with (
        build(
            settings=settings,
            storage="postgres",
            model_policy="astra",
            clock=clock,
            model_provider_overrides={"openai": after, "anthropic": after},
        ) as restarted,
        api_client(restarted) as client,
    ):
        kept = await client.get("/v1/settings/models")
        assert kept.json()["version"] == 1
        assert kept.json()["chat"] == {"model_policy": "fable", "reasoning_effort": "low"}

        later = await _new_chat(client)
        # The chat that already existed keeps its model; the saved effort reaches
        # its next run all the same.
        assert await _say(restarted, client, earlier, "Again.") == "Second."
        earlier_version, earlier_policy = await chat_model(restarted, earlier)
        later_version, later_policy = await chat_model(restarted, later)
        assert (earlier_policy, later_policy) == ("astra", "fable")
        assert earlier_version != later_version
        assert [request.reasoning_effort for request in before.requests] == [ReasoningEffort.HIGH]
        assert [request.reasoning_effort for request in after.requests] == [ReasoningEffort.LOW]
        async with restarted.uow_factory() as uow:
            updates = await uow.process_events.list("settings.models.updated")
    assert [event.payload["version"] for event in updates] == [1]
    assert updates[0].payload["chat_model_policy"] == "fable"
