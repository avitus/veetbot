"""Current source validation for the safe view of a frozen recall trace."""

from agent_core.domain.agents import Principal
from agent_core.domain.memory import SENSITIVITY_ORDER, RecallTrace, Sensitivity, TracedBelief
from agent_core.ports.determinism import Clock
from agent_core.ports.reconsolidation import ReconsolidationStore


async def trace_beliefs(
    trace: RecallTrace,
    ceiling: Sensitivity,
    reconsolidation: ReconsolidationStore | None,
    clock: Clock | None,
) -> list[TracedBelief]:
    effective = min((trace.sensitivity_ceiling, ceiling), key=SENSITIVITY_ORDER.__getitem__)
    owner = Principal(tenant_id=trace.tenant_id, principal_id=trace.principal_id)
    result = []
    for item in trace.beliefs:
        if SENSITIVITY_ORDER[item.sensitivity] > SENSITIVITY_ORDER[effective] or item.blocked:
            continue
        if item.record_kind != "belief":
            # Older/custom compositions without current-source validation fail closed.
            if reconsolidation is None or clock is None:
                continue
            current = await reconsolidation.get_summary(
                owner,
                item.operation_id,
                clock.now(),
                ceiling=effective,
                current_scope=trace.query.current_scope,
                as_of=trace.query.as_of
                or (trace.created_at if trace.query.known_at is not None else None),
                known_at=trace.query.known_at,
            )
            if current is None or current.revision != item.operation_revision:
                continue
        result.append(
            TracedBelief(
                belief_id=item.belief_id,
                record_kind=item.record_kind,
                operation_id=item.operation_id if item.record_kind != "belief" else None,
                support_ids=item.support_ids if item.record_kind != "belief" else (),
                subject=item.subject,
                statement=item.statement,
                learned_at=item.valid_from,
                origin_scope=item.origin_scope,
                carried=item.carried,
                authority=item.authority,
                source_event_id=item.source_event_ids[0] if item.source_event_ids else None,
                confidence_band=item.confidence_band,
                used=item.belief_id in trace.cited,
            )
        )
    return result
