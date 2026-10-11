"""Reviewed dreaming must not change recall before explicit owner approval."""

from typing import cast

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.dreaming import DreamingRun
from agent_core.domain.memory import Sensitivity
from agent_core.memory.reconsolidation_apply import apply_review
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.reconsolidation_apply_cases import prepared_batch
from tests.contract.reconsolidation_cases import Factory
from tests.contract.support import NOW, principal


async def review_mode_stages_merge_without_suppressing_originals(factory: Factory) -> None:
    prepared, review = await prepared_batch(factory)
    # The worker's reviewed path must persist pending proposals in the same UOW.
    result = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
        owner_review=True,
    )
    keys = result[0].operation_ids
    assert keys
    async with factory() as uow:
        view = await uow.reconsolidation.get_operation(
            principal(), keys[0], NOW, ceiling=Sensitivity.RESTRICTED
        )
        assert view is not None and view.state == "proposed"
        assert view.content is not None and len(view.sources) == 2
        members = tuple(s.belief_id for s in view.sources)
        assert not await uow.reconsolidation.active_merges(principal(), members, NOW)
        assert not await uow.reconsolidation.merges_at(
            principal(), members, as_of=NOW, known_at=NOW
        )


async def owner_approval_activates_only_that_proposal_and_replays_receipt(factory: Factory) -> None:
    from datetime import timedelta

    prepared, review = await prepared_batch(factory)
    result = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
        owner_review=True,
    )
    async with factory() as uow:
        key = result[0].operation_ids[0]
        before = await uow.reconsolidation.get_operation(
            principal(), key, NOW, ceiling=Sensitivity.RESTRICTED
        )
        assert before is not None
        at = NOW + timedelta(days=2)
        after = await uow.reconsolidation.decide_owner_review(
            principal(),
            key,
            before.revision,
            "approved",
            "approve-one",
            at,
        )
        assert after.state == "committed", "owner approval must activate the exact proposal"
        assert after.revision == before.revision + 1
        assert (
            await uow.reconsolidation.decide_owner_review(
                principal(),
                key,
                before.revision,
                "approved",
                "approve-one",
                at,
            )
            == after
        )
        members = tuple(s.belief_id for s in before.sources)
        assert [
            v.id for v in await uow.reconsolidation.active_merges(principal(), members, at)
        ] == [key]
        assert not await uow.reconsolidation.merges_at(
            principal(),
            members,
            as_of=NOW,
            known_at=at,
        ), "approval must not become active before its actual decision time"


async def daily_preview_claim_is_durable_and_has_no_catchup_burst(factory: Factory) -> None:
    from datetime import timedelta

    async with factory() as uow:
        first = await uow.reconsolidation.claim_dreaming_run(principal(), NOW)
        assert first is not None, "daily reviewed dreaming needs a durable due claim"
    async with factory() as uow:
        assert await uow.reconsolidation.claim_dreaming_run(principal(), NOW) is None
        assert (
            await uow.reconsolidation.claim_dreaming_run(principal(), NOW + timedelta(hours=23))
            is None
        )
        assert (
            await uow.reconsolidation.claim_dreaming_run(principal(), NOW + timedelta(days=5))
            is not None
        )
        assert (
            await uow.reconsolidation.claim_dreaming_run(principal(), NOW + timedelta(days=5))
            is None
        )


async def approved_summary_enters_delta_recall_after_pending_watermark(factory: Factory) -> None:
    prepared, review = await prepared_batch(factory, kind="summarize_related")
    result = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
        owner_review=True,
    )
    async with factory() as uow:
        key = result[0].operation_ids[0]
        operation = await uow.reconsolidation.summary_operation(principal(), key)
        assert operation is not None
        watermark = operation.store_position
        assert not await uow.reconsolidation.active_summaries(
            principal(),
            operation.plan.member_ids,
            NOW,
            ceiling=Sensitivity.RESTRICTED,
            current_scope="user",
        )
        await uow.reconsolidation.decide_owner_review(
            principal(), key, operation.revision, "approved", "summary", NOW
        )
        summaries = await uow.reconsolidation.active_summaries(
            principal(),
            operation.plan.member_ids,
            NOW,
            ceiling=Sensitivity.RESTRICTED,
            current_scope="user",
            min_store_position=watermark,
        )
        assert summaries, "approval must advance the summary's recall watermark"


async def reject_retry_conflict_stale_source_and_rollback(factory: Factory) -> None:
    import pytest

    from agent_core.domain.errors import ConflictError, NotFoundError

    prepared, review = await prepared_batch(factory)
    result = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
        owner_review=True,
    )
    key, other = result[0].operation_ids
    async with factory() as uow:
        before = await uow.reconsolidation.get_operation(
            principal(), key, NOW, ceiling=Sensitivity.RESTRICTED
        )
        assert before is not None
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.reconsolidation.decide_owner_review(
                principal(), key, before.revision, "approved", "rollback", NOW
            )
            raise RuntimeError("abort")
    async with factory() as uow:
        after = await uow.reconsolidation.get_operation(
            principal(), key, NOW, ceiling=Sensitivity.RESTRICTED
        )
        assert after == before
        rejected = await uow.reconsolidation.decide_owner_review(
            principal(), key, before.revision, "rejected", "reject", NOW
        )
        assert rejected.state == "rejected"
        assert (
            await uow.reconsolidation.decide_owner_review(
                principal(), key, before.revision, "rejected", "reject", NOW
            )
            == rejected
        )
        with pytest.raises(ConflictError):
            await uow.reconsolidation.decide_owner_review(
                principal(), key, before.revision, "approved", "approve", NOW
            )
        other_view = await uow.reconsolidation.get_operation(
            principal(), other, NOW, ceiling=Sensitivity.RESTRICTED
        )
        assert other_view is not None
        with pytest.raises(ConflictError):
            await uow.reconsolidation.decide_owner_review(
                principal(), other, other_view.revision + 1, "approved", "stale", NOW
            )
        foreign = principal().model_copy(update={"principal_id": "foreign"})
        with pytest.raises(NotFoundError):
            await uow.reconsolidation.decide_owner_review(
                foreign, other, other_view.revision, "approved", "foreign", NOW
            )
        await uow.memories.fence_for_erasure(principal(), [other_view.sources[0].belief_id])
    async with factory() as uow:
        with pytest.raises(ConflictError):
            await uow.reconsolidation.decide_owner_review(
                principal(), other, other_view.revision, "approved", "erased", NOW
            )
        erased = await uow.reconsolidation.get_operation(
            principal(), other, NOW, ceiling=Sensitivity.RESTRICTED
        )
        assert erased is not None and erased.content is None and not erased.sources


async def concurrent_daily_claim_pause_restart_and_history(factory: Factory) -> None:
    import asyncio
    from datetime import timedelta

    import pytest

    from agent_core.domain.errors import ConflictError

    async def claim() -> DreamingRun | None:
        async with factory() as uow:
            return await uow.reconsolidation.claim_dreaming_run(principal(), NOW)

    claims = await asyncio.gather(claim(), claim(), claim())
    assert sum(r is not None for r in claims) == 1
    run = next(r for r in claims if r is not None)
    async with factory() as uow:
        state = await uow.reconsolidation.dreaming_schedule(principal())
        paused = await uow.reconsolidation.pause_dreaming(principal(), True, state.revision)
        assert await uow.reconsolidation.pause_dreaming(principal(), True, state.revision) == paused
        with pytest.raises(ConflictError):
            await uow.reconsolidation.pause_dreaming(principal(), False, state.revision)
        assert (
            await uow.reconsolidation.claim_dreaming_run(principal(), NOW + timedelta(days=2))
            is None
        )
        complete = run.model_copy(
            update={"outcome": "failed", "reason": "execution_failed", "finished_at": NOW}
        )
        await uow.reconsolidation.finish_dreaming_run(principal(), complete)
    async with factory() as uow:
        assert (await uow.reconsolidation.dreaming_schedule(principal())).paused
        assert await uow.reconsolidation.dreaming_runs(principal(), NOW) == (complete,)
        assert not await uow.reconsolidation.dreaming_runs(
            principal().model_copy(update={"principal_id": "foreign"}), NOW
        )


SCENARIOS = [
    review_mode_stages_merge_without_suppressing_originals,
    owner_approval_activates_only_that_proposal_and_replays_receipt,
    daily_preview_claim_is_durable_and_has_no_catchup_burst,
    approved_summary_enters_delta_recall_after_pending_watermark,
    reject_retry_conflict_stale_source_and_rollback,
    concurrent_daily_claim_pause_restart_and_history,
]


async def expired_summary_cannot_be_approved(factory: Factory) -> None:
    from datetime import timedelta

    import pytest

    from agent_core.domain.errors import ConflictError
    from tests.contract.reconsolidation_summary_cases import summary_inputs

    async with factory() as uow:
        job, group, _, clauses = await summary_inputs(uow, expires=True)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
        value = await uow.reconsolidation.commit_summary(
            principal(), job.lease_token, group.id, prepared, NOW
        )
        await uow.reconsolidation.stage_owner_review(principal(), job.lease_token, value.id, NOW)
    async with factory() as uow:
        with pytest.raises(ConflictError):
            await uow.reconsolidation.decide_owner_review(
                principal(),
                value.id,
                value.revision + 1,
                "approved",
                "expired",
                NOW + timedelta(minutes=1),
            )
        assert not await uow.reconsolidation.get_summary(
            principal(),
            value.id,
            NOW + timedelta(minutes=1),
            ceiling=Sensitivity.RESTRICTED,
            current_scope="user",
        )


SCENARIOS.append(expired_summary_cannot_be_approved)


async def pending_merge_does_not_reintroduce_originals_into_session_delta(factory: Factory) -> None:
    from tests.contract.memory_fixtures import recall_query

    prepared, review = await prepared_batch(factory)
    async with factory() as uow:
        watermark = await uow.memories.head_position(principal())
    result = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
        owner_review=True,
    )
    query = recall_query().model_copy(
        update={
            "min_store_position": watermark,
            "include_merge_changes": True,
            "owner_reviewed_merges_only": True,
        }
    )
    async with factory() as uow:
        assert not await uow.memories.query(query), (
            "pending proposals must not replay originals into an existing session"
        )
        key = result[0].operation_ids[0]
        view = await uow.reconsolidation.get_operation(
            principal(), key, NOW, ceiling=Sensitivity.RESTRICTED
        )
        assert view is not None
        await uow.reconsolidation.decide_owner_review(
            principal(), key, view.revision, "approved", "delta", NOW
        )
        assert {r.id for r in await uow.memories.query(query)} == {
            s.belief_id for s in view.sources
        }


SCENARIOS.append(pending_merge_does_not_reintroduce_originals_into_session_delta)
