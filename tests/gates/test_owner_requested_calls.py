"""ADR-0213: a call the owner asks for, now or at a scheduled time, is placed without a prompt.

The 2026-10-17 regression: "call Riv at 6:15" produced a schedule with no
delegated scopes, so the occurrence had no call tool and reported that no
calling integration was available.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.calls import CallRecord
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
)
from agent_core.domain.notifications import NotificationKind
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.schedules import CallPlan, ScheduleDefinition, ScheduleState
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import ScheduleMaterializer
from bland_mcp.client import BlandClient
from bland_mcp.server import create_server
from tests.contract.test_bland_client_contract import CALL_ID, KEY, NUMBER, RECIPIENT
from tests.gates.test_call_lifecycle import arguments
from tests.gates.test_call_m27 import call_configuration
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 10, 17, 22, 52, tzinfo=UTC)
FIRE_AT = NOW + timedelta(hours=2, minutes=23)
CALL_SCOPES = frozenset({"mcp.bland_call.use", "mcp.bland_read.use"})


async def discovery(mode: str) -> MCPDiscovery:
    catalog = create_server(mode, BlandClient(KEY, NUMBER), call_configuration().model_dump())
    return MCPDiscovery(
        tools=tuple(
            MCPRemoteTool(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
                application_only=(tool.meta or {}).get("veetbot/application-only") is True,
            )
            for tool in await catalog.list_tools()
        )
    )


async def call_factory(*, read_tools: bool = False, calls: int = 1) -> ScriptedMCPClientFactory:
    provider_ids = [CALL_ID, *(f"00000000-0000-4000-8000-{n:012d}" for n in range(1, calls))]
    return ScriptedMCPClientFactory(
        {
            "bland_read": ScriptedMCPServer(
                name="bland_read",
                discovery=await discovery("read") if read_tools else MCPDiscovery(),
            ),
            "bland_call": ScriptedMCPServer(
                name="bland_call",
                discovery=await discovery("call"),
                responses=tuple(
                    ScriptedMCPResponse(
                        name="start_call",
                        result=MCPCallResult(
                            structured={"provider_call_id": provider_id, "status": "accepted"}
                        ),
                    )
                    for provider_id in provider_ids
                ),
            ),
        }
    )


def call_settings(**changes: Any) -> Settings:
    configuration = call_configuration()
    return replace(
        memory_settings(),
        call_enabled=True,
        call_configuration=configuration,
        credentials={
            name: SecretStr(
                json.dumps({"api_key": KEY, "configuration": configuration.model_dump()})
            )
            for name in ("bland_read", "bland_call")
        },
        **changes,
    )


def placed(factory: ScriptedMCPClientFactory) -> int:
    return sum(client.call_count for client in factory.created)


async def test_an_owner_asked_call_is_placed_without_a_prompt() -> None:
    factory = await call_factory()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="mcp.bland_call.start_call", arguments=arguments())
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I called and asked.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=call_settings(), script=script, sequential_ids=True, mcp_client_factory=factory
    ) as app:
        run_id = await app.runs.submit(f"Call the shop at {RECIPIENT} about ticket 12.")
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=5)
        pending = await app.approvals.list_pending(run_id=run_id)

    assert terminal.status is RunStatus.COMPLETED
    assert pending == []
    assert placed(factory) == 1


async def test_a_number_the_owner_did_not_give_keeps_the_card() -> None:
    """ADR-0213: content or the model alone cannot choose who is called."""
    factory = await call_factory()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="mcp.bland_call.start_call", arguments=arguments())
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="unused", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=call_settings(), script=script, sequential_ids=True, mcp_client_factory=factory
    ) as app:
        run_id = await app.runs.submit("Call the shop and ask about ticket 12.")
        assert len(await app.approvals.list_pending(run_id=run_id)) == 1
    assert placed(factory) == 0


async def test_the_kill_switch_restores_the_confirmation() -> None:
    factory = await call_factory()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="mcp.bland_call.start_call", arguments=arguments())
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="unused", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=call_settings(call_owner_requests_enabled=False),
        script=script,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        run_id = await app.runs.submit(f"Call the shop at {RECIPIENT} about ticket 12.")
        assert len(await app.approvals.list_pending(run_id=run_id)) == 1
    assert placed(factory) == 0


async def test_a_scheduled_owner_call_is_placed_at_its_time_without_a_prompt() -> None:
    """The Riv reminder: one request, no confirmation, and the call is made at 6:15."""
    factory = await call_factory()
    instruction = (
        f"Place one call to Riv at {arguments()['phone_number']} and remind her to "
        "throw the ball for Marzipan. Leave a voicemail if she does not answer."
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create",
                        arguments={
                            "title": "Call Riv: throw the ball for Marzipan",
                            "instruction": instruction,
                            "at": FIRE_AT.isoformat(),
                            "place_call": True,
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Scheduled for 6:15 p.m.", stop_reason=StopReason.END_TURN),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="mcp.bland_call.start_call", arguments=arguments())
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I called Riv.", stop_reason=StopReason.END_TURN),
        ]
    )
    settings = call_settings(schedule_api_enabled=True, schedule_worker_enabled=True)
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        chat = await app.runs.submit(
            f"Call Riv at {RECIPIENT} at 6:15 tonight to remind her to throw the ball."
        )
        created = await asyncio.wait_for(app.runs.wait_terminal(chat), timeout=5)
        assert created.status is RunStatus.COMPLETED, created.failure
        assert await app.approvals.list_pending(run_id=chat) == []
        [record] = (await app.schedules.list(app.principal, 10, None)).items
        assert record.revision.requested_scopes == CALL_SCOPES
        assert placed(factory) == 0

        run = await fire(app, record.schedule.id)
        pending = await app.approvals.list_pending(run_id=run.id)

    assert run.principal_scopes == CALL_SCOPES
    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.final_message == "I called Riv."
    assert pending == []
    assert placed(factory) == 1


async def fire(app: Composition, schedule_id: UUID, at: datetime = FIRE_AT) -> Run:
    """Advance to the firing time, materialize the occurrence and run it."""
    assert isinstance(app.clock, FixedClock)
    app.clock.advance(at - app.clock.now())
    materializer = ScheduleMaterializer(
        uow_factory=app.uow_factory,
        principals=StaticSchedulePrincipalDirectory(app.principal),
        admission=AllowScheduleAdmissionController(),
        clock=app.clock,
        ids=app.ids,
        seed_checkpoint=DurableCheckpointSeeder(app.clock),
    )
    occurrence = await materializer.materialize(schedule_id)
    assert occurrence is not None and occurrence.run_id is not None
    await app.executor.execute(occurrence.run_id)
    async with app.uow_factory() as uow:
        return await uow.runs.get(occurrence.run_id, app.principal)


async def test_a_call_occurrence_that_reads_call_records_first_keeps_the_card() -> None:
    """The 2026-10-20 wake-up call, as ADR-0213 decides it.

    The occurrence's instruction asked it to check earlier outcomes, so its first
    step read the call list. That result is call-record content, labelled
    untrusted even when the list is empty, so ADR-0213's authority is gone for
    every later call in the run and the owner had to confirm on a card.
    """
    factory = await call_factory(read_tools=True)
    instruction = (
        f"Call Cheryl at {arguments()['phone_number']} and wake her for her hair "
        "appointment. Check the preceding call outcomes before each retry."
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create",
                        arguments={
                            "title": "Wake Cheryl",
                            "instruction": instruction,
                            "at": FIRE_AT.isoformat(),
                            "place_call": True,
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Scheduled.", stop_reason=StopReason.END_TURN),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="mcp.bland_read.list_calls", arguments={"limit": 10})
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="mcp.bland_call.start_call", arguments=arguments())
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="unused", stop_reason=StopReason.END_TURN),
        ]
    )
    settings = call_settings(schedule_api_enabled=True, schedule_worker_enabled=True)
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        chat = await app.runs.submit(
            f"Call Cheryl at {RECIPIENT} at 9:30 tomorrow and make sure she is awake."
        )
        created = await asyncio.wait_for(app.runs.wait_terminal(chat), timeout=5)
        assert created.status is RunStatus.COMPLETED, created.failure
        assert await app.approvals.list_pending(run_id=chat) == []
        [record] = (await app.schedules.list(app.principal, 10, None)).items
        run = await fire(app, record.schedule.id)
        pending = await app.approvals.list_pending(run_id=run.id)
        async with app.uow_factory() as uow:
            reservations = await uow.calls.list(app.principal, "dispatch")

    assert run.principal_scopes == CALL_SCOPES
    assert run.status is RunStatus.WAITING_FOR_APPROVAL
    assert [approval.tool_name for approval in pending] == ["mcp.bland_call.start_call"]
    assert reservations == []


async def test_a_call_schedule_tells_chat_it_arranges_no_retries_or_follow_ups() -> None:
    """The 2026-10-20 regression: Chat promised retries the occurrence could never arrange.

    An occurrence holds only its schedule's calling scopes, so it cannot create
    a schedule, and it runs once. The result says so, so Chat tells the owner
    which steps will not happen instead of confirming them.
    """
    from typing import cast

    from agent_core.tools.registry import RegisteredTool
    from agent_core.tools.schedule_create import ScheduleCreateTool
    from tests.contract.support import tool_context

    settings = call_settings(schedule_api_enabled=True, schedule_worker_enabled=True)
    async with build(settings=settings, fixed_clock_at=NOW) as app:
        registered = cast(RegisteredTool, app.tool_pipeline._registry.get("schedule.create"))
        tool = cast(ScheduleCreateTool, registered.implementation)
        context = replace(tool_context(), principal=app.principal)
        call = await tool.execute(
            {
                "title": "Wake Cheryl",
                "instruction": f"Call Cheryl at {RECIPIENT}; retry every 2 minutes until 9:40.",
                "at": FIRE_AT.isoformat(),
                "place_call": True,
            },
            replace(context, idempotency_key="wake-cheryl"),
        )
        reminder = await tool.execute(
            {"title": "Stretch", "instruction": "Remind me to stretch.", "at": FIRE_AT.isoformat()},
            replace(context, idempotency_key="stretch"),
        )

    assert call.ok is True and reminder.ok is True
    assert call.structured is not None and reminder.structured is not None
    [narration] = [part.text for part in call.content if isinstance(part, TextPart)]
    assert narration.startswith(f"Created schedule {call.structured['schedule_id']} for ")
    assert "cannot create or change schedules" in narration
    assert "no retry or follow-up" in narration
    [plain] = [part.text for part in reminder.content if isinstance(part, TextPart)]
    assert plain == (
        f"Created schedule {reminder.structured['schedule_id']} for {FIRE_AT.isoformat()}."
    )


async def test_a_call_schedule_fails_at_creation_when_calling_is_off() -> None:
    """Never accept a call schedule that cannot call when it fires."""
    from typing import cast

    from agent_core.tools.registry import RegisteredTool
    from agent_core.tools.schedule_create import ScheduleCreateTool
    from tests.contract.support import tool_context

    settings = replace(memory_settings(), schedule_api_enabled=True, schedule_worker_enabled=True)
    async with build(settings=settings, fixed_clock_at=NOW) as app:
        registered = cast(RegisteredTool, app.tool_pipeline._registry.get("schedule.create"))
        tool = cast(ScheduleCreateTool, registered.implementation)
        result = await tool.execute(
            {
                "title": "Call Riv",
                "instruction": "Call Riv and remind her to throw the ball.",
                "at": FIRE_AT.isoformat(),
                "place_call": True,
            },
            replace(tool_context(), principal=app.principal, idempotency_key="call-riv"),
        )
        page = await app.schedules.list(app.principal, 10, None)

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.reason_code == "schedule.calling_unavailable"
    assert page.items == []


def test_the_schedule_role_vouches_for_call_scopes_only_when_calling_is_enabled() -> None:
    from agent_core.bootstrap import _schedule_principal

    base = replace(
        memory_settings(),
        auth_scopes=frozenset({"schedule.read", "schedule.write"}),
    )
    assert not CALL_SCOPES & _schedule_principal(base).scopes
    enabled = replace(base, call_enabled=True, call_configuration=call_configuration())
    assert _schedule_principal(enabled).scopes >= CALL_SCOPES


def plan_turns(calls: int, report: str) -> list[ScriptedTurn]:
    """Chat creates the plan; each attempt places its call; the report run only reports."""
    return [
        ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name="schedule.create",
                    arguments={
                        "title": "Wake Cheryl",
                        "instruction": (
                            f"Call Cheryl at {arguments()['phone_number']} and wake her for her "
                            "hair appointment. On the follow-up, check she is still awake."
                        ),
                        "at": FIRE_AT.isoformat(),
                        "place_call": True,
                        "call_plan": {
                            "retry_every_minutes": 2,
                            "retry_until": (FIRE_AT + timedelta(minutes=10)).isoformat(),
                            "follow_up_after_answer_minutes": 5,
                        },
                    },
                )
            ],
            stop_reason=StopReason.TOOL_USE,
        ),
        ScriptedTurn(text="Scheduled.", stop_reason=StopReason.END_TURN),
        *(
            turn
            for _ in range(calls)
            for turn in (
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(name="mcp.bland_call.start_call", arguments=arguments())
                    ],
                    stop_reason=StopReason.TOOL_USE,
                ),
                ScriptedTurn(text="I placed the call.", stop_reason=StopReason.END_TURN),
            )
        ),
        ScriptedTurn(text=report, stop_reason=StopReason.END_TURN),
    ]


async def settle(app: Composition, run: Run, status: str, duration: float = 0.5) -> None:
    """Record a call's closed result as reconciliation would."""
    async with app.uow_factory() as uow:
        [dispatch] = [
            row
            for row in await uow.calls.list(app.principal, "dispatch")
            if row.payload.get("run_id") == str(run.id)
        ]
        provider_id = str(dispatch.payload["provider_call_id"])
        now = app.clock.now()
        await uow.calls.put(
            dispatch.model_copy(
                update={
                    "revision": dispatch.revision + 1,
                    "payload": {**dispatch.payload, "active": False, "status": status},
                    "updated_at": now,
                }
            ),
            expected_revision=dispatch.revision,
        )
        existing = await uow.calls.get(app.principal, "call", provider_id)
        await uow.calls.put(
            CallRecord(
                tenant_id=app.principal.tenant_id,
                principal_id=app.principal.principal_id,
                kind="call",
                key=provider_id,
                revision=1 if existing is None else existing.revision + 1,
                payload={
                    "provider_call_id": provider_id,
                    "direction": "outbound",
                    "status": status,
                    "created_at": run.created_at.isoformat(),
                    "duration_minutes": duration,
                    "summary": "She said she was awake.",
                    "transcript": "",
                },
                created_at=now,
                updated_at=now,
            ),
            expected_revision=0 if existing is None else existing.revision,
        )


async def seed_text(app: Composition, run: Run) -> str:
    async with app.uow_factory() as uow:
        seed = await uow.events.latest_before(
            run.session_id, run.seed_event_sequence + 1, "user.message.created", app.principal
        )
    assert seed is not None
    content = seed.payload["content"]
    assert isinstance(content, str)
    return content


async def test_a_call_plan_retries_follows_up_and_reports_without_a_prompt() -> None:
    """ADR-0221: the 2026-10-20 wake-up plan, run by the platform from call results.

    No answer at 9:30, an answer at 9:32, the follow-up five minutes after that
    call ended, then a report. Nothing asks the owner, no call record reaches a
    model, and the report run cannot call.
    """
    factory = await call_factory(calls=3)
    script = FakeModelScript(turns=plan_turns(3, "Cheryl answered and is awake."))
    settings = call_settings(schedule_api_enabled=True, schedule_worker_enabled=True)
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        assert app.call_plans is not None
        chat = await app.runs.submit(
            f"Call Cheryl at {RECIPIENT} at 9:30 to wake her. If she doesn't answer, call "
            "every 2 minutes until 9:40. If she answers, call 5 minutes later."
        )
        created = await asyncio.wait_for(app.runs.wait_terminal(chat), timeout=5)
        assert created.status is RunStatus.COMPLETED, created.failure
        [record] = (await app.schedules.list(app.principal, 10, None)).items
        schedule_id = record.schedule.id
        assert record.revision.requested_scopes == {"mcp.bland_call.use"}
        assert record.revision.call_plan == CallPlan(
            retry_every_minutes=2,
            retry_until=FIRE_AT + timedelta(minutes=10),
            follow_up_after_answer_minutes=5,
        )

        first = await fire(app, schedule_id)
        assert first.status is RunStatus.COMPLETED, first.failure
        assert first.principal_scopes == {"mcp.bland_call.use"}
        assert "call 1 of up to 7" in await seed_text(app, first)
        waiting = await app.schedules.get(app.principal, schedule_id)
        assert waiting.schedule.state is ScheduleState.ACTIVE
        assert waiting.schedule.next_fire_at is None

        await app.call_plans.run_once()
        held = await app.schedules.get(app.principal, schedule_id)
        assert held.schedule.next_fire_at is None, "no result yet, so no retry"

        await settle(app, first, "no_answer")
        await app.call_plans.run_once()
        retrying = await app.schedules.get(app.principal, schedule_id)
        assert retrying.schedule.next_fire_at == FIRE_AT + timedelta(minutes=2)

        second = await fire(app, schedule_id, FIRE_AT + timedelta(minutes=2))
        assert second.status is RunStatus.COMPLETED, second.failure
        assert "Earlier calls: " in await seed_text(app, second)
        await settle(app, second, "completed", duration=1.0)
        await app.call_plans.run_once()
        following = await app.schedules.get(app.principal, schedule_id)
        follow_up_at = FIRE_AT + timedelta(minutes=8)
        assert following.schedule.next_fire_at == follow_up_at

        third = await fire(app, schedule_id, follow_up_at)
        assert third.status is RunStatus.COMPLETED, third.failure
        assert "follow-up call" in await seed_text(app, third)
        await settle(app, third, "completed")
        await app.call_plans.run_once()

        ending = await app.schedules.get(app.principal, schedule_id)
        assert ending.schedule.next_fire_at is not None
        report = await fire(app, schedule_id, ending.schedule.next_fire_at)
        assert report.status is RunStatus.COMPLETED, report.failure
        assert report.principal_scopes == set()
        assert "Do not place any call" in await seed_text(app, report)
        assert report.final_message == "Cheryl answered and is awake."
        done = await app.schedules.get(app.principal, schedule_id)
        pending = [
            approval
            for run in (first, second, third, report)
            for approval in await app.approvals.list_pending(run_id=run.id)
        ]

    assert done.schedule.state is ScheduleState.COMPLETED
    assert pending == []
    assert placed(factory) == 3


async def test_a_call_plan_that_never_reaches_anyone_ends_with_a_report() -> None:
    """Six unanswered calls, then one report; only the report alerts the owner."""
    factory = await call_factory(calls=6)
    script = FakeModelScript(turns=plan_turns(6, "Cheryl did not answer any call."))
    settings = call_settings(
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        assert app.call_plans is not None
        chat = await app.runs.submit(f"Call Cheryl at {RECIPIENT} at 9:30, retrying until 9:40.")
        await asyncio.wait_for(app.runs.wait_terminal(chat), timeout=5)
        [record] = (await app.schedules.list(app.principal, 10, None)).items
        for minutes in range(0, 11, 2):
            run = await fire(app, record.schedule.id, FIRE_AT + timedelta(minutes=minutes))
            assert run.principal_scopes == {"mcp.bland_call.use"}
            await settle(app, run, "voicemail" if minutes % 4 else "no_answer")
            await app.call_plans.run_once()
        ending = await app.schedules.get(app.principal, record.schedule.id)
        assert ending.schedule.next_fire_at is not None
        report = await fire(app, record.schedule.id, ending.schedule.next_fire_at)
        note = await seed_text(app, report)
        done = await app.schedules.get(app.principal, record.schedule.id)
        async with app.uow_factory() as uow:
            alerts = [
                row
                for row in await uow.notification_outbox.list(app.principal, limit=100)
                if row.kind is NotificationKind.SCHEDULE_RUN_FINISHED
            ]

    assert [alert.run_id for alert in alerts] == [report.id]
    assert "No call was answered." in note
    assert note.count(" UTC ") == 6
    assert f"{FIRE_AT:%H:%M} UTC no answer" in note
    assert report.principal_scopes == set()
    assert done.schedule.state is ScheduleState.COMPLETED
    assert placed(factory) == 6


async def test_a_call_plan_needs_a_one_time_call_schedule() -> None:
    from typing import cast

    from agent_core.tools.registry import RegisteredTool
    from agent_core.tools.schedule_create import ScheduleCreateTool
    from tests.contract.support import tool_context

    settings = call_settings(schedule_api_enabled=True, schedule_worker_enabled=True)
    plan: dict[str, Any] = {
        "retry_every_minutes": 2,
        "retry_until": (FIRE_AT + timedelta(minutes=10)).isoformat(),
    }
    async with build(settings=settings, fixed_clock_at=NOW) as app:
        registered = cast(RegisteredTool, app.tool_pipeline._registry.get("schedule.create"))
        tool = cast(ScheduleCreateTool, registered.implementation)
        context = replace(tool_context(), principal=app.principal)
        base: dict[str, Any] = {
            "title": "Wake Cheryl",
            "instruction": f"Call Cheryl at {RECIPIENT}.",
        }
        candidates: tuple[dict[str, Any], ...] = (
            {**base, "at": FIRE_AT.isoformat(), "call_plan": plan},
            {
                **base,
                "cadence": {"kind": "DAILY", "local_time": "09:30", "timezone": "UTC"},
                "place_call": True,
                "call_plan": plan,
            },
            {
                **base,
                "at": FIRE_AT.isoformat(),
                "place_call": True,
                "call_plan": {**plan, "retry_every_minutes": 1},
            },
            {
                **base,
                "at": FIRE_AT.isoformat(),
                "place_call": True,
                "call_plan": {"retry_every_minutes": 2},
            },
        )
        refused = [
            await tool.execute(arguments, replace(context, idempotency_key=f"plan-{index}"))
            for index, arguments in enumerate(candidates)
        ]
        page = await app.schedules.list(app.principal, 10, None)

    assert [result.failure.reason_code for result in refused if result.failure] == [
        "schedule.call_plan_needs_call",
        "schedule.call_plan_needs_one_time",
        "schedule.call_plan_invalid",
        "schedule.call_plan_invalid",
    ]
    assert page.items == []


async def test_a_call_plan_cannot_change_after_its_first_call() -> None:
    """The owner approved the whole plan; a later edit cannot redirect its calls."""
    from agent_core.domain.errors import ConflictError
    from agent_core.domain.schedules import ScheduleDefinitionPatch

    factory = await call_factory(calls=1)
    script = FakeModelScript(turns=plan_turns(1, "unused"))
    settings = call_settings(schedule_api_enabled=True, schedule_worker_enabled=True)
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        chat = await app.runs.submit(f"Call Cheryl at {RECIPIENT} at 9:30, retrying until 9:40.")
        await asyncio.wait_for(app.runs.wait_terminal(chat), timeout=5)
        [record] = (await app.schedules.list(app.principal, 10, None)).items
        await fire(app, record.schedule.id)
        current = await app.schedules.get(app.principal, record.schedule.id)
        definition = current.revision.model_dump(
            exclude={"schedule_id", "revision", "timezone", "created_by_principal_id", "created_at"}
        )
        with pytest.raises(ConflictError) as patched:
            await app.schedules.patch(
                app.principal,
                record.schedule.id,
                current.schedule.current_revision,
                ScheduleDefinitionPatch(instruction="Call +14155550199 instead."),
                "edit-plan",
            )
        with pytest.raises(ConflictError) as replaced:
            await app.schedules.update(
                app.principal,
                record.schedule.id,
                current.schedule.current_revision,
                ScheduleDefinition.model_validate(
                    {**definition, "instruction": "Call +14155550199 instead."}
                ),
            )

    reasons = [patched.value.reason, replaced.value.reason]
    assert reasons == ["schedule.call_plan_started", "schedule.call_plan_started"]
