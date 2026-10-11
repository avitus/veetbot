"""Remaining-budget summary recall; originals retain their ranking and priority."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import MemoryRecord, RecalledBelief, RecalledSummary, RecallQuery
from agent_core.domain.people_sources import source_id
from agent_core.memory.identity_recall import identity_scoped_records
from agent_core.ports.persistence import RepositoryUnitOfWork


async def summary_candidates(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    query: RecallQuery,
    member_ids: tuple[UUID, ...],
    selected_ids: set[UUID],
    now: datetime,
    score_original: Callable[[MemoryRecord], RecalledBelief | None],
    *,
    owner_approved_only: bool = False,
) -> list[RecalledSummary]:
    summaries = await uow.reconsolidation.active_summaries(
        principal,
        member_ids,
        now,
        ceiling=query.sensitivity_ceiling,
        current_scope=query.current_scope,
        as_of=query.as_of,
        known_at=query.known_at,
        min_store_position=query.min_store_position,
    )
    result = []
    for summary in summaries:
        if owner_approved_only:
            operation = await uow.reconsolidation.summary_operation(principal, summary.operation_id)
            if (
                operation is None
                or operation.owner_review is None
                or operation.owner_review.decision != "approved"
            ):
                continue
        content = summary.content
        clause_ids = {key for clause in content.clauses for key in clause.source_ids}
        if (
            (summary.kind == "summary" and clause_ids & selected_ids)
            or clause_ids & set(query.exclude_ids)
            or summary.id in query.exclude_ids
        ):
            continue
        if query.include_ids is not None and summary.id not in query.include_ids:
            continue
        # Retain every required source in erasure lineage, including unrendered
        # clauses, and respect the snapshot's ban on provisional original support.
        originals = []
        for key in content.plan.member_ids:
            try:
                originals.append(
                    await uow.memories.get(key, principal)
                    if query.known_at is None
                    else await uow.memories.get_at(key, principal, known_at=query.known_at)
                )
            except NotFoundError:
                break
        if len(originals) != len(content.plan.member_ids) or any(
            record.status.value == "provisional" and not query.include_provisional
            for record in originals
        ):
            continue
        if query.people_scope is not None and len(
            await identity_scoped_records(uow.people, query, originals)
        ) != len(originals):
            continue
        rendered_originals = [r for r in originals if r.id in clause_ids]
        if query.belief_types and any(
            r.belief_type not in query.belief_types for r in rendered_originals
        ):
            continue
        scores = [score_original(r) for r in rendered_originals]
        if not scores or any(item is not None and item.blocked for item in scores):
            continue
        admitted = [item for item in scores if item is not None]
        if not admitted or (summary.kind == "summary" and len(admitted) != len(scores)):
            continue
        carried = content.scope not in {query.current_scope, "user", "global"}
        band = (
            "high"
            if content.confidence >= 0.8
            else "medium"
            if content.confidence >= 0.55
            else "low"
        )
        if carried:
            band = "medium" if band == "high" else "low"
        result.append(
            RecalledSummary(
                record_kind=summary.kind,
                subject=content.subject,
                belief_id=summary.id,
                operation_id=summary.operation_id,
                operation_revision=summary.revision,
                statement=content.rendered,
                belief_types=content.belief_types,
                confidence_band=band,
                authority=content.authority,
                origin_scope=content.scope,
                portability=content.portability,
                sensitivity=content.sensitivity,
                carried=carried,
                valid_from=content.valid_from,
                valid_to=content.expires_at,
                score=min(item.score for item in admitted),
                arms=[summary.kind],
                support_ids=content.plan.member_ids,
                source_ids=tuple(
                    sorted(
                        {
                            source_id(principal, dep.source_session_id, seq)
                            for dep in content.plan.dependencies
                            for seq in dep.source_event_ids
                        }
                    )
                ),
                source_session_ids=tuple(
                    sorted({dep.source_session_id for dep in content.plan.dependencies})
                ),
                clause_source_ids=tuple(sorted(clause_ids)),
            )
        )
    return sorted(result, key=lambda item: (-item.score, str(item.belief_id)))
