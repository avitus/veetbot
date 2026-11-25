"""A schedule's script approval in the app (ADR-0342 decision 13).

The plain-words form is a setup like the chat card: its draft carries the
approval when the client shows it, and saving the draft is the approval. A
schedule read tells the app whether the approval can be given, so the detail
screen can show it, give it and withdraw it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.api import create_app
from agent_core.application.schedule_script_approvals import ScheduleScriptOffers
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.execution import SANDBOX_EXECUTE_SCOPE, SANDBOX_RUN_CODE_TOOL_NAME
from agent_core.domain.messages import FakeModelScript, ModelUsage, ScriptedTurn
from agent_core.domain.schedules import JobTask, ScheduleDraftStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.scheduling.drafts import ScheduleDrafter, ScheduleDraftInterpreter
from tests.contract.support import principal
from tests.unit.test_reply_attachments import _settings
from tests.unit.test_schedule_drafts import AGENT, LIMITS, NOW, TEXT, ZONE, _answer, _Router

_SCRIPT_AGENT = AGENT.model_copy(update={"enabled_tools": [SANDBOX_RUN_CODE_TOOL_NAME]})
_OWNER = principal().model_copy(update={"scopes": set(PLATFORM_SCOPES)})
_BODY = {"text": TEXT, "timezone": ZONE}


def _drafter(factory: Any, *, agent: Any, approve_scripts: bool) -> ScheduleDrafter:
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=_answer(),
                    usage=ModelUsage(
                        input_tokens=300, output_tokens=40, provider="openai", model="m"
                    ),
                )
            ]
        ),
        FixedClock(NOW),
    )
    ids = SequenceIdFactory()
    return ScheduleDrafter(
        uow_factory=factory,
        clock=FixedClock(NOW),
        ids=ids,
        agent=agent,
        limits=LIMITS,
        interpreter=ScheduleDraftInterpreter(
            router=_Router(),
            providers={"openai": provider},
            clock=FixedClock(NOW),
            ids=ids,
            model_policy="balanced",
        ),
        approve_scripts=approve_scripts,
    )


# --- the draft -----------------------------------------------------------------


@asynccontextmanager
async def _built(
    tmp_path: Path, *, scripts: bool = True, owner: Principal = _OWNER
) -> AsyncIterator[tuple[httpx.AsyncClient, Composition]]:
    """The API over a composition whose default agent has the script tool."""
    async with build(
        settings=replace(
            _settings(tmp_path),
            code_mode_enabled=True,
            schedule_scripts_enabled=scripts,
            schedule_api_enabled=True,
        ),
        script=FakeModelScript(turns=[]),
        principal=_OWNER,
    ) as built:
        drafter = _drafter(
            built.uow_factory, agent=built.sessions._default_agent, approve_scripts=scripts
        )
        app = create_app(
            replace(built.services, schedule_drafts=drafter),
            built.settings,
            owner,
            built.new_request_id,
            built.readiness_probe,
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            yield client, built


async def test_a_draft_carries_the_approval_when_the_client_shows_it(tmp_path: Path) -> None:
    async with _built(tmp_path) as (_, built):
        drafter = _drafter(built.uow_factory, agent=_SCRIPT_AGENT, approve_scripts=True)

        draft = await drafter.draft(_OWNER, text=TEXT, timezone=ZONE, offer_script_approval=True)

    assert draft.status is ScheduleDraftStatus.READY
    assert draft.definition is not None
    assert draft.definition.scripts_approved is True
    # Without the scope a run is not offered the script tool at all.
    assert draft.definition.requested_scopes == frozenset({SANDBOX_EXECUTE_SCOPE})


@pytest.mark.parametrize(
    "case", ["client_does_not_show_it", "setting_off", "agent_without_the_tool", "owner_cannot"]
)
async def test_a_draft_carries_no_approval_the_owner_was_not_shown_or_cannot_give(
    tmp_path: Path, case: str
) -> None:
    owner = _OWNER
    if case == "owner_cannot":
        owner = _OWNER.model_copy(update={"scopes": set(PLATFORM_SCOPES) - {SANDBOX_EXECUTE_SCOPE}})
    async with _built(tmp_path) as (_, built):
        drafter = _drafter(
            built.uow_factory,
            agent=AGENT if case == "agent_without_the_tool" else _SCRIPT_AGENT,
            approve_scripts=case != "setting_off",
        )

        draft = await drafter.draft(
            owner,
            text=TEXT,
            timezone=ZONE,
            offer_script_approval=case != "client_does_not_show_it",
        )

    assert draft.definition is not None
    assert draft.definition.scripts_approved is False
    assert draft.definition.requested_scopes == frozenset()


async def test_saving_the_form_s_draft_sets_up_a_schedule_approved_for_scripts(
    tmp_path: Path,
) -> None:
    async with _built(tmp_path) as (client, _):
        drafted = await client.post(
            "/v1/schedules/drafts", json={**_BODY, "offer_script_approval": True}
        )
        assert drafted.status_code == 200, drafted.text
        definition = drafted.json()["definition"]
        assert definition["scripts_approved"] is True
        assert SANDBOX_EXECUTE_SCOPE in definition["requested_scopes"]

        created = await client.post(
            "/v1/schedules", json=definition, headers={"Idempotency-Key": "form-1"}
        )

    assert created.status_code == 201, created.text
    assert created.json()["revision"]["scripts_approved"] is True


async def test_an_older_client_s_draft_is_unchanged(tmp_path: Path) -> None:
    async with _built(tmp_path) as (client, _):
        drafted = await client.post("/v1/schedules/drafts", json=_BODY)

    assert drafted.status_code == 200, drafted.text
    definition = drafted.json()["definition"]
    assert "scripts_approved" not in definition
    assert definition["requested_scopes"] == []


# --- the schedule the app reads -------------------------------------------------


async def _create(client: httpx.AsyncClient, key: str = "plain") -> dict[str, Any]:
    drafted = await client.post("/v1/schedules/drafts", json=_BODY)
    assert drafted.status_code == 200, drafted.text
    created = await client.post(
        "/v1/schedules", json=drafted.json()["definition"], headers={"Idempotency-Key": key}
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def _definition(record: dict[str, Any], *, approved: bool) -> dict[str, Any]:
    """The full definition the app sends back, with only the approval changed."""
    revision = record["revision"]
    fields = (
        "title",
        "instruction",
        "agent_id",
        "agent_version",
        "policy_profile",
        "limits",
        "run_timeout_seconds",
        "cadence",
        "misfire_grace_seconds",
        "max_consecutive_failures",
        "notify",
    )
    definition = {name: revision[name] for name in fields if name in revision}
    scopes = set(revision["requested_scopes"]) - {SANDBOX_EXECUTE_SCOPE}
    if approved:
        scopes.add(SANDBOX_EXECUTE_SCOPE)
        definition["scripts_approved"] = True
    definition["requested_scopes"] = sorted(scopes)
    return definition


async def test_the_app_can_give_and_withdraw_the_approval_where_the_read_offers_it(
    tmp_path: Path,
) -> None:
    async with _built(tmp_path) as (client, _):
        created = await _create(client)
        schedule_id = created["schedule"]["id"]
        assert created["scripts_available"] is True
        assert "scripts_approved" not in created["revision"]

        given = await client.patch(
            f"/v1/schedules/{schedule_id}",
            json={"expected_revision": 1, "definition": _definition(created, approved=True)},
        )
        assert given.status_code == 200, given.text
        assert given.json()["revision"]["scripts_approved"] is True
        assert given.json()["scripts_available"] is True

        read = (await client.get(f"/v1/schedules/{schedule_id}")).json()
        assert read["revision"]["scripts_approved"] is True
        assert read["scripts_available"] is True

        withdrawn = await client.patch(
            f"/v1/schedules/{schedule_id}",
            json={"expected_revision": 2, "definition": _definition(read, approved=False)},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert "scripts_approved" not in withdrawn.json()["revision"]
        assert SANDBOX_EXECUTE_SCOPE not in withdrawn.json()["revision"]["requested_scopes"]
        # Paused or resumed, the schedule still says the approval can be given.
        paused = await client.post(
            f"/v1/schedules/{schedule_id}/pause", json={"expected_revision": 3}
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["scripts_available"] is True


async def test_a_read_offers_nothing_while_the_setting_is_off(tmp_path: Path) -> None:
    async with _built(tmp_path, scripts=False) as (client, _):
        created = await _create(client)
        read = (await client.get(f"/v1/schedules/{created['schedule']['id']}")).json()

    assert "scripts_available" not in created
    assert "scripts_available" not in read


@pytest.mark.parametrize(
    "case", ["agent_without_the_tool", "owner_cannot", "setting_off", "job_schedule"]
)
async def test_the_approval_is_offered_only_where_it_can_be_given(
    tmp_path: Path, case: str
) -> None:
    owner = _OWNER
    if case == "owner_cannot":
        owner = _OWNER.model_copy(update={"scopes": set(PLATFORM_SCOPES) - {SANDBOX_EXECUTE_SCOPE}})
    async with _built(tmp_path) as (client, built):
        created = await _create(client)
        async with built.uow_factory() as uow:
            await uow.agents.put(AGENT)
            held = await uow.schedules.get_revision(UUID(created["schedule"]["id"]), 1, _OWNER)
        revision = held
        if case == "agent_without_the_tool":
            revision = held.model_copy(
                update={"agent_id": AGENT.id, "agent_version": AGENT.version}
            )
        if case == "job_schedule":
            revision = held.model_copy(
                update={
                    "task": JobTask(slug="collector", revision_id=UUID(int=1), grant_id=UUID(int=2))
                }
            )
        refusing = ScheduleScriptOffers(
            uow_factory=built.uow_factory, enabled=case != "setting_off"
        )
        offering = ScheduleScriptOffers(uow_factory=built.uow_factory, enabled=True)

        assert await refusing.available(owner, revision) is False
        assert await offering.available(_OWNER, held) is True
