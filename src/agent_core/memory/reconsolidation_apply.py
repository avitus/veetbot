"""Apply reviewed candidates through existing deterministic store planners."""

from collections.abc import Callable
from typing import Literal
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.reconsolidation_execution import AppliedGroup
from agent_core.domain.reconsolidation_inputs import PreparedVerificationRequest
from agent_core.domain.reconsolidation_merge import normalize_claim
from agent_core.domain.reconsolidation_provider import ProposalReview, ProposedOperation
from agent_core.domain.reconsolidation_summary import SummaryClause
from agent_core.memory.reconsolidation_provider import (
    candidate_input_failure,
    proposal_digest,
    validate_prepared_verification,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


def _check_review(
    principal: Principal, prepared: PreparedVerificationRequest, review: ProposalReview
) -> None:
    validate_prepared_verification(prepared)
    owner = (principal.tenant_id, principal.principal_id)
    if (
        (prepared.context.tenant_id, prepared.context.principal_id) != owner
        or (review.tenant_id, review.principal_id) != owner
        or review.batch_id != prepared.context.batch_id
        or review.proposal_digest != proposal_digest(prepared.proposal)
        or tuple(c.operation for c in review.candidates) != prepared.proposal.operations
        or any(r not in review.candidates for r in prepared.local_rejections)
    ):
        raise ConflictError("review does not match its prepared batch")


async def _commit(
    uow: RepositoryUnitOfWork,
    clock: Clock,
    principal: Principal,
    token: UUID,
    operation: ProposedOperation,
    model_identity: str,
) -> UUID | None:
    source_ids = tuple(ref.belief_id for ref in operation.inputs)
    store = uow.reconsolidation
    if operation.kind == "merge_equivalent":
        plan = await store.plan_merge(
            principal, token, operation.group_id, clock.now(), source_ids=source_ids
        )
        if plan is None:
            return None
        original = await uow.memories.get(plan.canonical_id, principal)
        if normalize_claim(operation.clauses[0].text) != normalize_claim(original.statement):
            return None
        value = await store.commit_merge(
            principal, token, operation.group_id, plan, clock.now(), complete_group=False
        )
        return value.id
    if operation.kind == "flag_conflict":
        conflict = await store.commit_conflict(
            principal,
            token,
            operation.group_id,
            source_ids,
            clock.now(),
            complete_group=False,
            model_identity=model_identity,
        )
        return None if conflict is None else conflict.id
    if operation.kind in {"summarize_related", "infer_connection"}:
        clauses = tuple(
            SummaryClause(
                text=clause.text,
                source_ids=tuple(sorted({support.belief_id for support in clause.support})),
            )
            for clause in operation.clauses
        )
        summary = await store.plan_summary(
            principal,
            token,
            operation.group_id,
            clauses,
            clock.now(),
            source_ids=source_ids,
            kind="hypothesis" if operation.kind == "infer_connection" else "summary",
        )
        if summary is None:
            return None
        derived = await store.commit_summary(
            principal,
            token,
            operation.group_id,
            summary,
            clock.now(),
            complete_group=False,
            model_identity=model_identity,
        )
        return derived.id
    return None


async def apply_review(
    factory: UnitOfWorkFactory,
    clock: Clock,
    principal: Principal,
    prepared: PreparedVerificationRequest,
    review: ProposalReview,
    *,
    admitted: Callable[[], bool],
    merge_only: bool = False,
    owner_review: bool = False,
) -> tuple[AppliedGroup, ...]:
    """Finish each group atomically; a stale group cannot discard its siblings.

    The review is the executor's local result, never provider-supplied authority.
    Existing planners revalidate exact sources, evidence, rejection and undo rules.
    Provider execution and its call audit are separate from these local writes.
    """
    _check_review(principal, prepared, review)
    results: list[AppliedGroup] = []
    for offered in prepared.context.groups:
        if not admitted():
            break
        candidates = tuple(c for c in review.candidates if c.operation.group_id == offered.id)
        try:
            async with factory() as uow, uow.people.lock(principal):
                current = await uow.reconsolidation.renew(
                    principal, prepared.lease_token, clock.now()
                )
                if current.id != prepared.job_id or not admitted():
                    raise ConflictError("reconsolidation admission changed")
                fresh = await uow.reconsolidation.original_input(
                    principal, prepared.lease_token, offered.id, clock.now()
                )
                expected = {
                    (s.source.belief_id, s.source.content_revision) for s in offered.sources
                }
                valid = fresh is not None and expected == {
                    (s.version.belief_id, s.version.content_revision) for s in fresh.sources
                }
                accepted = tuple(c.operation for c in candidates if c.requires_local_validation)
                operation_ids: list[UUID] = []
                if valid:
                    for op in accepted:
                        if merge_only and op.kind != "merge_equivalent":
                            continue
                        if not admitted():
                            raise ConflictError("reconsolidation admission withdrawn")
                        if candidate_input_failure(op, offered) is not None:
                            continue
                        operation_id = await _commit(
                            uow,
                            clock,
                            principal,
                            prepared.lease_token,
                            op,
                            prepared.request.metadata["provider"]
                            + ":"
                            + prepared.request.metadata["model"],
                        )
                        if operation_id is not None:
                            if owner_review:
                                await uow.reconsolidation.stage_owner_review(
                                    principal, prepared.lease_token, operation_id, clock.now()
                                )
                            operation_ids.append(operation_id)
                if not admitted():
                    raise ConflictError("reconsolidation admission withdrawn")
                outcome: Literal["committed", "no_change", "retry"] = (
                    "committed" if operation_ids else "no_change" if valid else "retry"
                )
                await uow.reconsolidation.finish_group(
                    principal, prepared.lease_token, offered.id, outcome, clock.now()
                )
            results.append(
                AppliedGroup(
                    group_id=offered.id, outcome=outcome, operation_ids=tuple(operation_ids)
                )
            )
        except (ConflictError, NotFoundError):
            # The group UOW rolled back. Recovery/retry remains fenced by its lease.
            results.append(AppliedGroup(group_id=offered.id, outcome="deferred"))
    return tuple(results)
