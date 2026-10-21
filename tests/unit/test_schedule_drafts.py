"""ADR-0225 part B: a schedule described in plain words becomes a draft.

One bounded, tool-less model call reads the owner's sentence and returns only
the timing and a short title; the server builds the cadence, derives every
pin the chat tool derives, and computes the next firings itself. Nothing is
saved. The owner's sentence is the instruction, unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.agents import AgentSpec
from agent_core.domain.errors import RateLimitedError, ScheduleValidationError
from agent_core.domain.messages import (
    CapabilitySet,
    FakeModelScript,
    ModelCapabilities,
    ModelUsage,
    ProviderPin,
    ResolvedModel,
    ScriptedTurn,
    TextPart,
    UserMessage,
)
from agent_core.domain.runs import RunLimits
from agent_core.domain.schedules import (
    DailyCadence,
    OnceCadence,
    ScheduleDefinitionLimits,
    ScheduleDraftStatus,
    WeeklyCadence,
)
from agent_core.scheduling.defaults import schedule_run_limits
from agent_core.scheduling.drafts import (
    DRAFTS_PER_HOUR,
    ScheduleDrafter,
    ScheduleDraftInterpreter,
)
from agent_core.tools.schedule_create import ScheduleCreateTool
from tests.contract.support import memory_uow_factory, principal

# A Monday morning in Los Angeles.
NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
ZONE = "America/Los_Angeles"
AGENT = AgentSpec(
    id=UUID(int=1501),
    version="1.0.0",
    name="Scheduled agent",
    instructions="Do the scheduled work.",
    model_policy="balanced",
    enabled_tools=[],
    policy_profile="default",
    limits=RunLimits(max_steps=40, max_model_calls=40, max_tool_calls=80, max_cost=Decimal("9")),
)
LIMITS = ScheduleDefinitionLimits(
    max_run_timeout_seconds=900,
    max_misfire_grace_seconds=7200,
    max_steps_per_run=30,
    max_model_calls_per_run=30,
    max_tool_calls_per_run=60,
    max_cost_per_run=Decimal("5"),
)


class _Router:
    async def resolve(
        self, model_policy: str, *, tenant_id: str, required: CapabilitySet | None = None
    ) -> ResolvedModel:
        del tenant_id, required
        return ResolvedModel(
            provider="openai",
            model="draft-model",
            policy_name=model_policy,
            capabilities=ModelCapabilities(structured_output=True),
            resolved_at=NOW,
        )

    async def resolve_pinned(self, pin: ProviderPin) -> ResolvedModel:
        raise AssertionError(f"unexpected pin: {pin}")

    def pin(self, run_id: UUID, resolved: ResolvedModel) -> ProviderPin:
        raise AssertionError(f"unexpected pin: {run_id} {resolved}")


def _answer(**overrides: object) -> str:
    values: dict[str, object] = {
        "status": "ready",
        "reason": "none",
        "title": "Weekday briefing",
        "cadence_kind": "WEEKLY",
        "once_local": "",
        "local_time": "07:00",
        "weekdays": [1, 2, 3, 4, 5],
        "days_of_month": [],
        "last_day": False,
        "dates": [],
    }
    values.update(overrides)
    return json.dumps(values)


async def _drafter(
    *answers: str, usage: ModelUsage | None = None
) -> tuple[ScheduleDrafter, FakeModelProvider, object]:
    clock, factory = await memory_uow_factory()
    del clock
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=answer,
                    usage=usage
                    or ModelUsage(input_tokens=300, output_tokens=40, provider="openai", model="m"),
                )
                for answer in answers
            ]
        ),
        FixedClock(NOW),
    )
    ids = SequenceIdFactory()
    drafter = ScheduleDrafter(
        uow_factory=factory,
        clock=FixedClock(NOW),
        ids=ids,
        agent=AGENT,
        limits=LIMITS,
        interpreter=ScheduleDraftInterpreter(
            router=_Router(),
            providers={"openai": provider},
            clock=FixedClock(NOW),
            ids=ids,
            model_policy="balanced",
        ),
    )
    return drafter, provider, factory


TEXT = "Every weekday at 7 am, tell me what needs me today."


async def test_a_weekday_sentence_becomes_a_weekly_cadence_with_derived_pins() -> None:
    drafter, provider, _ = await _drafter(_answer())

    draft = await drafter.draft(principal(), text=TEXT, timezone=ZONE)

    assert draft.status is ScheduleDraftStatus.READY
    assert draft.definition is not None
    assert draft.definition.cadence == WeeklyCadence(
        local_time=time(7), weekdays=(1, 2, 3, 4, 5), timezone=ZONE
    )
    # The owner's own sentence is the instruction; the model only titles it.
    assert draft.definition.instruction == TEXT
    assert draft.definition.title == "Weekday briefing"
    tool = ScheduleCreateTool(object(), AGENT, LIMITS)  # type: ignore[arg-type]
    assert draft.definition.limits == tool._run_limits() == schedule_run_limits(AGENT, LIMITS)
    assert draft.definition.agent_id == AGENT.id
    assert draft.definition.agent_version == AGENT.version
    assert draft.definition.policy_profile == AGENT.policy_profile
    assert draft.definition.requested_scopes == frozenset()
    assert draft.definition.max_consecutive_failures == 2
    assert draft.definition.run_timeout_seconds == 300
    assert draft.definition.misfire_grace_seconds == 3600
    # Tuesday 07:00 local is the first firing after Monday 08:00 local.
    assert draft.next_fire_at[0] == datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    assert len(draft.next_fire_at) == 3
    request = provider.requests[0]
    assert request.tools == []
    message = request.conversation[-1]
    assert isinstance(message, UserMessage)
    document = json.loads(
        "".join(part.text for part in message.content if isinstance(part, TextPart))
    )
    assert document["text"] == TEXT
    assert document["timezone"] == ZONE
    assert document["now_local"].startswith("2026-10-19T08:00")


async def test_a_one_time_request_resolves_in_the_owners_timezone() -> None:
    drafter, _, _ = await _drafter(
        _answer(cadence_kind="ONCE", once_local="2026-10-21T18:15", local_time="", weekdays=[])
    )

    draft = await drafter.draft(principal(), text="Call Riv on Wednesday at 6:15 pm", timezone=ZONE)

    assert draft.status is ScheduleDraftStatus.READY
    assert draft.definition is not None
    assert draft.definition.cadence == OnceCadence(at=datetime(2026, 10, 22, 1, 15, tzinfo=UTC))
    assert draft.next_fire_at == [datetime(2026, 10, 22, 1, 15, tzinfo=UTC)]


async def test_a_time_already_past_asks_again_instead_of_saving_nothing_useful() -> None:
    drafter, _, _ = await _drafter(
        _answer(cadence_kind="ONCE", once_local="2026-10-18T09:00", local_time="", weekdays=[])
    )

    draft = await drafter.draft(principal(), text="Remind me Sunday at 9", timezone=ZONE)

    assert draft.status is ScheduleDraftStatus.NEEDS_CLARIFICATION
    assert draft.reason == "in_past"
    assert draft.definition is None


async def test_the_models_doubt_is_passed_on_with_no_definition() -> None:
    drafter, _, _ = await _drafter(_answer(status="needs_clarification", reason="no_timing"))

    draft = await drafter.draft(principal(), text="Check the snow report", timezone=ZONE)

    assert draft.status is ScheduleDraftStatus.NEEDS_CLARIFICATION
    assert draft.reason == "no_timing"
    assert draft.definition is None
    assert draft.next_fire_at == []


async def test_an_impossible_cadence_is_a_question_not_an_error() -> None:
    drafter, _, _ = await _drafter(_answer(weekdays=[9]))

    draft = await drafter.draft(principal(), text=TEXT, timezone=ZONE)

    assert draft.status is ScheduleDraftStatus.NEEDS_CLARIFICATION
    assert draft.reason == "ambiguous_timing"


async def test_a_broken_answer_fails_without_raising() -> None:
    drafter, _, _ = await _drafter("not json")

    draft = await drafter.draft(principal(), text=TEXT, timezone=ZONE)

    assert draft.status is ScheduleDraftStatus.FAILED
    assert draft.definition is None


async def test_an_expensive_answer_is_discarded() -> None:
    drafter, _, _ = await _drafter(
        _answer(),
        usage=ModelUsage(
            input_tokens=300, output_tokens=40, cost=Decimal("0.06"), provider="o", model="m"
        ),
    )

    draft = await drafter.draft(principal(), text=TEXT, timezone=ZONE)

    assert draft.status is ScheduleDraftStatus.FAILED


async def test_a_secret_never_reaches_the_model() -> None:
    drafter, provider, _ = await _drafter(_answer())

    with pytest.raises(ScheduleValidationError) as caught:
        await drafter.draft(
            principal(),
            text="Every day post with password: hunter2-correct-horse",
            timezone=ZONE,
        )

    assert caught.value.reason == "schedule.instruction_secret"
    assert provider.requests == []


@pytest.mark.parametrize("zone", ["Mars/Olympus", ""])
async def test_an_unknown_timezone_is_refused(zone: str) -> None:
    drafter, provider, _ = await _drafter(_answer())

    with pytest.raises(ScheduleValidationError):
        await drafter.draft(principal(), text=TEXT, timezone=zone)

    assert provider.requests == []


async def test_drafts_are_limited_per_hour() -> None:
    drafter, provider, factory = await _drafter(*[_answer()] * (DRAFTS_PER_HOUR + 1))

    for _ in range(DRAFTS_PER_HOUR):
        await drafter.draft(principal(), text=TEXT, timezone=ZONE)
    with pytest.raises(RateLimitedError):
        await drafter.draft(principal(), text=TEXT, timezone=ZONE)

    assert len(provider.requests) == DRAFTS_PER_HOUR
    async with factory() as uow:  # type: ignore[operator]
        events = await uow.process_events.list("schedule.draft_interpreted")
    assert len(events) == DRAFTS_PER_HOUR
    # Content-free: the sentence and the title never enter the event.
    assert all(TEXT not in event.model_dump_json() for event in events)


async def test_a_daily_draft_lists_its_next_three_firings() -> None:
    drafter, _, _ = await _drafter(_answer(cadence_kind="DAILY", weekdays=[], local_time="06:30"))

    draft = await drafter.draft(principal(), text="Daily at 6:30 check the snow", timezone=ZONE)

    assert draft.definition is not None
    assert draft.definition.cadence == DailyCadence(local_time=time(6, 30), timezone=ZONE)
    first = datetime(2026, 10, 20, 13, 30, tzinfo=UTC)
    assert draft.next_fire_at == [first, first + timedelta(days=1), first + timedelta(days=2)]
