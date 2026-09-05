"""Milestone 11 atomic materialization gates."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    AssistantMessage,
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolCallItem,
    UserMessage,
)
from agent_core.domain.policies import PolicyProfileRecord, TrustLevel
from agent_core.domain.runs import RunLimits, RunStatus, Step
from agent_core.domain.schedules import (
    DailyCadence,
    OccurrenceDisposition,
    OnceCadence,
    Schedule,
    ScheduleAdmissionDecision,
    ScheduleAdmissionOutcome,
    ScheduleOccurrence,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.ports.schedules import ScheduleAdmissionController
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import ScheduleMaterializer
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)
SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000000411")
AGENT_ID = UUID("00000000-0000-0000-0000-000000000412")


def _principal(*, scopes: set[str] | None = None) -> Principal:
    return Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes={"memory.read"} if scopes is None else scopes,
    )


def _agent() -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="Scheduled agent",
        instructions="Use the scheduled instruction.",
        model_policy="fake",
        enabled_tools=["workspace.write_text"],
        policy_profile="default",
        limits=RunLimits(),
    )


def _schedule() -> Schedule:
    return Schedule(
        id=SCHEDULE_ID,
        tenant_id="local",
        principal_id="local-user",
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _revision() -> ScheduleRevision:
    return ScheduleRevision(
        schedule_id=SCHEDULE_ID,
        revision=1,
        title="Daily briefing",
        instruction="Summarize project changes.",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=frozenset({"memory.read"}),
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("1"),
        ),
        run_timeout_seconds=60,
        cadence=OnceCadence(at=NOW),
        timezone=None,
        misfire_grace_seconds=60,
        max_consecutive_failures=3,
        created_by_principal_id="local-user",
        created_at=NOW,
    )


def _profile() -> PolicyProfileRecord:
    return PolicyProfileRecord(
        policy_version="default@v1",
        profile_name="default",
        profile_sha256="a" * 64,
        hardline_sha256="b" * 64,
        rule_count=1,
        loaded_at=NOW,
        loaded_by="test",
    )


@asynccontextmanager
async def _materialize(
    principal: Principal,
    *,
    enabled: bool = True,
    admission: ScheduleAdmissionController | None = None,
) -> AsyncIterator[tuple[Composition, ScheduleOccurrence | None]]:
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.policy_profiles.record(_profile())
            await uow.schedules.create(_schedule(), _revision())
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal, enabled=enabled),
            admission=admission or AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        yield composition, await materializer.materialize(SCHEDULE_ID)


async def test_due_schedule_materializes_one_complete_ordinary_run() -> None:
    async with _materialize(_principal()) as (composition, occurrence):
        assert occurrence is not None
        assert occurrence.disposition is OccurrenceDisposition.MATERIALIZED
        assert occurrence.session_id is not None
        assert occurrence.run_id is not None
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(occurrence.session_id, _principal())
            run = await uow.runs.get(occurrence.run_id, _principal())
            assert session.metadata["schedule_id"] == str(SCHEDULE_ID)
            assert run.status is RunStatus.QUEUED
            assert run.priority == 10
            assert run.principal_scopes == {"memory.read"}
            assert run.limits.model_dump(exclude={"deadline_at"}) == (
                _revision().limits.model_dump(exclude={"deadline_at"})
            )
            assert run.deadline_at == datetime(2026, 8, 20, 16, 1, tzinfo=UTC)
            checkpoint = await uow.checkpoints.latest(run.id)
            assert checkpoint is not None
            agent = await uow.agents.get_version(run.agent_id, run.agent_version)
            events = await uow.events.list_after(run.session_id, 0, _principal())
            assert [event.event_type for event in events] == [
                "session.created",
                "user.message.created",
                "run.queued",
                "run.checkpointed",
            ]
        scheduled_principal = _principal().model_copy(
            update={"scopes": set(run.principal_scopes)}, deep=True
        )
        result = await composition.tool_pipeline.dispatch(
            run=run,
            checkpoint=checkpoint,
            tool_calls=[
                ToolCallItem(
                    call_id="undeclared-scheduled-write",
                    item_index=0,
                    name="workspace.write_text",
                    arguments={"path": "should-not-exist.txt", "content": "denied"},
                    raw_arguments=('{"path":"should-not-exist.txt","content":"denied"}'),
                )
            ],
            principal=scheduled_principal,
            step=Step(run_id=run.id, step_number=1, started_at=composition.clock.now()),
            agent=agent,
            token=RunCancellationToken(composition.clock, run.deadline_at),
        )
        assert result[0].is_error is True
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, _principal())
        assert events[-1].event_type == "tool.call.denied"
        assert events[-1].payload["reason_code"] == "policy.scope.missing"


async def test_scheduled_instruction_is_context_only_on_public_chat_surfaces() -> None:
    public_principal = _principal(scopes={"memory.read", "run.read", "session.read"})
    async with _materialize(public_principal) as (composition, occurrence):
        assert occurrence is not None
        assert occurrence.session_id is not None
        assert occurrence.run_id is not None
        answer = AssistantMessage(content=[TextPart(text="The briefing result.")])
        async with composition.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=occurrence.session_id,
                    run_id=occurrence.run_id,
                    event_type="assistant.message.completed",
                    actor_type="runtime",
                    payload={"message": answer.model_dump(mode="json")},
                )
            )
            running = await uow.runs.transition(
                occurrence.run_id,
                RunStatus.QUEUED,
                RunStatus.RUNNING,
            )
            await uow.runs.transition(
                running.id,
                RunStatus.RUNNING,
                RunStatus.COMPLETED,
                final_message="The briefing result.",
            )

        transcript = await composition.services.sessions.messages(
            public_principal,
            occurrence.session_id,
            limit=100,
            cursor=None,
        )
        assert [
            (message.role, [block.model_dump(mode="json") for block in message.content])
            for message in transcript.items
        ] == [("assistant", [{"type": "text", "text": "The briefing result."}])]

        frames = [
            frame
            async for frame in composition.services.runs.stream(
                public_principal, occurrence.run_id, None
            )
        ]
        assert "user.message.created" not in [frame.event for frame in frames]
        assert "assistant.message.completed" in [frame.event for frame in frames]


async def test_scheduled_run_honors_an_explicit_final_synthesis_reserve() -> None:
    limits = RunLimits(
        max_steps=2,
        max_model_calls=4,
        max_tool_calls=2,
        max_cost=Decimal("2"),
        synthesis_reserve_steps=1,
        synthesis_reserve_model_calls=1,
        synthesis_reserve_cost=Decimal("0.25"),
    )
    agent = _agent().model_copy(
        update={
            "enabled_tools": ["math.calculate"],
            "limits": limits,
            "model_policy": "fake-balanced",
        },
        deep=True,
    )
    revision = _revision().model_copy(
        update={
            "requested_scopes": frozenset(),
            "limits": limits,
        },
        deep=True,
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "2 + 2"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                text="The bounded result is 4.",
                context_contains="Runtime control: the final-synthesis reserve is active",
            ),
        ]
    )
    principal = _principal(scopes=set())

    async with build(
        settings=memory_settings(),
        storage="memory",
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(agent)
            await uow.policy_profiles.record(_profile())
            await uow.schedules.create(_schedule(), revision)
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        occurrence = await materializer.materialize(SCHEDULE_ID)
        assert occurrence is not None and occurrence.run_id is not None

        await composition.executor.execute(occurrence.run_id)
        completed = await composition.runs.get(occurrence.run_id)
        model_provider = composition.executor._model_provider
        assert isinstance(model_provider, FakeModelProvider)
        requests = [request.model_copy(deep=True) for request in model_provider.requests]

    assert completed.status is RunStatus.COMPLETED, completed.failure
    assert completed.final_message == "The bounded result is 4."
    assert len(requests) == 2
    controls = [
        part.text
        for item in requests[1].conversation
        if isinstance(item, UserMessage) and item.trust is TrustLevel.PLATFORM
        for part in item.content
        if isinstance(part, TextPart)
    ]
    assert any("the final-synthesis reserve is active" in text for text in controls)
    assert any("because the run steps budget is exhausted" in text for text in controls)
    assert all("research steps budget" not in text for text in controls)


async def test_firing_revalidates_current_authority() -> None:
    async with _materialize(_principal(scopes=set())) as (_composition, occurrence):
        assert occurrence is not None
        assert occurrence.disposition is OccurrenceDisposition.AUTHORIZATION_FAILED
        assert occurrence.reason_code == "schedule.scope_revoked"
        assert occurrence.session_id is None
        assert occurrence.run_id is None
    async with _materialize(_principal(), enabled=False) as (_composition, occurrence):
        assert occurrence is not None
        assert occurrence.disposition is OccurrenceDisposition.AUTHORIZATION_FAILED
        assert occurrence.reason_code == "schedule.principal_disabled"
        assert occurrence.session_id is None
        assert occurrence.run_id is None


async def test_occurrences_from_one_schedule_never_overlap() -> None:
    principal = _principal()
    other_schedule_id = UUID(int=SCHEDULE_ID.int + 1)
    cadence = DailyCadence(local_time=time(16), timezone="UTC")
    recurring_revision = _revision().model_copy(update={"cadence": cadence, "timezone": "UTC"})
    other_revision = recurring_revision.model_copy(update={"schedule_id": other_schedule_id})
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.policy_profiles.record(_profile())
            await uow.schedules.create(_schedule(), recurring_revision)
            await uow.schedules.create(
                _schedule().model_copy(
                    update={"id": other_schedule_id, "next_fire_at": NOW + timedelta(days=1)}
                ),
                other_revision,
            )
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        first = await materializer.materialize(SCHEDULE_ID)
        assert first is not None and first.run_id is not None
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(timedelta(days=1))

        skipped = await materializer.materialize(SCHEDULE_ID)
        other = await materializer.materialize(other_schedule_id)

        assert skipped is not None
        assert skipped.disposition is OccurrenceDisposition.SKIPPED_OVERLAP
        assert skipped.reason_code == "schedule.in_flight"
        assert skipped.run_id is None
        assert other is not None
        assert other.disposition is OccurrenceDisposition.MATERIALIZED
        async with composition.uow_factory() as uow:
            schedule = await uow.schedules.get(SCHEDULE_ID, principal)
            assert schedule.consecutive_failures == 0
            occurrences = await uow.schedule_occurrences.list(SCHEDULE_ID, principal, limit=10)
            assert [item.disposition for item in occurrences] == [
                OccurrenceDisposition.SKIPPED_OVERLAP,
                OccurrenceDisposition.MATERIALIZED,
            ]


async def test_downtime_coalesces_thousands_of_misfires_without_backlog_iteration() -> None:
    principal = _principal()
    oldest = NOW - timedelta(days=10_000)
    cadence = DailyCadence(local_time=time(16), timezone="UTC")
    recurring_revision = _revision().model_copy(update={"cadence": cadence, "timezone": "UTC"})
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.schedules.create(
                _schedule().model_copy(update={"next_fire_at": oldest}), recurring_revision
            )
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )

        occurrence = await materializer.materialize(SCHEDULE_ID)

        assert occurrence is not None
        assert occurrence.nominal_fire_at == NOW
        async with composition.uow_factory() as uow:
            coalesced = await uow.process_events.list("schedule.misfires_coalesced")
            assert len(coalesced) == 1
            assert coalesced[0].payload == {
                "schedule_id": str(SCHEDULE_ID),
                "schedule_revision": 1,
                "tenant_id": "local",
                "principal_id": "local-user",
                "actor": "scheduler",
                "event_time": NOW.isoformat(),
                "first_nominal_at": oldest.isoformat(),
                "last_nominal_at": NOW.isoformat(),
                "count": 10_001,
            }


async def test_future_occurrence_never_fires_early() -> None:
    principal = _principal()
    due_at = NOW + timedelta(microseconds=1)
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.schedules.create(
                _schedule().model_copy(update={"next_fire_at": due_at}),
                _revision().model_copy(update={"cadence": OnceCadence(at=due_at)}),
            )
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )

        assert await materializer.materialize(SCHEDULE_ID) is None
        async with composition.uow_factory() as uow:
            assert await uow.schedule_occurrences.list(SCHEDULE_ID, principal, limit=10) == []


class _FixedAdmission:
    def __init__(self, decision: ScheduleAdmissionDecision) -> None:
        self._decision = decision

    async def check(
        self, tenant_id: str, revision: ScheduleRevision, now: datetime
    ) -> ScheduleAdmissionDecision:
        return self._decision


async def test_admission_denials_create_no_run_and_obey_retry_semantics() -> None:
    retry = _FixedAdmission(
        ScheduleAdmissionDecision(
            outcome=ScheduleAdmissionOutcome.RETRY,
            reason_code="schedule.concurrency_limit",
        )
    )
    async with _materialize(_principal(), admission=retry) as (composition, occurrence):
        assert occurrence is None
        async with composition.uow_factory() as uow:
            schedule = await uow.schedules.get(SCHEDULE_ID, _principal())
            assert schedule.next_fire_at == NOW
            assert schedule.state is ScheduleState.ACTIVE
            assert await uow.schedule_occurrences.list(SCHEDULE_ID, _principal(), limit=10) == []

    reject = _FixedAdmission(
        ScheduleAdmissionDecision(
            outcome=ScheduleAdmissionOutcome.REJECT,
            reason_code="schedule.rate_limit",
        )
    )
    async with _materialize(_principal(), admission=reject) as (composition, occurrence):
        assert occurrence is not None
        assert occurrence.disposition is OccurrenceDisposition.MISSED
        assert occurrence.reason_code == "schedule.rate_limit"
        assert occurrence.session_id is None
        assert occurrence.run_id is None
        async with composition.uow_factory() as uow:
            schedule = await uow.schedules.get(SCHEDULE_ID, _principal())
            assert schedule.state is ScheduleState.COMPLETED
