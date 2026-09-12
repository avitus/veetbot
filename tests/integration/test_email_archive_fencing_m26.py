"""Real PostgreSQL lease replacement fences stale Gmail archive consent and dispatch."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.persistence import ClaimedRun, WorkerLease
from agent_core.domain.runs import Run, RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_email_archive_m26 import _archive_factory
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _seed_draft
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("stage", ["approval", "dispatch"])
async def test_archive_replaced_worker_cannot_approve_or_dispatch(stage: str) -> None:
    """Only the replacement lease may consume consent or reach the fake Gmail mutation."""
    writes: list[dict[str, Any]] = []
    clock = FixedClock(datetime(2026, 9, 12, 12, tzinfo=UTC))
    settings = replace(
        _email_settings(), database_url=database_settings().database_url, email_mode_enabled=True
    )
    async with build(
        settings=settings,
        storage="postgres",
        clock=clock,
        mcp_client_factory=await _archive_factory(writes),
    ) as app:
        thread, _ = await _seed_draft(app)
        operation = await app.services.email.archive(
            app.principal,
            thread.id,
            1,
            archived=True,
            idempotency_key=f"lease-{stage}",
        )
        first_worker = DurableWorker(
            uow_factory=app.uow_factory,
            executor=app.executor,
            clock=clock,
            worker_id="archive-old",
        )
        claimed = await first_worker.claim()
        assert claimed is not None and claimed.run.id == operation.run_id
        replacement: ClaimedRun | None = None

        async def replace_lease() -> None:
            """Expire and reclaim through the real queue before the old worker continues."""
            nonlocal replacement
            clock.advance(timedelta(seconds=31))
            async with app.uow_factory() as uow:
                assert uow.queue is not None
                assert await uow.queue.reclaim_expired(limit=1) == 1
            clock.advance(timedelta(seconds=2))
            next_worker = DurableWorker(
                uow_factory=app.uow_factory,
                executor=app.executor,
                clock=clock,
                worker_id="archive-replacement",
            )
            replacement = await next_worker.claim()
            assert replacement is not None
            assert replacement.run.id == claimed.run.id
            assert replacement.lease.lease_epoch > claimed.lease.lease_epoch

        approve = app.services.email.approve_archive
        pipeline = app.executor._dispatch_tools
        write_attempts = 0

        async def stale_approval(
            owner: Principal,
            run: Run,
            lease: WorkerLease | None,
            approval_id: UUID,
        ) -> None:
            """Force lease loss after the real pending approval exists, before it can resolve."""
            assert lease == claimed.lease
            if stage == "approval":
                await replace_lease()
            await approve(owner, run, lease, approval_id)

        async def stale_dispatch(**kwargs: Any) -> Any:
            """Replace ownership between one-time approval and the approved tool attempt."""
            nonlocal write_attempts
            if kwargs["tool_calls"][0].name.endswith("modify_labels"):
                write_attempts += 1
                if stage == "dispatch" and write_attempts == 2:
                    await replace_lease()
            return await pipeline(**kwargs)

        vars(app.services.email)["approve_archive"] = stale_approval
        app.executor._dispatch_tools = stale_dispatch
        await app.executor.execute_claimed(claimed)
        assert replacement is not None, "the test must replace a live lease at the target boundary"
        assert writes == [], "a stale worker reached the provider mutation"
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(operation.run_id, app.principal)
            [write] = [value for value in invocations if value.tool_name.endswith("modify_labels")]
            approval = await uow.approvals.get_by_action(write.id)
            assert approval is not None
            assert approval.status is (
                ApprovalStatus.PENDING if stage == "approval" else ApprovalStatus.APPROVED
            )
            assert write.effect_sent_at is None
        latest = await app.runs.get(operation.run_id)
        assert latest.status is RunStatus.RUNNING
        assert latest.lease_epoch == replacement.lease.lease_epoch

        vars(app.services.email)["approve_archive"] = approve
        app.executor._dispatch_tools = pipeline
        await app.executor.execute_claimed(replacement)
        assert len(writes) == 1
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        assert (await app.services.email.thread(app.principal, thread.id))["in_inbox"] is False
