"""A schedule's script approval, given once at setup (ADR-0342 decision 13).

A scheduled run has nobody to answer a card. The owner approves a schedule's
scripts when the schedule is set up, and each of its runs then starts a script
without asking, even after the run has read outside content. The approval
covers the script tool and the card outside content raises, and nothing else.
A run is offered the tool only when its schedule requests the sandbox scope,
so the setup that records the approval requests that scope too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.application.schedule_script_approvals import (
    SCHEDULE_SCRIPT_APPROVAL_AUTHORIZATION_KIND,
    ScheduleScriptAuthorizer,
)
from agent_core.bootstrap import build
from agent_core.domain.errors import NotFoundError, ScheduleValidationError
from agent_core.domain.execution import SANDBOX_EXECUTE_SCOPE, SANDBOX_RUN_CODE_TOOL_NAME
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, StopReason
from agent_core.domain.policies import AuthorizationTurn, PolicyDecisionType, TrustLevel
from agent_core.domain.runs import Run, RunLimits, RunStatus
from agent_core.domain.schedules import (
    DailyCadence,
    JobTask,
    OccurrenceDisposition,
    ScheduleDefinition,
    ScheduleDefinitionPatch,
    ScheduledRunLink,
    ScheduleOccurrence,
    ScheduleRevision,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.scheduling.defaults import derived_definition
from agent_core.tools.registry import RegisteredTool, StaticToolRegistry
from agent_core.tools.schedule_create import ScheduleCreateTool
from tests.contract.support import NOW, principal
from tests.gates.test_deferred_tools_adr0123 import _production
from tests.gates.test_mcp_schedules_adr0291 import _create, _create_turn
from tests.unit.test_code_mode import _Probe, _spec
from tests.unit.test_code_mode_budget import _Script, _turn
from tests.unit.test_job_request_grant import SCHEDULE, a_run
from tests.unit.test_reply_attachments import _settings
from tests.unit.test_script_request_grant import decision, proposed

_OUTSIDE_READ = _spec("demo.outside", output_trust=TrustLevel.EXTERNAL_UNTRUSTED)
_DAILY = DailyCadence(local_time=time(9), timezone="UTC")
_SANDBOX = frozenset({SANDBOX_EXECUTE_SCOPE})
_SCHEDULER_TURN = AuthorizationTurn(
    newest_user_trust=TrustLevel.USER, tool_names=frozenset({"demo.outside"})
)


# --- a scheduled run, end to end -----------------------------------------------


async def _scheduled_run(tmp_path: Path, *, approved: bool) -> tuple[Run, int, list[str]]:
    """Fire one run of a schedule whose model reads outside content, then scripts.

    Returns the run once it ends or pauses, how many cards it raised, and the
    kind of each standing authorization it recorded.
    """
    model = FakeModelScript(
        turns=[
            _turn(("demo.outside", "read")),
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "first")),
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "second")),
            ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
        ]
    )
    owner = principal().model_copy(update={"scopes": set(PLATFORM_SCOPES)})
    async with build(
        settings=replace(_settings(tmp_path), code_mode_enabled=True, schedule_api_enabled=True),
        script=model,
        enabled_tools=[SANDBOX_RUN_CODE_TOOL_NAME, "demo.outside"],
        principal=owner,
    ) as built:
        registry = cast(StaticToolRegistry, built.tool_pipeline._registry)
        registry.register(_Probe(_OUTSIDE_READ))
        registered = cast(RegisteredTool, registry.get(SANDBOX_RUN_CODE_TOOL_NAME))
        registered.implementation = _Script(registered.spec, [[], []])
        definition = derived_definition(
            built.sessions._default_agent,
            built.schedules._limits,
            title="Morning briefing",
            instruction="Read the news and work out what changed.",
            cadence=_DAILY,
            # Without the scope the run would not be offered the tool at all.
            requested_scopes=_SANDBOX,
            scripts_approved=approved,
        )
        record = await built.schedules.create(owner, definition, "morning-briefing")
        runner = cast(Any, built.services).schedule_runs
        occurrence, _replayed = await runner.materialize_manual(
            owner, record.schedule.id, expected_revision=1, request_key="now"
        )
        assert occurrence.run_id is not None
        await built.executor.execute(occurrence.run_id)
        run = await built.runs.get(occurrence.run_id)
        events = await built.runs.events(occurrence.run_id)
    cards = sum(event.event_type == "approval.requested" for event in events)
    authorized = [
        str(event.payload["authorization_kind"])
        for event in events
        if event.event_type == "tool.call.authorized" and "authorization_kind" in event.payload
    ]
    return run, cards, authorized


async def test_a_schedule_approved_for_scripts_runs_them_without_a_card(tmp_path: Path) -> None:
    run, cards, authorized = await _scheduled_run(tmp_path, approved=True)

    # Both scripts follow outside content, and neither stopped the run.
    assert run.status is RunStatus.COMPLETED, run.failure
    assert cards == 0
    assert authorized == [SCHEDULE_SCRIPT_APPROVAL_AUTHORIZATION_KIND] * 2


async def test_a_schedule_without_the_approval_still_asks(tmp_path: Path) -> None:
    run, cards, authorized = await _scheduled_run(tmp_path, approved=False)

    assert run.status is RunStatus.WAITING_FOR_APPROVAL, run.failure
    assert cards == 1
    assert authorized == []


# --- the rule itself -----------------------------------------------------------


def _revision(**update: Any) -> ScheduleRevision:
    return ScheduleRevision(
        schedule_id=SCHEDULE,
        revision=3,
        title="Morning briefing",
        instruction="Read the news.",
        agent_id=UUID(int=0xA6),
        agent_version="agent-v1",
        policy_profile="default",
        requested_scopes=frozenset(),
        limits=RunLimits(max_steps=4, max_model_calls=4, max_tool_calls=4, max_cost=Decimal("1")),
        run_timeout_seconds=60,
        cadence=_DAILY,
        timezone="UTC",
        misfire_grace_seconds=3600,
        max_consecutive_failures=2,
        created_by_principal_id=principal().principal_id,
        created_at=NOW,
        scripts_approved=True,
    ).model_copy(update=update)


def _link(run: Run, **update: Any) -> ScheduledRunLink:
    return ScheduledRunLink(
        occurrence=ScheduleOccurrence(
            id=UUID(int=0x0CC),
            schedule_id=SCHEDULE,
            schedule_revision=3,
            nominal_fire_at=NOW,
            disposition=OccurrenceDisposition.MATERIALIZED,
            session_id=run.session_id,
            run_id=run.id,
            authority_version="authority-v1",
            materialized_at=NOW,
            created_at=NOW,
        ),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
    ).model_copy(update=update)


def _authorizer(
    link: ScheduledRunLink | None, revision: ScheduleRevision | None
) -> ScheduleScriptAuthorizer:
    """The authorizer over a store that holds this link and this revision."""

    class _Occurrences:
        async def get_by_run(self, run_id: UUID) -> ScheduledRunLink | None:
            return link if link is not None and link.occurrence.run_id == run_id else None

    class _Schedules:
        async def get_revision(self, schedule_id: UUID, number: int, owner: Any) -> Any:
            if revision is None or (schedule_id, number) != (SCHEDULE, revision.revision):
                raise NotFoundError("schedule revision not found")
            return revision

    @asynccontextmanager
    async def uow_factory() -> AsyncIterator[Any]:
        yield SimpleNamespace(schedule_occurrences=_Occurrences(), schedules=_Schedules())

    return ScheduleScriptAuthorizer(uow_factory=cast(Any, uow_factory))


async def _authorize(
    authorizer: ScheduleScriptAuthorizer,
    run: Run,
    *,
    tool: str = SANDBOX_RUN_CODE_TOOL_NAME,
    untrusted: bool = True,
    kind: PolicyDecisionType = PolicyDecisionType.REQUIRE_APPROVAL,
    agent_version: str = "agent-v1",
    turn: AuthorizationTurn | None = _SCHEDULER_TURN,
) -> Any:
    return await authorizer.authorize(
        action=proposed(tool),
        decision=decision(untrusted=untrusted, kind=kind),
        principal=principal(),
        run=run,
        agent_version=agent_version,
        action_deadline=NOW + timedelta(minutes=5),
        turn=turn,
    )


async def test_the_approval_names_the_schedule_revision_that_holds_it() -> None:
    run = a_run()
    allowed = await _authorize(_authorizer(_link(run), _revision()), run)

    assert allowed.allowed
    assert allowed.authorization_kind == SCHEDULE_SCRIPT_APPROVAL_AUTHORIZATION_KIND
    assert allowed.authorization_ref == f"{SCHEDULE}:3"


@pytest.mark.parametrize(
    "case",
    [
        "another_tool",
        "another_reason_to_ask",
        "not_an_approval_decision",
        "not_a_scheduled_run",
        "revision_without_the_approval",
        "revision_gone",
        "another_agent_version",
        "another_owner",
        "a_child_run",
        "an_outside_message_is_newest",
        "no_turn",
    ],
)
async def test_the_approval_covers_nothing_else(case: str) -> None:
    run = a_run(parent_run_id=UUID(int=0xFA) if case == "a_child_run" else None)
    link: ScheduledRunLink | None = _link(run)
    revision: ScheduleRevision | None = _revision()
    options: dict[str, Any] = {}
    if case == "another_tool":
        options["tool"] = "sandbox.run_command"
    elif case == "another_reason_to_ask":
        options["untrusted"] = False
    elif case == "not_an_approval_decision":
        options["kind"] = PolicyDecisionType.DENY
    elif case == "not_a_scheduled_run":
        link = None
    elif case == "revision_without_the_approval":
        revision = _revision(scripts_approved=False)
    elif case == "revision_gone":
        revision = None
    elif case == "another_agent_version":
        options["agent_version"] = "agent-v2"
    elif case == "another_owner":
        link = _link(run, principal_id="someone-else")
    elif case == "an_outside_message_is_newest":
        options["turn"] = AuthorizationTurn(
            newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED, tool_names=frozenset()
        )
    elif case == "no_turn":
        options["turn"] = None

    refused = await _authorize(_authorizer(link, revision), run, **options)

    assert not refused.allowed


# --- where the approval is recorded --------------------------------------------


def test_a_job_schedule_cannot_hold_a_script_approval() -> None:
    briefing = derived_definition(
        _agent_like(), _limits(), title="Collector", instruction="Collect.", cadence=_DAILY
    )
    job = {
        **briefing.model_dump(mode="json"),
        "task": JobTask(slug="radar", revision_id=UUID(int=1), grant_id=UUID(int=2)).model_dump(
            mode="json"
        ),
        "limits": RunLimits(
            max_steps=4, max_model_calls=0, max_tool_calls=4, max_cost=Decimal("0")
        ),
    }
    assert ScheduleDefinition.model_validate(job).scripts_approved is False

    with pytest.raises(ValidationError, match="runs no script"):
        ScheduleDefinition.model_validate({**job, "scripts_approved": True})


def test_a_definition_without_the_approval_stores_nothing_for_it() -> None:
    definition = derived_definition(
        _agent_like(), _limits(), title="Briefing", instruction="Brief.", cadence=_DAILY
    )

    assert definition.scripts_approved is False
    assert "scripts_approved" not in definition.model_dump(mode="json")
    approved = definition.model_copy(update={"scripts_approved": True})
    assert approved.model_dump(mode="json")["scripts_approved"] is True


async def test_a_content_change_keeps_the_approval_and_only_a_full_update_drops_it(
    tmp_path: Path,
) -> None:
    owner = principal().model_copy(update={"scopes": set(PLATFORM_SCOPES)})
    async with build(
        settings=replace(_settings(tmp_path), code_mode_enabled=True, schedule_api_enabled=True),
        script=FakeModelScript(turns=[]),
        principal=owner,
    ) as built:
        definition = derived_definition(
            built.sessions._default_agent,
            built.schedules._limits,
            title="Morning briefing",
            instruction="Read the news.",
            cadence=_DAILY,
            requested_scopes=_SANDBOX,
            scripts_approved=True,
        )
        created = await built.schedules.create(owner, definition, "briefing")
        patched = await built.schedules.patch(
            owner,
            created.schedule.id,
            1,
            ScheduleDefinitionPatch(instruction="Read the news and the weather."),
            "briefing-patch",
        )
        dropped = await built.schedules.update(
            owner,
            created.schedule.id,
            2,
            definition.model_copy(update={"scripts_approved": False}),
        )

    assert created.revision.scripts_approved
    assert patched.revision.scripts_approved
    assert not dropped.revision.scripts_approved


@pytest.mark.parametrize(
    ("code_mode", "scopes", "reason"),
    [
        (True, frozenset(), "schedule.scripts_scope_required"),
        (False, _SANDBOX, "schedule.scripts_unavailable"),
    ],
)
async def test_an_approval_needs_the_scope_and_an_agent_with_the_tool(
    tmp_path: Path, code_mode: bool, scopes: frozenset[str], reason: str
) -> None:
    owner = principal().model_copy(update={"scopes": set(PLATFORM_SCOPES)})
    async with build(
        settings=replace(
            _settings(tmp_path), code_mode_enabled=code_mode, schedule_api_enabled=True
        ),
        script=FakeModelScript(turns=[]),
        principal=owner,
    ) as built:
        definition = derived_definition(
            built.sessions._default_agent,
            built.schedules._limits,
            title="Morning briefing",
            instruction="Read the news.",
            cadence=_DAILY,
            requested_scopes=scopes,
            scripts_approved=True,
        )
        with pytest.raises(ScheduleValidationError) as refused:
            await built.schedules.create(owner, definition, "briefing")

    assert refused.value.reason == reason


# --- the setup card ------------------------------------------------------------

_ASK = {
    "title": "Morning briefing",
    "instruction": "Read the news.",
    "at": "2030-01-07T15:00:00+00:00",
}


@pytest.mark.parametrize(
    ("allowed", "has_tool", "approves"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
async def test_the_setup_card_says_when_it_also_approves_the_scripts(
    allowed: bool, has_tool: bool, approves: bool
) -> None:
    agent = _agent_like().model_copy(
        update={"enabled_tools": [SANDBOX_RUN_CODE_TOOL_NAME] if has_tool else []}
    )
    tool = ScheduleCreateTool(cast(Any, None), agent, _limits(), approve_scripts=allowed)

    summary, view = await tool.approval_view(dict(_ASK), tenant_id="t")

    assert summary.endswith("its runs may run scripts without asking") is approves
    assert view.get("scripts_approved", False) is approves
    assert view["requested_scopes"] == ([SANDBOX_EXECUTE_SCOPE] if approves else [])


async def test_a_schedule_set_up_in_chat_records_the_approval_its_card_showed(
    tmp_path: Path,
) -> None:
    _factory, context = await _production(
        tmp_path,
        _create_turn(),
        extra_environment={
            "AGENT_CODE_MODE_ENABLED": "1",
            "AGENT_SCHEDULE_SCRIPTS_ENABLED": "1",
        },
    )
    async with context as app:
        approval = await _create(app)
        page = await app.schedules.list(app.principal, 10, None)

    assert approval.arguments["scripts_approved"] is True
    assert approval.action_summary.endswith("its runs may run scripts without asking")
    [record] = page.items
    assert record.revision.scripts_approved
    assert SANDBOX_EXECUTE_SCOPE in record.revision.requested_scopes


def _agent_like() -> Any:
    from agent_core.domain.agents import AgentSpec

    return AgentSpec(
        id=UUID(int=0xA6),
        version="agent-v1",
        name="Agent",
        instructions="Work.",
        model_policy="fake",
        enabled_tools=[],
        policy_profile="default",
        limits=RunLimits(),
    )


def _limits() -> Any:
    from agent_core.domain.schedules import ScheduleDefinitionLimits

    return ScheduleDefinitionLimits(
        max_run_timeout_seconds=3600,
        max_misfire_grace_seconds=86_400,
        max_steps_per_run=64,
        max_model_calls_per_run=64,
        max_tool_calls_per_run=128,
        max_cost_per_run=Decimal("5"),
    )
