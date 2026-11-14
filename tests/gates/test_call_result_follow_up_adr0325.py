"""ADR-0325: Chat reports a call's outcome in the conversation that placed it.

The 2026-11-13 regression: the owner asked Chat to call, Chat answered "the
call was placed", and nothing ever said whether anyone answered or what they
said. The provider's result arrived and was stored; only a push pointed at it.
"""

import asyncio
from datetime import timedelta
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.application.call_authorization import _PHONE_NUMBER
from agent_core.bootstrap import Composition, build
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import Run, RunStatus
from tests.contract.test_bland_client_contract import CALL_ID, RECIPIENT
from tests.gates.test_call_lifecycle import arguments
from tests.gates.test_owner_requested_calls import (
    FIRE_AT,
    NOW,
    call_factory,
    call_settings,
    fire,
    placed,
    settle,
)

REPORT = "She answered and said she was awake."


def turns(*, report: bool = True) -> list[ScriptedTurn]:
    placed_call = [
        ScriptedTurn(
            # Explicit identifiers: one conversation holds every run's tool pairs.
            tool_calls=[
                ScriptedToolCall(
                    name="mcp.bland_call.start_call", arguments=arguments(), call_id="place"
                )
            ],
            stop_reason=StopReason.TOOL_USE,
        ),
        ScriptedTurn(text="I placed the call.", stop_reason=StopReason.END_TURN),
    ]
    if not report:
        return placed_call
    return [
        *placed_call,
        ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name="mcp.bland_read.get_call", arguments={"call_id": CALL_ID}, call_id="read"
                )
            ],
            stop_reason=StopReason.TOOL_USE,
        ),
        ScriptedTurn(text=REPORT, stop_reason=StopReason.END_TURN),
    ]


async def place(app: Composition) -> Run:
    run_id = await app.runs.submit(f"Call Cheryl at {RECIPIENT} and check she is awake.")
    run = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=5)
    assert run.status is RunStatus.COMPLETED, run.failure
    return run


async def session_runs(app: Composition, session_id: UUID) -> list[Run]:
    async with app.uow_factory() as uow:
        events = await uow.events.list_after(session_id, 0, app.principal)
        ids = list(dict.fromkeys(event.run_id for event in events if event.run_id is not None))
        return [await uow.runs.get(run_id, app.principal) for run_id in ids]


async def test_chat_reports_the_outcome_in_the_conversation_that_placed_the_call() -> None:
    factory = await call_factory(read_tools=True)
    async with build(
        settings=call_settings(),
        script=FakeModelScript(turns=turns()),
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        assert app.call_follow_ups is not None
        call_run = await place(app)

        assert await app.call_follow_ups.run_once() == 0, "no result yet, so nothing to report"
        await settle(app, call_run, "completed")
        assert await app.call_follow_ups.run_once() == 1

        [first, follow_up] = await session_runs(app, call_run.session_id)
        assert first.id == call_run.id
        follow_up = await asyncio.wait_for(app.runs.wait_terminal(follow_up.id), timeout=5)
        assert await app.call_follow_ups.run_once() == 0, "one report per call"
        async with app.uow_factory() as uow:
            seed = await uow.events.latest_before(
                follow_up.session_id,
                follow_up.seed_event_sequence + 1,
                "user.message.created",
                app.principal,
            )
            shown = await uow.events.list_conversation_after(
                follow_up.session_id, 0, app.principal, limit=100
            )
        pending = await app.approvals.list_pending(run_id=follow_up.id)

    assert follow_up.status is RunStatus.COMPLETED, follow_up.failure
    assert follow_up.final_message == REPORT
    # The unattended run can read the call and nothing else; it cannot place one.
    assert follow_up.principal_scopes == {"mcp.bland_read.use"}
    assert pending == []
    assert placed(factory) == 1
    # The platform's note is context only: not the owner's words, and not shown as theirs.
    assert seed is not None and seed.run_id == follow_up.id
    assert seed.actor_type == "calling"
    note = seed.payload["content"]
    assert isinstance(note, str)
    assert "answered" in note
    assert "She said she was awake." not in note, "the note carries no call content"
    assert _PHONE_NUMBER.findall(note) == [], "nothing the number check could read as dialable"
    assert seed.sequence not in {event.sequence for event in shown}
    assert REPORT in str([event.payload for event in shown])


async def test_a_report_waits_while_the_owner_is_mid_turn_and_never_answers_a_question() -> None:
    factory = await call_factory(read_tools=True)
    script = FakeModelScript(
        turns=[
            *turns(report=False),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="conversation.ask_user",
                        arguments={"question": "Which day?"},
                        call_id="ask",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
        ]
    )
    async with build(
        settings=call_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        assert app.call_follow_ups is not None
        call_run = await place(app)
        await settle(app, call_run, "completed")
        asking = await app.runs.submit("Also book the salon.", call_run.session_id)
        for _ in range(200):
            if (await app.runs.get(asking)).status is RunStatus.WAITING_FOR_USER:
                break
            await asyncio.sleep(0.01)
        assert (await app.runs.get(asking)).status is RunStatus.WAITING_FOR_USER

        assert await app.call_follow_ups.run_once() == 0
        runs = await session_runs(app, call_run.session_id)

    assert [run.id for run in runs] == [call_run.id, asking]


async def test_a_call_dispatched_before_this_decision_is_never_reported() -> None:
    """Deploying must not post a report into every old conversation that placed a call."""
    factory = await call_factory(read_tools=True)
    async with build(
        settings=call_settings(),
        script=FakeModelScript(turns=turns(report=False)),
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        assert app.call_follow_ups is not None
        call_run = await place(app)
        async with app.uow_factory() as uow:
            [dispatch] = await uow.calls.list(app.principal, "dispatch")
            legacy = {k: v for k, v in dispatch.payload.items() if k != "follow_up"}
            await uow.calls.put(
                dispatch.model_copy(update={"revision": dispatch.revision + 1, "payload": legacy}),
                expected_revision=dispatch.revision,
            )
        await settle(app, call_run, "completed")

        assert await app.call_follow_ups.run_once() == 0
        runs = await session_runs(app, call_run.session_id)

    assert [run.id for run in runs] == [call_run.id]


async def test_a_result_that_arrives_too_late_is_left_to_the_push() -> None:
    factory = await call_factory(read_tools=True)
    async with build(
        settings=call_settings(),
        script=FakeModelScript(turns=turns(report=False)),
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        assert app.call_follow_ups is not None and isinstance(app.clock, FixedClock)
        call_run = await place(app)
        await settle(app, call_run, "completed")
        app.clock.advance(timedelta(minutes=31))

        assert await app.call_follow_ups.run_once() == 0
        async with app.uow_factory() as uow:
            [dispatch] = await uow.calls.list(app.principal, "dispatch")
        runs = await session_runs(app, call_run.session_id)

    assert dispatch.payload["follow_up"] == "expired"
    assert [run.id for run in runs] == [call_run.id]


async def test_a_scheduled_call_is_not_reported_a_second_time() -> None:
    """A scheduled occurrence reads its own outcome and has its own alert."""
    factory = await call_factory(read_tools=True)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create",
                        arguments={
                            "title": "Call Riv",
                            "instruction": f"Place one call to Riv at {RECIPIENT}.",
                            "at": FIRE_AT.isoformat(),
                            "place_call": True,
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Scheduled.", stop_reason=StopReason.END_TURN),
            *turns(report=False),
        ]
    )
    async with build(
        settings=call_settings(schedule_api_enabled=True, schedule_worker_enabled=True),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        assert app.call_follow_ups is not None
        chat = await app.runs.submit(f"Call Riv at {RECIPIENT} at 6:15 tonight.")
        created = await asyncio.wait_for(app.runs.wait_terminal(chat), timeout=5)
        assert created.status is RunStatus.COMPLETED, created.failure
        [record] = (await app.schedules.list(app.principal, 10, None)).items
        occurrence = await fire(app, record.schedule.id)
        assert occurrence.status is RunStatus.COMPLETED, occurrence.failure
        await settle(app, occurrence, "completed")

        assert await app.call_follow_ups.run_once() == 0
        async with app.uow_factory() as uow:
            [dispatch] = await uow.calls.list(app.principal, "dispatch")
        runs = await session_runs(app, occurrence.session_id)

    assert dispatch.payload["follow_up"] == "skipped"
    assert [run.id for run in runs] == [occurrence.id]


async def test_the_kill_switch_leaves_results_to_the_push_alone() -> None:
    factory = await call_factory()
    async with build(
        settings=call_settings(call_result_follow_up_enabled=False),
        script=FakeModelScript(turns=turns(report=False)),
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as app:
        assert app.call_follow_ups is None
