from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, text, update

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import (
    SchemaRevisionError,
    assert_schema_revision,
    create_engine,
    create_session_factory,
)
from agent_core.adapters.persistence.repositories import PostgresCheckpointRepository
from agent_core.adapters.persistence.sqlalchemy_models import (
    CheckpointRow,
    ProjectionWatermarkRow,
    RunRow,
    ToolInvocationRow,
)
from agent_core.bootstrap import build
from agent_core.context.history import validate_tool_pairs
from agent_core.domain.errors import ConflictError, WorkerFencedError
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import Run, RunCheckpoint, RunLimits, RunStatus, Step
from agent_core.domain.views import TextContentBlock
from agent_core.runtime import loop as runtime_loop
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from tests.contract.support import NOW
from tests.integration.m2_support import PRINCIPAL, database_settings


async def test_sequence_integrity_with_concurrent_rollbacks_and_projection_observation() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        session_ids = [
            await composition.sessions.create(),
            await composition.sessions.create(),
        ]

        async def append(index: int) -> None:
            session_id = session_ids[index % len(session_ids)]
            try:
                async with composition.uow_factory() as uow:
                    await uow.events.append(
                        NewEvent(
                            session_id=session_id,
                            run_id=None,
                            event_type="user.message.created",
                            actor_type="test",
                            payload={"content": f"message-{index}"},
                        )
                    )
                    if index % 7 == 0:
                        raise RuntimeError("injected rollback")
            except RuntimeError as exc:
                assert str(exc) == "injected rollback"

        await asyncio.gather(*(append(index) for index in range(40)))
        for session_id in session_ids:
            async with composition.uow_factory() as uow:
                events = await uow.events.list_after(session_id, 0, PRINCIPAL)
                history = await uow.history.catch_up(session_id)
            sequences = [event.sequence for event in events]
            assert len(sequences) == len(set(sequences))
            assert sequences == sorted(sequences)
            assert history.through_sequence == max(sequences)
            assert len(history.items) == len(events) - 1  # session.created is not conversation


async def test_projection_rebuild_matches_incremental_state() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        for content in ("one", "two", "three"):
            async with composition.uow_factory() as uow:
                await uow.events.append(
                    NewEvent(
                        session_id=session_id,
                        run_id=None,
                        event_type="user.message.created",
                        actor_type="test",
                        payload={"content": content},
                    )
                )
                incremental = await uow.history.catch_up(session_id)
        async with composition.uow_factory() as uow:
            rebuilt = await uow.history.rebuild(session_id)
        assert rebuilt.items == incremental.items
        assert rebuilt.through_sequence == incremental.through_sequence
        assert rebuilt.builder_version == incremental.builder_version
        run_id = await composition.runs.submit("project one completed trajectory")
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="trajectory-worker",
        )
        assert await worker.run_once()
        async with composition.uow_factory() as uow:
            incremental_trajectory = await uow.trajectory.catch_up(run_id)
            completed = await uow.runs.get(run_id, PRINCIPAL)
            completed_history = await uow.history.catch_up(completed.session_id)
        async with composition.uow_factory() as uow:
            rebuilt_trajectory = await uow.trajectory.rebuild(run_id)
        assert incremental_trajectory is not None
        assert incremental_trajectory == rebuilt_trajectory
        assert incremental_trajectory.terminal
        assert [item.kind for item in completed_history.items] == [
            "user",
            "tool_call",
            "tool_result",
            "assistant",
        ]


async def test_projection_rebuilds_when_builder_version_changes() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="test",
                    payload={"content": "versioned history"},
                )
            )
            expected = await uow.history.catch_up(session_id)
        engine = create_engine(database_settings().database_url)
        try:
            async with create_session_factory(engine)() as session:
                await session.execute(
                    update(ProjectionWatermarkRow)
                    .where(
                        ProjectionWatermarkRow.projection_name == "session_history",
                        ProjectionWatermarkRow.scope == str(session_id),
                    )
                    .values(builder_version="session-history@obsolete")
                )
                await session.commit()
        finally:
            await engine.dispose()
        async with composition.uow_factory() as uow:
            rebuilt = await uow.history.catch_up(session_id)
        assert rebuilt == expected


async def test_two_workers_race_but_only_one_claim_executes_and_releases() -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="done")])
    async with build(
        settings=database_settings(), storage="postgres", script=script
    ) as composition:
        run_id = await composition.runs.submit("race the workers")
        async with composition.uow_factory() as uow:
            initial_checkpoint = await uow.checkpoints.latest(run_id)
        assert initial_checkpoint is not None
        assert initial_checkpoint.version == 1
        first = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="worker-a",
        )
        second = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="worker-b",
        )
        results = await asyncio.gather(first.run_once(), second.run_once())
        run = await composition.runs.get(run_id)
        events = await composition.runs.events(run_id)
    assert sorted(results) == [False, True]
    assert run.status is RunStatus.COMPLETED
    assert run.attempts == 1
    assert run.lease_owner is None
    assert [event.event_type for event in events].count("run.claimed") == 1
    assert [event.event_type for event in events].count("run.completed") == 1


async def test_concurrent_idempotent_submissions_return_one_committed_run() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        first, second = await asyncio.gather(
            composition.runs.submit("submit once", idempotency_key="m2-concurrent-request"),
            composition.runs.submit("submit once", idempotency_key="m2-concurrent-request"),
        )
        events = await composition.runs.events(first)
    assert first == second
    assert [event.event_type for event in events].count("run.queued") == 1


async def test_concurrent_idempotent_submissions_share_one_existing_session() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        first, second = await asyncio.gather(
            composition.runs.submit(
                "submit once in session",
                session_id,
                idempotency_key="m2-concurrent-session-request",
            ),
            composition.runs.submit(
                "submit once in session",
                session_id,
                idempotency_key="m2-concurrent-session-request",
            ),
        )
        events = await composition.runs.events(first)
    assert first == second
    assert [event.event_type for event in events].count("run.queued") == 1


async def test_expired_idempotency_key_creates_a_new_run() -> None:
    clock = FixedClock(NOW)
    async with build(settings=database_settings(), storage="postgres", clock=clock) as composition:
        first = await composition.runs.submit(
            "submit after expiry", idempotency_key="m2-expired-request"
        )
        clock.advance(timedelta(hours=25))
        second = await composition.runs.submit(
            "submit after expiry", idempotency_key="m2-expired-request"
        )
    assert second != first


async def test_distinct_agent_specs_receive_distinct_stable_versions() -> None:
    async with build(settings=database_settings(), storage="postgres") as first_composition:
        first_session_id = await first_composition.sessions.create()
        async with first_composition.uow_factory() as uow:
            first_session = await uow.sessions.get(first_session_id, PRINCIPAL)

    custom_limits = RunLimits(max_steps=31, max_model_calls=16, max_tool_calls=32)
    async with build(
        settings=database_settings(), storage="postgres", limits=custom_limits
    ) as second_composition:
        second_session_id = await second_composition.sessions.create()
        async with second_composition.uow_factory() as uow:
            second_session = await uow.sessions.get(second_session_id, PRINCIPAL)

    async with build(
        settings=database_settings(), storage="postgres", limits=custom_limits
    ) as repeated_composition:
        repeated_session_id = await repeated_composition.sessions.create()
        async with repeated_composition.uow_factory() as uow:
            repeated_session = await uow.sessions.get(repeated_session_id, PRINCIPAL)

    assert first_session.agent_version != second_session.agent_version
    assert second_session.agent_version == repeated_session.agent_version


async def test_active_run_and_idempotency_hash_conflicts_are_typed() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        await composition.runs.submit(
            "first request", session_id, idempotency_key="m2-request-hash"
        )
        with pytest.raises(ConflictError, match="different request"):
            await composition.runs.submit(
                "changed request", session_id, idempotency_key="m2-request-hash"
            )
        with pytest.raises(ConflictError, match="non-terminal run"):
            await composition.runs.submit("second active run", session_id)


async def test_terminal_prune_preserves_the_latest_delta_chain() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        run_id = await composition.runs.submit("preserve checkpoint deltas")
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="checkpoint-worker",
        )
        claimed = await worker.claim()
        assert claimed is not None
        async with composition.uow_factory() as uow:
            assert await uow.checkpoints.delete_nonterminal(run_id) == 1
            history = await uow.history.catch_up(claimed.run.session_id)
            for version, full in (
                (1, True),
                (2, False),
                (3, True),
                (4, False),
                (5, True),
            ):
                event = await uow.events.append(
                    NewEvent(
                        session_id=claimed.run.session_id,
                        run_id=run_id,
                        event_type="run.checkpointed",
                        actor_type="test",
                        payload={"version": version, "full": full},
                    ),
                    lease=claimed.lease,
                )
                await uow.checkpoints.write(
                    run_id,
                    RunCheckpoint(
                        run_id=run_id,
                        version=version,
                        status=(RunStatus.COMPLETED if version == 5 else RunStatus.RUNNING),
                        conversation=[item.model_copy(deep=True) for item in history.items],
                        working_state={"checkpoint": version},
                        last_event_sequence=event.sequence,
                        created_at=composition.clock.now(),
                    ),
                    full=full,
                    lease=claimed.lease,
                )
            expected = await uow.checkpoints.latest(run_id)
            assert expected is not None
            assert await uow.checkpoints.prune(run_id, terminal=True) == 4
            assert await uow.checkpoints.latest(run_id) == expected


async def test_nonterminal_prune_keeps_a_checkpoint_committed_after_it_read_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Production run b642e4d7 (2026-10-07): maintenance pruned while the worker
    # committed its next delta, the delta vanished, and the run's following
    # write failed with "checkpoint version 14 does not follow 12".
    async with build(settings=database_settings(), storage="postgres") as composition:
        run_id = await composition.runs.submit("keep a concurrent checkpoint")
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="checkpoint-worker",
        )
        claimed = await worker.claim()
        assert claimed is not None

        async def write(version: int, *, full: bool) -> None:
            async with composition.uow_factory() as uow:
                event = await uow.events.append(
                    NewEvent(
                        session_id=claimed.run.session_id,
                        run_id=run_id,
                        event_type="run.checkpointed",
                        actor_type="test",
                        payload={"version": version, "full": full},
                    ),
                    lease=claimed.lease,
                )
                await uow.checkpoints.write(
                    run_id,
                    RunCheckpoint(
                        run_id=run_id,
                        version=version,
                        status=RunStatus.RUNNING,
                        working_state={"checkpoint": version},
                        last_event_sequence=event.sequence,
                        created_at=composition.clock.now(),
                    ),
                    full=full,
                    lease=claimed.lease,
                )

        async with composition.uow_factory() as uow:
            assert await uow.checkpoints.delete_nonterminal(run_id) == 1
        await write(1, full=True)
        await write(2, full=False)

        async with composition.uow_factory() as uow:
            checkpoints = cast(PostgresCheckpointRepository, uow.checkpoints)
            read_chain = checkpoints._rows
            raced = False

            async def read_then_commit_the_next_delta(target: UUID) -> list[CheckpointRow]:
                nonlocal raced
                rows = await read_chain(target)
                if not raced:
                    raced = True
                    await write(3, full=False)
                return rows

            monkeypatch.setattr(checkpoints, "_rows", read_then_commit_the_next_delta)
            assert await checkpoints.prune(run_id, terminal=False) == 0
        assert raced

        async with composition.uow_factory() as uow:
            latest = await uow.checkpoints.latest(run_id)
        assert latest is not None
        assert latest.version == 3
        await write(4, full=False)


class _InjectedWorkerCrash(BaseException):
    pass


async def test_tool_usage_is_not_double_counted_after_post_commit_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(NOW)
    original = UnitOfWorkBudgetLedger.record_tool_usage
    crashed = False

    async def crash_after_commit(
        ledger: UnitOfWorkBudgetLedger,
        run: Run,
        count: int,
        *,
        step: Step,
    ) -> None:
        nonlocal crashed
        await original(ledger, run, count, step=step)
        if not crashed:
            crashed = True
            raise _InjectedWorkerCrash

    async with build(settings=database_settings(), storage="postgres", clock=clock) as composition:
        run_id = await composition.runs.submit("calculate 17 times 23")
        monkeypatch.setattr(UnitOfWorkBudgetLedger, "record_tool_usage", crash_after_commit)
        failed_worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="post-usage-crash",
        )
        with pytest.raises(_InjectedWorkerCrash):
            await failed_worker.run_once()
        interrupted = await composition.runs.get(run_id)
        assert interrupted.tool_call_count == 1
        monkeypatch.setattr(UnitOfWorkBudgetLedger, "record_tool_usage", original)
        clock.advance(timedelta(seconds=31))
        maintenance = MaintenanceWorker(uow_factory=composition.uow_factory, clock=clock)
        assert await maintenance.run_once() == 1
        clock.advance(timedelta(seconds=2))
        recovery_worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="post-usage-recovery",
        )
        assert await recovery_worker.run_once()
        recovered = await composition.runs.get(run_id)
    assert recovered.status is RunStatus.COMPLETED
    assert recovered.tool_call_count == 1
    assert recovered.usage.tool_calls == 1


async def _crash_after_checkpoint(*, delete_checkpoint: bool) -> RunStatus:
    clock = FixedClock(NOW)
    script = FakeModelScript(turns=[ScriptedTurn(text="recovered")])
    async with build(
        settings=database_settings(),
        storage="postgres",
        script=script,
        clock=clock,
    ) as composition:
        run_id = await composition.runs.submit("survive a worker crash")
        crashed = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="crashed-worker",
        )
        claimed = await crashed.claim()
        assert claimed is not None
        async with composition.uow_factory() as uow:
            if delete_checkpoint:
                assert await uow.checkpoints.delete_nonterminal(run_id) == 1
            else:
                assert await uow.checkpoints.latest(run_id) is not None
        clock.advance(timedelta(seconds=31))
        maintenance = MaintenanceWorker(
            uow_factory=composition.uow_factory,
            clock=clock,
            poll_interval_seconds=0,
        )
        assert await maintenance.run_once() == 1
        clock.advance(timedelta(seconds=2))
        recovered = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="recovery-worker",
        )
        assert await recovered.run_once()
        return (await composition.runs.get(run_id)).status


async def test_worker_crash_after_checkpoint_resumes_to_terminal_state() -> None:
    assert await _crash_after_checkpoint(delete_checkpoint=False) is RunStatus.COMPLETED


async def test_artifact_orphan_reconciliation_uses_a_coarse_independent_cadence() -> None:
    clock = FixedClock(NOW)
    expiry_calls = 0
    orphan_calls = 0

    async def sweep_expired() -> int:
        nonlocal expiry_calls
        expiry_calls += 1
        return 0

    async def reconcile_orphans() -> int:
        nonlocal orphan_calls
        orphan_calls += 1
        return 0

    async with build(settings=database_settings(), storage="postgres", clock=clock) as composition:
        maintenance = MaintenanceWorker(
            uow_factory=composition.uow_factory,
            clock=clock,
            sweep_artifacts=sweep_expired,
            sweep_artifact_orphans=reconcile_orphans,
            artifact_orphan_interval_seconds=60,
        )
        await maintenance.run_once()
        await maintenance.run_once()
        assert (expiry_calls, orphan_calls) == (2, 1)
        clock.advance(timedelta(seconds=61))
        await maintenance.run_once()

    assert (expiry_calls, orphan_calls) == (3, 2)


async def test_nonterminal_checkpoints_are_dispensible() -> None:
    assert await _crash_after_checkpoint(delete_checkpoint=True) is RunStatus.COMPLETED


async def test_terminal_delta_checkpoint_preserves_chain_and_allows_memory_sweep() -> None:
    clock = FixedClock(NOW)
    settings = database_settings()
    engine = create_engine(settings.database_url)
    memory_sweeps = 0

    async def consolidate() -> int:
        nonlocal memory_sweeps
        memory_sweeps += 1
        return 0

    try:
        async with build(settings=settings, storage="postgres", clock=clock) as composition:
            failed_id = await composition.runs.submit("a failed run")
            healthy_id = await composition.runs.submit("a completed run")
            async with composition.uow_factory() as uow:
                for run_id, count in ((failed_id, 6), (healthy_id, 2)):
                    initial = await uow.checkpoints.latest(run_id)
                    assert initial is not None and initial.version == 1
                    for version in range(2, count + 1):
                        terminal = run_id == healthy_id and version == count
                        await uow.checkpoints.write(
                            run_id,
                            RunCheckpoint(
                                run_id=run_id,
                                version=version,
                                status=RunStatus.COMPLETED if terminal else RunStatus.RUNNING,
                                created_at=NOW,
                            ),
                            full=terminal,
                        )
            # Reproduce persisted terminal state without a final full checkpoint.
            async with engine.begin() as connection:
                for run_id, status in (
                    (failed_id, RunStatus.FAILED),
                    (healthy_id, RunStatus.COMPLETED),
                ):
                    await connection.execute(
                        update(RunRow).where(RunRow.id == run_id).values(status=status.value)
                    )

            worker = MaintenanceWorker(
                uow_factory=composition.uow_factory,
                clock=clock,
                sweep_memory_consolidation=consolidate,
            )
            await worker.run_once()

            assert memory_sweeps == 1
            async with engine.connect() as connection:
                counts = dict(
                    (
                        await connection.execute(
                            select(CheckpointRow.run_id, func.count()).group_by(
                                CheckpointRow.run_id
                            )
                        )
                    )
                    .tuples()
                    .all()
                )
            assert counts == {failed_id: 6, healthy_id: 1}
            async with composition.uow_factory() as uow:
                with pytest.raises(ConflictError, match="final full snapshot"):
                    await uow.checkpoints.prune(failed_id, terminal=True)
                assert (await uow.checkpoints.latest(failed_id)) is not None
    finally:
        await engine.dispose()


async def test_stale_fenced_worker_cannot_affect_rows() -> None:
    clock = FixedClock(NOW)
    async with build(settings=database_settings(), storage="postgres", clock=clock) as composition:
        run_id = await composition.runs.submit("fence the old worker")
        old_worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="old-worker",
        )
        old_claim = await old_worker.claim()
        assert old_claim is not None
        clock.advance(timedelta(seconds=31))
        maintenance = MaintenanceWorker(uow_factory=composition.uow_factory, clock=clock)
        assert await maintenance.run_once() == 1
        clock.advance(timedelta(seconds=2))
        new_worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="new-worker",
        )
        new_claim = await new_worker.claim()
        assert new_claim is not None
        before = len(await composition.runs.events(run_id))
        with pytest.raises(WorkerFencedError, match="fenced"):
            async with composition.uow_factory() as uow:
                await uow.events.append(
                    NewEvent(
                        session_id=old_claim.run.session_id,
                        run_id=run_id,
                        event_type="stale.write",
                        actor_type="test",
                    ),
                    lease=old_claim.lease,
                )
        assert len(await composition.runs.events(run_id)) == before


async def test_wrong_pinned_revision_is_refused_without_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_core.adapters.persistence.database as database

    engine = database.create_engine(database_settings().database_url)
    monkeypatch.setattr(database, "EXPECTED_REVISION", "wrong-revision")
    try:
        with pytest.raises(SchemaRevisionError, match="does not match expected wrong-revision"):
            await database.assert_schema_revision(engine)
    finally:
        await engine.dispose()


async def test_multiple_database_revisions_are_refused_cleanly() -> None:
    engine = create_engine(database_settings().database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('unexpected_branch')")
            )
        with pytest.raises(SchemaRevisionError, match="database revisions"):
            await assert_schema_revision(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM alembic_version WHERE version_num = 'unexpected_branch'")
            )
        await engine.dispose()


async def test_resume_recovers_parallel_tool_results_after_a_lost_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease lost between the effects and their checkpoint must still resume.

    Production run d139f688 (2026-09-22) died here: a step dispatched two tool
    calls, both succeeded, the lease expired before the `tool_call` checkpoint,
    and the resumed attempt raised while assembling context instead of
    recovering. Runtime gate 10 requires the resume to re-enter the pipeline.
    """

    clock = FixedClock(NOW)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                reasoning="two lookups before answering",
                provider_reasoning_payload={"id": "rs_parallel", "summary": []},
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "17*23"},
                        call_id="parallel-first",
                    ),
                    ScriptedToolCall(
                        name="system.current_time",
                        arguments={"timezone": "UTC"},
                        call_id="parallel-second",
                    ),
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="recovered"),
        ]
    )
    original_checkpoint = runtime_loop.checkpoint
    crashed = False

    async def crash_before_tool_checkpoint(context: Any, trigger: str) -> None:
        nonlocal crashed
        if trigger == "tool_call" and not crashed:
            crashed = True
            raise _InjectedWorkerCrash
        await original_checkpoint(context, trigger)

    async with build(
        settings=database_settings(),
        storage="postgres",
        script=script,
        clock=clock,
    ) as composition:
        run_id = await composition.runs.submit("use two tools and then answer")
        monkeypatch.setattr(runtime_loop, "checkpoint", crash_before_tool_checkpoint)
        interrupted = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="lost-lease-worker",
        )
        with pytest.raises(_InjectedWorkerCrash):
            await interrupted.run_once()
        monkeypatch.setattr(runtime_loop, "checkpoint", original_checkpoint)
        clock.advance(timedelta(seconds=31))
        maintenance = MaintenanceWorker(
            uow_factory=composition.uow_factory,
            clock=clock,
            poll_interval_seconds=0,
        )
        assert await maintenance.run_once() == 1
        clock.advance(timedelta(seconds=2))
        recovery = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="lost-lease-recovery",
        )
        assert await recovery.run_once()
        recovered = await composition.runs.get(run_id)

    assert recovered.failure is None
    assert recovered.status is RunStatus.COMPLETED
    assert recovered.tool_call_count == 2


def _two_tool_script(final_text: str = "recovered") -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                reasoning="look up the product first",
                provider_reasoning_payload={"id": "rs_first", "summary": []},
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "17*23"},
                        call_id="adopted-first",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                reasoning="then the time",
                provider_reasoning_payload={"id": "rs_second", "summary": []},
                tool_calls=[
                    ScriptedToolCall(
                        name="system.current_time",
                        arguments={"timezone": "UTC"},
                        call_id="adopted-second",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text=final_text),
        ]
    )


def _conversation_shape(items: list[Any]) -> list[tuple[str, str | None]]:
    return [(item.kind, getattr(item, "call_id", None)) for item in items]


async def _reclaim_and_resume(composition: Any, clock: FixedClock, worker_id: str) -> None:
    clock.advance(timedelta(seconds=31))
    maintenance = MaintenanceWorker(
        uow_factory=composition.uow_factory,
        clock=clock,
        poll_interval_seconds=0,
    )
    assert await maintenance.run_once() == 1
    clock.advance(timedelta(seconds=2))
    recovery = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=clock,
        worker_id=worker_id,
    )
    assert await recovery.run_once()


@pytest.mark.parametrize(
    ("interrupted_at", "lost"),
    [
        # The second step's model turn is committed; its tool_pending is not.
        ("second_tool_pending", 1),  # restore the first step's tool_call
        ("second_tool_pending", 2),  # restore its tool_pending (run b642e4d7)
        ("second_tool_pending", 3),  # restore its model_response
        ("second_tool_pending", 4),  # restore the submission seed
        ("second_tool_pending", 5),  # no checkpoint survives
        # The first step's results are committed and the second step has begun.
        ("second_model_call", 1),  # restore the first step's tool_pending
    ],
)
async def test_lost_checkpoints_cost_time_not_information(
    monkeypatch: pytest.MonkeyPatch,
    interrupted_at: str,
    lost: int,
) -> None:
    """Resuming behind work the log already holds reaches the same terminal state.

    Production run b642e4d7 (2026-10-07) lost its newest checkpoint after the
    run had committed a tool result and the next model turn. The resumed
    attempt re-ran the tool under a new invocation, and the next resume read a
    session history with two results for one call and an unanswered call.
    """

    settings = database_settings()
    engine = create_engine(settings.database_url)
    try:
        clock = FixedClock(NOW)
        async with build(
            settings=settings,
            storage="postgres",
            script=_two_tool_script(),
            clock=clock,
        ) as composition:
            control_id = await composition.runs.submit("use two tools and then answer")
            control = DurableWorker(
                uow_factory=composition.uow_factory,
                executor=composition.executor,
                clock=clock,
                worker_id="uninterrupted-worker",
            )
            assert await control.run_once()
            control_run = await composition.runs.get(control_id)
            async with composition.uow_factory() as uow:
                expected = _conversation_shape(
                    (await uow.history.catch_up(control_run.session_id)).items
                )

        clock = FixedClock(NOW)
        async with build(
            settings=settings,
            storage="postgres",
            script=_two_tool_script(),
            clock=clock,
        ) as composition:
            run_id = await composition.runs.submit("use two tools and then answer")
            original_checkpoint = runtime_loop.checkpoint
            original_invoke = runtime_loop._invoke_model
            seen: dict[str, int] = {"tool_pending": 0, "model": 0}

            async def interrupt_checkpoint(context: Any, trigger: str) -> None:
                if trigger == "tool_pending":
                    seen["tool_pending"] += 1
                    if interrupted_at == "second_tool_pending" and seen["tool_pending"] == 2:
                        raise _InjectedWorkerCrash
                await original_checkpoint(context, trigger)

            async def interrupt_model(context: Any, *args: Any, **kwargs: Any) -> Any:
                seen["model"] += 1
                if interrupted_at == "second_model_call" and seen["model"] == 2:
                    raise _InjectedWorkerCrash
                return await original_invoke(context, *args, **kwargs)

            monkeypatch.setattr(runtime_loop, "checkpoint", interrupt_checkpoint)
            monkeypatch.setattr(runtime_loop, "_invoke_model", interrupt_model)
            interrupted = DurableWorker(
                uow_factory=composition.uow_factory,
                executor=composition.executor,
                clock=clock,
                worker_id="interrupted-worker",
            )
            with pytest.raises(_InjectedWorkerCrash):
                await interrupted.run_once()
            monkeypatch.setattr(runtime_loop, "checkpoint", original_checkpoint)
            monkeypatch.setattr(runtime_loop, "_invoke_model", original_invoke)

            async with engine.begin() as connection:
                newest = await connection.scalar(
                    select(func.max(CheckpointRow.version)).where(CheckpointRow.run_id == run_id)
                )
                assert newest is not None and newest >= lost
                await connection.execute(
                    delete(CheckpointRow).where(
                        CheckpointRow.run_id == run_id,
                        CheckpointRow.version > newest - lost,
                    )
                )

            await _reclaim_and_resume(composition, clock, "resuming-worker")
            recovered = await composition.runs.get(run_id)
            async with composition.uow_factory() as uow:
                history = (await uow.history.catch_up(recovered.session_id)).items
            async with engine.connect() as connection:
                executions = dict(
                    (
                        await connection.execute(
                            select(ToolInvocationRow.provider_call_id, func.count())
                            .where(ToolInvocationRow.run_id == run_id)
                            .group_by(ToolInvocationRow.provider_call_id)
                        )
                    )
                    .tuples()
                    .all()
                )
    finally:
        await engine.dispose()

    assert recovered.failure is None
    assert recovered.status is RunStatus.COMPLETED
    assert recovered.final_message == "recovered"
    # Each call ran once, under the invocation that first proposed it.
    assert executions == {"adopted-first": 1, "adopted-second": 1}
    assert recovered.tool_call_count == 2
    # The history the next resume, or the session's next run, reads is valid.
    validate_tool_pairs(history)
    assert _conversation_shape(history) == expected


async def test_a_question_adopted_after_lost_checkpoints_survives_the_next_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run b642e4d7 end to end: the resume after the owner's answer must still assemble.

    A lookup returned, the model then asked the owner a question, and the
    checkpoints recording both were lost. Production failed with a duplicate
    tool pair on the resume after this one; here that resume answers.
    """

    clock = FixedClock(NOW)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "17*23"},
                        call_id="lookup-before-question",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                text="One thing first.",
                tool_calls=[
                    ScriptedToolCall(
                        name="conversation.ask_user",
                        arguments={"question": "Should I round the answer?"},
                        call_id="question-after-lookup",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="recovered"),
        ]
    )
    settings = database_settings()
    engine = create_engine(settings.database_url)
    original_checkpoint = runtime_loop.checkpoint
    pending_checkpoints = 0

    async def crash_before_the_question_is_pending(context: Any, trigger: str) -> None:
        nonlocal pending_checkpoints
        if trigger == "tool_pending":
            pending_checkpoints += 1
            if pending_checkpoints == 2:
                raise _InjectedWorkerCrash
        await original_checkpoint(context, trigger)

    try:
        async with build(
            settings=settings,
            storage="postgres",
            script=script,
            clock=clock,
            enabled_tools=["math.calculate", "conversation.ask_user"],
        ) as composition:
            run_id = await composition.runs.submit("multiply, but check with me first")
            monkeypatch.setattr(runtime_loop, "checkpoint", crash_before_the_question_is_pending)
            interrupted = DurableWorker(
                uow_factory=composition.uow_factory,
                executor=composition.executor,
                clock=clock,
                worker_id="question-crash",
            )
            with pytest.raises(_InjectedWorkerCrash):
                await interrupted.run_once()
            monkeypatch.setattr(runtime_loop, "checkpoint", original_checkpoint)
            async with engine.begin() as connection:
                newest = await connection.scalar(
                    select(func.max(CheckpointRow.version)).where(CheckpointRow.run_id == run_id)
                )
                assert newest is not None
                # Restore the lookup's tool_pending checkpoint, as production did.
                await connection.execute(
                    delete(CheckpointRow).where(
                        CheckpointRow.run_id == run_id, CheckpointRow.version > newest - 2
                    )
                )

            await _reclaim_and_resume(composition, clock, "question-recovery")
            waiting = await composition.runs.get(run_id)
            assert waiting.status is RunStatus.WAITING_FOR_USER, waiting.failure
            question = next(
                event
                for event in reversed(await composition.runs.events(run_id))
                if event.event_type == "run.waiting_for_user"
            )
            await composition.services.runs.deliver_input(
                composition.principal,
                run_id,
                [TextContentBlock(text="No rounding.")],
                UUID(str(question.payload["question_id"])),
            )
            answered = DurableWorker(
                uow_factory=composition.uow_factory,
                executor=composition.executor,
                clock=clock,
                worker_id="answered-worker",
            )
            assert await answered.run_once()
            recovered = await composition.runs.get(run_id)
            async with composition.uow_factory() as uow:
                history = (await uow.history.catch_up(recovered.session_id)).items
            async with engine.connect() as connection:
                executions = dict(
                    (
                        await connection.execute(
                            select(ToolInvocationRow.provider_call_id, func.count())
                            .where(ToolInvocationRow.run_id == run_id)
                            .group_by(ToolInvocationRow.provider_call_id)
                        )
                    )
                    .tuples()
                    .all()
                )
    finally:
        await engine.dispose()

    assert recovered.failure is None
    assert recovered.status is RunStatus.COMPLETED
    assert recovered.final_message == "recovered"
    assert executions == {"lookup-before-question": 1, "question-after-lookup": 1}
    validate_tool_pairs(history)


async def test_a_committed_final_reply_completes_the_resumed_run() -> None:
    """A reply committed before finalization is the run's answer, not a reason to ask again."""

    clock = FixedClock(NOW)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "17*23"},
                        call_id="before-the-reply",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="answered once"),
        ]
    )
    original_complete_reply = runtime_loop._complete_reply

    async def crash_after_the_reply(context: Any, message: Any) -> Any:
        await original_complete_reply(context, message)
        raise _InjectedWorkerCrash

    async with build(
        settings=database_settings(),
        storage="postgres",
        script=script,
        clock=clock,
    ) as composition:
        run_id = await composition.runs.submit("answer after one tool")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runtime_loop, "_complete_reply", crash_after_the_reply)
            interrupted = DurableWorker(
                uow_factory=composition.uow_factory,
                executor=composition.executor,
                clock=clock,
                worker_id="reply-then-crash",
            )
            with pytest.raises(_InjectedWorkerCrash):
                await interrupted.run_once()
        await _reclaim_and_resume(composition, clock, "reply-recovery")
        recovered = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            replies = [
                event
                for event in await uow.events.list_after(
                    recovered.session_id, 0, PRINCIPAL, run_id=run_id
                )
                if event.event_type == "assistant.message.completed"
            ]

    assert recovered.failure is None
    assert recovered.status is RunStatus.COMPLETED
    assert recovered.final_message == "answered once"
    assert recovered.model_call_count == 2
    assert len(replies) == 1


async def test_resume_after_a_crash_before_the_tool_pending_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run interrupted between `model_response` and `tool_pending` must resume.

    That checkpoint holds a reasoning provider's continuation and a conversation
    whose trailing items are the model's own tool calls, with nothing yet in
    `pending_tool_calls`. Assembling the next request from it must not raise.
    """

    clock = FixedClock(NOW)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                reasoning="decide which tool to use",
                provider_reasoning_payload={"id": "rs_durable", "summary": []},
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "17*23"},
                        call_id="continuation-call",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="recovered"),
        ]
    )
    original_checkpoint = runtime_loop.checkpoint
    crashed = False

    async def crash_before_tool_pending(context: Any, trigger: str) -> None:
        nonlocal crashed
        if trigger == "tool_pending" and not crashed:
            crashed = True
            raise _InjectedWorkerCrash
        await original_checkpoint(context, trigger)

    async with build(
        settings=database_settings(),
        storage="postgres",
        script=script,
        clock=clock,
    ) as composition:
        run_id = await composition.runs.submit("use one tool and then answer")
        monkeypatch.setattr(runtime_loop, "checkpoint", crash_before_tool_pending)
        interrupted = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="pre-pending-crash",
        )
        with pytest.raises(_InjectedWorkerCrash):
            await interrupted.run_once()
        async with composition.uow_factory() as uow:
            persisted = await uow.checkpoints.latest(run_id)
        assert persisted is not None
        assert persisted.provider_continuation is not None
        assert persisted.pending_tool_calls == []
        monkeypatch.setattr(runtime_loop, "checkpoint", original_checkpoint)
        clock.advance(timedelta(seconds=31))
        maintenance = MaintenanceWorker(
            uow_factory=composition.uow_factory,
            clock=clock,
            poll_interval_seconds=0,
        )
        assert await maintenance.run_once() == 1
        clock.advance(timedelta(seconds=2))
        recovery = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="pre-pending-recovery",
        )
        assert await recovery.run_once()
        recovered = await composition.runs.get(run_id)

    assert recovered.failure is None
    assert recovered.status is RunStatus.COMPLETED
