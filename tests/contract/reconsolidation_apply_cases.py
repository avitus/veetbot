"""Reviewed candidates reuse local planning and transactional writes in both stores."""

import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.reconsolidation_inputs import PreparedVerificationRequest
from agent_core.domain.reconsolidation_provider import ProposalReview
from agent_core.memory.reconsolidation_admission import ReconsolidationRequestAdmission
from agent_core.memory.reconsolidation_apply import apply_review
from agent_core.memory.reconsolidation_provider import proposal_digest, review_prepared_verification
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_admission_cases import model, policy
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_input_cases import named
from tests.contract.reconsolidation_source_cases import seed_sources
from tests.contract.support import NOW, principal


async def prepared_batch(
    factory: Factory,
    *,
    kind: str = "merge_equivalent",
    mixed: bool = False,
    overlap: bool = False,
    change: Callable[[list[dict[str, Any]]], None] | None = None,
    reject_first: bool = False,
    independent: bool = False,
) -> tuple[PreparedVerificationRequest, ProposalReview]:
    async with factory() as uow:
        await seed_sources(uow)
        if independent:
            from agent_core.domain.events import NewEvent
            from tests.contract.support import SESSION_ID

            event = await uow.events.append(
                NewEvent(
                    session_id=SESSION_ID,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=principal().principal_id,
                    payload={"content": "I like brief explanations."},
                )
            )
            original = await uow.memories.get(UUID(int=502), principal())
            await uow.memories.reinforce(
                original.model_copy(
                    update={
                        "statement": "User likes brief explanations.",
                        "source_event_ids": [event.sequence],
                    }
                )
            )
        for i in (503, 504):
            await uow.memories.upsert_belief(
                memory(belief_id=i).model_copy(
                    update={
                        "store_position": await uow.memories.next_position(),
                        "source_event_ids": [1],
                    }
                )
            )
        job = await uow.reconsolidation.claim_due(principal(), NOW, "apply-test")
        assert job is not None
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        groups = (
            (page.sources, page.sources[:2])
            if overlap
            else (page.sources[:2], page.sources[2:])
            if mixed
            else (page.sources,)
        )
        await uow.reconsolidation.checkpoint(principal(), job.lease_token, page, groups, NOW)
        claimed = [
            await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW) for _ in groups
        ]
        group_ids = tuple(g.id for g in claimed if g is not None)
    admission = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    first, spend = await admission.proposal(
        job.lease_token, group_ids, model=model(), batch_id=UUID(int=90)
    )
    assert first.prepared is not None and spend is not None
    ops: list[dict[str, Any]] = []
    for group in first.prepared.context.groups:
        sources = group.sources
        subsets = (sources,) if mixed or len(sources) == 2 else (sources[:2], sources[2:])
        for subset in subsets:
            ops.append(
                {
                    "id": str(UUID(int=900 + len(ops))),
                    "group_id": str(group.id),
                    "kind": kind,
                    "inputs": [s.source.model_dump(mode="json") for s in subset],
                    "clauses": []
                    if kind in {"no_change", "flag_conflict"}
                    else [
                        {
                            "text": memory().statement,
                            "support": [
                                {
                                    "belief_id": str(s.source.belief_id),
                                    "excerpt_id": str(s.excerpt_ids[0]),
                                }
                                for s in subset
                            ],
                        }
                    ],
                }
            )
    if kind == "no_change":
        ops = [
            dict(
                ops[0],
                inputs=[
                    s.source.model_dump(mode="json")
                    for s in first.prepared.context.groups[0].sources
                ],
            )
        ]
    if change is not None:
        change(ops)
    proposal = json.dumps(
        {
            "schema_version": "reconsolidation-proposal@1",
            "batch_id": str(UUID(int=90)),
            "group_ids": [str(g) for g in group_ids],
            "operations": ops,
        }
    )
    async with factory() as uow:
        await uow.reconsolidation.settle(principal(), job.lease_token, spend.id, None, NOW)
    second, spend = await admission.verification(
        first.prepared, proposal, model=model(), remaining_usd=Decimal("0.2")
    )
    assert second.prepared is not None and spend is not None
    async with factory() as uow:
        await uow.reconsolidation.settle(principal(), job.lease_token, spend.id, None, NOW)
    review = review_prepared_verification(
        second.prepared,
        json.dumps(
            {
                "schema_version": "reconsolidation-verification@1",
                "batch_id": str(UUID(int=90)),
                "proposal_digest": proposal_digest(second.prepared.proposal),
                "verdicts": [
                    {
                        "operation_id": str(op.id),
                        "clause_index": i,
                        "status": "unsupported"
                        if reject_first and op.id == UUID(int=900)
                        else "supported",
                        "reason": "contradicted"
                        if reject_first and op.id == UUID(int=900)
                        else "entailed",
                    }
                    for op in second.prepared.proposal.operations
                    if op.id not in {r.operation.id for r in second.prepared.local_rejections}
                    for i, _ in enumerate(op.clauses)
                ],
            }
        ),
    )
    return second.prepared, review


async def reviewed_subsets_commit_together(
    factory: Factory, *, kind: str = "merge_equivalent"
) -> None:
    prepared, review = await prepared_batch(factory, kind=kind)
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert len(applied) == 1 and applied[0].outcome == "committed", (
        "reviewed subsets need one atomic group outcome"
    )
    assert len(applied[0].operation_ids) == 2
    async with factory() as uow:
        operations = await uow.reconsolidation.operation_page(
            principal(), kind=None, state=None, before=None, limit=100
        )
        assert {v.id for v in operations} == set(applied[0].operation_ids)
        assert {v.plan.member_ids for v in operations} == {
            (UUID(int=501), UUID(int=502)),
            (UUID(int=503), UUID(int=504)),
        }
        current = await uow.reconsolidation.renew(principal(), prepared.lease_token, NOW)
        assert current.operations == 2 and current.requests == 2
        await uow.reconsolidation.release(principal(), prepared.lease_token, NOW)
        for source_key in (501, 502, 503, 504):
            assert (
                await uow.memories.get(UUID(int=source_key), principal())
            ).statement == memory().statement


async def reviewed_summary_subsets(factory: Factory) -> None:
    await reviewed_subsets_commit_together(factory, kind="summarize_related")


async def repeated_summary_inputs(factory: Factory, *, across_groups: bool = False) -> None:
    def repeat(ops: list[dict[str, Any]]) -> None:
        ops[1] = dict(ops[0], id=ops[1]["id"])

    prepared, review = await prepared_batch(
        factory,
        kind="summarize_related",
        overlap=across_groups,
        change=None if across_groups else repeat,
    )
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    expected = 2 if across_groups else 1
    assert sum(len(group.operation_ids) for group in applied) == expected, (
        "repeated source inputs must not create duplicate summaries"
    )
    assert all(group.outcome in {"committed", "no_change"} for group in applied)
    async with factory() as uow:
        job = await uow.reconsolidation.renew(principal(), prepared.lease_token, NOW)
        assert job.operations == expected
        operations = await uow.reconsolidation.operation_page(
            principal(), kind=None, state=None, before=None, limit=100
        )
        assert len(operations) == expected
        assert len({operation.evidence_identity for operation in operations}) == expected


APPLY_SCENARIOS: list[Callable[[Factory], Awaitable[None]]] = [
    reviewed_subsets_commit_together,
    reviewed_summary_subsets,
    repeated_summary_inputs,
    named(repeated_summary_inputs, "repeated_summary_inputs_across_groups", across_groups=True),
]


async def application_preserves_good_sibling(factory: Factory, *, rejected: bool = False) -> None:
    def fabricate(ops: list[dict[str, Any]]) -> None:
        ops[0]["clauses"][0]["text"] = "The owner prefers long answers."

    prepared, review = await prepared_batch(
        factory, reject_first=rejected, change=None if rejected else fabricate
    )
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert applied[0].outcome == "committed" and len(applied[0].operation_ids) == 1
    async with factory() as uow:
        operation = await uow.reconsolidation.get_merge(
            principal(), applied[0].operation_ids[0], NOW
        )
        assert operation is not None and operation.plan.member_ids == (UUID(int=503), UUID(int=504))


async def application_stale_group_preserves_other_group(
    factory: Factory, *, erased: bool = False
) -> None:
    prepared, review = await prepared_batch(factory, mixed=True)
    stale = prepared.context.groups[0].sources[0].source.belief_id
    async with factory() as uow:
        if erased:
            await uow.memories.fence_for_erasure(principal(), [stale])
        else:
            original = await uow.memories.get(stale, principal())
            await uow.memories.reinforce(original.model_copy(update={"confidence": 0.8}))
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert [g.outcome for g in applied] == ["retry", "committed"]
    assert len(applied[1].operation_ids) == 1
    async with factory() as uow:
        current = await uow.reconsolidation.renew(principal(), prepared.lease_token, NOW)
        assert current.operations == 1
        retried = await uow.reconsolidation.claim_group(principal(), prepared.lease_token, NOW)
        assert retried is None  # Old revisions are durably stale, never re-admitted.


async def application_group_rollback(factory: Factory) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    import pytest

    from tests.contract.reconsolidation_cases import Stores

    prepared, review = await prepared_batch(factory)
    enabled = True

    @asynccontextmanager
    async def failing() -> AsyncIterator[Stores]:
        async with factory() as uow:
            yield uow
            raise RuntimeError("failed group commit")

    with pytest.raises(RuntimeError, match="failed group commit"):
        await apply_review(
            cast(UnitOfWorkFactory, failing),
            FixedClock(NOW),
            principal(),
            prepared,
            review,
            admitted=lambda: enabled,
        )
    async with factory() as uow:
        assert (
            await uow.reconsolidation.operation_page(
                principal(), kind=None, state=None, before=None, limit=100
            )
            == ()
        )
        current = await uow.reconsolidation.renew(principal(), prepared.lease_token, NOW)
        assert current.operations == 0
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert len(applied[0].operation_ids) == 2
    again = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert again[0].outcome == "deferred" and not again[0].operation_ids


async def application_binding_refuses_mutations(factory: Factory, *, mode: str) -> None:
    import pytest

    from agent_core.domain.errors import ConflictError
    from agent_core.memory.reconsolidation_provider import ProviderBatchError

    prepared, review = await prepared_batch(factory)
    owner = principal()
    if mode == "owner":
        owner = owner.model_copy(update={"principal_id": "other-owner"})
    elif mode == "batch":
        review = review.model_copy(update={"batch_id": UUID(int=66)})
    elif mode == "digest":
        review = review.model_copy(update={"proposal_digest": "f" * 64})
    elif mode == "candidate":
        review = review.model_copy(update={"candidates": review.candidates[:1]})
    else:
        prepared = prepared.model_copy(
            update={"serialized_request": prepared.serialized_request + " "}
        )
    with pytest.raises((ConflictError, ProviderBatchError)):
        await apply_review(
            cast(UnitOfWorkFactory, factory),
            FixedClock(NOW),
            owner,
            prepared,
            review,
            admitted=lambda: True,
        )
    async with factory() as uow:
        assert (
            await uow.reconsolidation.operation_page(
                principal(), kind=None, state=None, before=None, limit=100
            )
            == ()
        )


async def application_rejects_dependent_connection(factory: Factory) -> None:
    prepared, review = await prepared_batch(factory, kind="infer_connection")
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert applied[0].outcome == "no_change" and not applied[0].operation_ids
    async with factory() as uow:
        retried = await uow.reconsolidation.claim_group(principal(), prepared.lease_token, NOW)
        assert retried is None


async def application_no_change(factory: Factory) -> None:
    prepared, review = await prepared_batch(factory, kind="no_change")
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert applied[0].outcome == "no_change" and not applied[0].operation_ids
    async with factory() as uow:
        await uow.reconsolidation.release(principal(), prepared.lease_token, NOW)


async def application_admission_withdrawal(factory: Factory, *, expired: bool = False) -> None:
    from datetime import timedelta

    prepared, review = await prepared_batch(factory)
    clock = FixedClock(NOW + timedelta(seconds=120) if expired else NOW)
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        clock,
        principal(),
        prepared,
        review,
        admitted=lambda: expired,
    )
    assert not applied if not expired else applied[0].outcome == "deferred"
    async with factory() as uow:
        assert (
            await uow.reconsolidation.operation_page(
                principal(), kind=None, state=None, before=None, limit=100
            )
            == ()
        )


async def subset_planners_do_not_admit_foreign_ids(factory: Factory) -> None:
    import pytest

    from agent_core.domain.errors import ConflictError

    prepared, _ = await prepared_batch(factory)
    token, group = prepared.lease_token, prepared.context.groups[0].id
    for ids in ((UUID(int=501), UUID(int=777)), (UUID(int=501), UUID(int=501)), (UUID(int=501),)):
        async with factory() as uow:
            with pytest.raises(ConflictError):
                await uow.reconsolidation.plan_merge(principal(), token, group, NOW, source_ids=ids)
        async with factory() as uow:
            with pytest.raises(ConflictError):
                await uow.reconsolidation.plan_summary(
                    principal(), token, group, (), NOW, source_ids=ids
                )
    async with factory() as uow:
        with pytest.raises(ConflictError, match="no committed operations"):
            await uow.reconsolidation.finish_group(principal(), token, group, "committed", NOW)


APPLY_SCENARIOS += [
    application_preserves_good_sibling,
    named(application_preserves_good_sibling, "application_rejected_sibling", rejected=True),
    application_stale_group_preserves_other_group,
    named(application_stale_group_preserves_other_group, "application_erased_group", erased=True),
    application_group_rollback,
    *[
        named(application_binding_refuses_mutations, f"application_binding_{mode}", mode=mode)
        for mode in ("owner", "batch", "digest", "candidate", "request")
    ],
    application_rejects_dependent_connection,
    application_no_change,
    application_admission_withdrawal,
    named(application_admission_withdrawal, "application_expired", expired=True),
    subset_planners_do_not_admit_foreign_ids,
]


async def application_kill_switch_rolls_back_earlier_candidates(factory: Factory) -> None:
    prepared, review = await prepared_batch(factory)
    checks = 0

    def admitted() -> bool:
        nonlocal checks
        checks += 1
        return checks <= 3

    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=admitted,
    )
    assert applied[0].outcome == "deferred" and checks == 4
    async with factory() as uow:
        assert (
            await uow.reconsolidation.operation_page(
                principal(), kind=None, state=None, before=None, limit=100
            )
            == ()
        )
        assert (
            await uow.reconsolidation.renew(principal(), prepared.lease_token, NOW)
        ).operations == 0


async def application_honors_owner_undo(factory: Factory) -> None:
    prepared, review = await prepared_batch(factory)
    group = prepared.context.groups[0].id
    async with factory() as uow:
        plan = await uow.reconsolidation.plan_merge(
            principal(), prepared.lease_token, group, NOW, source_ids=(UUID(int=501), UUID(int=502))
        )
        assert plan is not None
        original = await uow.reconsolidation.commit_merge(
            principal(), prepared.lease_token, group, plan, NOW, complete_group=False
        )
        await uow.reconsolidation.undo_merge(principal(), original.id, 1, "owner-undo", NOW)
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    # All four copies cite the same original event; owner undo blocks recreating
    # the same rejected claim under the other two record IDs as well.
    assert applied[0].outcome == "no_change" and not applied[0].operation_ids
    async with factory() as uow:
        saved = await uow.reconsolidation.get_merge(principal(), original.id, NOW)
        assert saved is not None and saved.state == "undone"


APPLY_SCENARIOS += [
    application_kill_switch_rolls_back_earlier_candidates,
    application_honors_owner_undo,
]


async def provider_execution_to_local_commit(factory: Factory) -> None:
    from collections.abc import AsyncIterator

    from agent_core.domain.messages import (
        AssistantMessage,
        ModelAttempt,
        ModelCompletedEvent,
        ModelEvent,
        ModelRequest,
        ResolvedModel,
        TextPart,
        UserMessage,
    )
    from agent_core.memory.reconsolidation_execution import ReconsolidationBatchExecutor
    from tests.contract.reconsolidation_execution_cases import (
        ExecutionProvider,
        TrackedFactory,
        reply,
    )
    from tests.contract.reconsolidation_source_cases import seed

    tracked = TrackedFactory(factory)
    async with tracked() as uow:
        job, group = await seed(uow)

    class ProposesMerge(ExecutionProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            message = request.conversation[1]
            assert isinstance(message, UserMessage) and isinstance(message.content[0], TextPart)
            payload = json.loads(message.content[0].text)
            response = json.loads(reply(request))
            if "operations" in response:
                op = response["operations"][0]
                op["kind"] = "merge_equivalent"
                op["clauses"] = [
                    {
                        "text": memory().statement,
                        "support": [
                            {"belief_id": s["belief_id"], "excerpt_id": s["excerpt_ids"][0]}
                            for s in payload["groups"][0]["sources"]
                        ],
                    }
                ]
            else:
                response["verdicts"] = [
                    {
                        "operation_id": op["id"],
                        "clause_index": i,
                        "status": "supported",
                        "reason": "entailed",
                    }
                    for op in payload["operations"]
                    for i, _ in enumerate(op["clauses"])
                ]
            async for event in super().stream(request, resolved, attempt):
                assert isinstance(event, ModelCompletedEvent)
                yield event.model_copy(
                    update={
                        "turn": event.turn.model_copy(
                            update={
                                "assistant_messages": [
                                    AssistantMessage(
                                        item_index=0, content=[TextPart(text=json.dumps(response))]
                                    )
                                ]
                            }
                        )
                    }
                )

    provider = ProposesMerge(tracked)
    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    execution = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert (
        execution.prepared is not None
        and execution.review is not None
        and execution.reason == "reviewed"
    )
    applied = await apply_review(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        execution.prepared,
        execution.review,
        admitted=lambda: True,
    )
    assert applied[0].outcome == "committed" and len(applied[0].operation_ids) == 1
    assert len(provider.requests) == 2 and all(c.spend.state == "settled" for c in execution.calls)


APPLY_SCENARIOS.append(provider_execution_to_local_commit)


async def reviewed_connection_is_tentative(factory: Factory) -> None:
    def connection(ops: list[dict[str, Any]]) -> None:
        del ops[1:]
        ops[0]["clauses"][0]["text"] = "User may prefer concise explanations."

    prepared, review = await prepared_batch(
        factory, kind="infer_connection", independent=True, change=connection
    )
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert applied[0].outcome == "committed", "a verified independent connection must form"
    assert len(applied[0].operation_ids) == 1
    from datetime import timedelta

    from agent_core.domain.memory import MemoryAuthority, Sensitivity

    async with factory() as uow:
        key = applied[0].operation_ids[0]
        view = await uow.reconsolidation.get_operation(
            principal(), key, NOW, ceiling=Sensitivity.INTERNAL
        )
        assert view is not None and view.kind == "hypothesis" and view.content is not None
        assert "tentative" in view.content.statement.lower()
        value = await uow.reconsolidation.get_summary(
            principal(), key, NOW, ceiling=Sensitivity.INTERNAL, current_scope=memory().scope
        )
        assert value is not None and value.kind == "hypothesis"
        assert (
            value.content.confidence <= 0.35 and value.content.authority == MemoryAuthority.INFERRED
        )
        assert value.content.expires_at == value.content.last_evidence_at + timedelta(days=30)
        assert value.content.valid_from == NOW
        assert len(value.content.plan.dependencies) == 2


async def reviewed_conflict_preserves_originals(factory: Factory) -> None:
    prepared, review = await prepared_batch(factory, kind="flag_conflict")
    applied = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert applied[0].outcome == "committed", "a reviewed conflict needs inspectable lineage"
    from agent_core.domain.memory import Sensitivity

    async with factory() as uow:
        for key in applied[0].operation_ids:
            view = await uow.reconsolidation.get_operation(
                principal(), key, NOW, ceiling=Sensitivity.INTERNAL
            )
            assert view is not None and view.kind == "conflict" and len(view.sources) == 2
            assert (
                await uow.reconsolidation.get_summary(
                    principal(),
                    key,
                    NOW,
                    ceiling=Sensitivity.INTERNAL,
                    current_scope=memory().scope,
                )
                is None
            )
        for source_key in (501, 502, 503, 504):
            assert (
                await uow.memories.get(UUID(int=source_key), principal())
            ).statement == memory().statement


APPLY_SCENARIOS += [reviewed_connection_is_tentative, reviewed_conflict_preserves_originals]


async def connection_lifecycle(factory: Factory, *, mode: str) -> None:
    from datetime import timedelta

    from agent_core.domain.memory import Sensitivity

    def connection(ops: list[dict[str, Any]]) -> None:
        del ops[1:]
        ops[0]["clauses"][0]["text"] = "User may prefer concise explanations."

    prepared, review = await prepared_batch(
        factory, kind="infer_connection", independent=True, change=connection
    )
    result = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    key = result[0].operation_ids[0]
    at = NOW
    async with factory() as uow:
        before = await uow.reconsolidation.get_summary(
            principal(), key, at, ceiling=Sensitivity.INTERNAL, current_scope=memory().scope
        )
        assert before is not None
        if mode == "usage":
            assert await uow.reconsolidation.update_summary_usage(
                principal(), key, 0.1, at + timedelta(seconds=1), cited=True
            )
        elif mode == "erasure":
            await uow.memories.fence_for_erasure(principal(), [UUID(int=501)])
        elif mode == "changed":
            source = await uow.memories.get(UUID(int=501), principal())
            await uow.memories.reinforce(
                source.model_copy(update={"statement": "User now prefers long answers."})
            )
        elif mode == "expired":
            at += timedelta(days=30)
        else:
            await uow.reconsolidation.change_summary(principal(), key, "untrue", at)
    async with factory() as uow:
        after = await uow.reconsolidation.get_summary(
            principal(), key, at, ceiling=Sensitivity.INTERNAL, current_scope=memory().scope
        )
        if mode == "usage":
            assert after is not None and after.last_used_at == NOW + timedelta(seconds=1)
            assert after.content == before.content
            other = await uow.reconsolidation.get_summary(
                principal().model_copy(update={"principal_id": "foreign"}),
                key,
                at,
                ceiling=Sensitivity.INTERNAL,
                current_scope=memory().scope,
            )
            assert other is None
        else:
            assert after is None
            historical = await uow.reconsolidation.get_summary(
                principal(),
                key,
                at,
                ceiling=Sensitivity.INTERNAL,
                current_scope=memory().scope,
                as_of=NOW,
            )
            assert historical is None
            if mode == "rejected":
                view = await uow.reconsolidation.get_operation(
                    principal(), key, at, ceiling=Sensitivity.INTERNAL
                )
                assert view is not None and view.content is None and view.sources == ()


APPLY_SCENARIOS += [
    named(connection_lifecycle, f"connection_{mode}", mode=mode)
    for mode in ("usage", "erasure", "changed", "expired", "rejected")
]


async def connection_honors_rejected_claim(factory: Factory) -> None:
    import hashlib

    from agent_core.domain.memory import BeliefRejection, RejectionKind

    claim = "User may prefer concise explanations."

    def connection(ops: list[dict[str, Any]]) -> None:
        del ops[1:]
        ops[0]["clauses"][0]["text"] = claim

    prepared, review = await prepared_batch(
        factory, kind="infer_connection", independent=True, change=connection
    )
    async with factory() as uow:
        rejected = memory(belief_id=599).model_copy(
            update={"statement": claim, "subject": "unrelated-name", "store_position": 5}
        )
        await uow.memories.upsert_belief(rejected)
        await uow.memories.delete(
            rejected.id,
            principal(),
            BeliefRejection(
                id=UUID(int=901),
                tenant_id=rejected.tenant_id,
                principal_id=rejected.principal_id,
                belief_id=rejected.id,
                kind=RejectionKind.DELETED,
                subject=rejected.subject,
                statement=None,
                statement_sha256=hashlib.sha256(claim.casefold().encode()).hexdigest(),
                belief_type=rejected.belief_type,
                scope=rejected.scope,
                created_at=NOW,
            ),
        )
    result = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert result[0].outcome == "no_change" and not result[0].operation_ids


APPLY_SCENARIOS.append(connection_honors_rejected_claim)
