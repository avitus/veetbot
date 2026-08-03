from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text, update

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import (
    SchemaRevisionError,
    assert_schema_revision,
    create_engine,
    create_session_factory,
)
from agent_core.adapters.persistence.sqlalchemy_models import ProjectionWatermarkRow
from agent_core.bootstrap import build
from agent_core.domain.errors import ConflictError, WorkerFencedError
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import Run, RunCheckpoint, RunStatus, Step
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
                (5, False),
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
                        status=RunStatus.RUNNING,
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
            assert await uow.checkpoints.prune(run_id, terminal=True) == 2
            assert await uow.checkpoints.latest(run_id) == expected


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


async def test_nonterminal_checkpoints_are_dispensible() -> None:
    assert await _crash_after_checkpoint(delete_checkpoint=True) is RunStatus.COMPLETED


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
