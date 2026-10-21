"""Fit complete original evidence before group membership becomes durable."""

from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation import SourceVersion, group_digest
from agent_core.domain.reconsolidation_inputs import ReconsolidationInput
from agent_core.memory.reconsolidation_inputs import EgressPolicy, prepare_proposal_request


def fit_selection(
    principal: Principal,
    preview: ReconsolidationInput,
    anchor: SourceVersion,
    *,
    model: ResolvedModel,
    egress_policy: EgressPolicy,
    now: datetime,
) -> tuple[SourceVersion, ...]:
    """Keep the anchor and highest-ranked neighbors that fit the real serializer.

    This is a local preview, not a reservation. Keep an unfittable group intact
    so ordinary admission reports its refusal instead of claiming no work exists.
    """
    original = preview.group.sources
    if anchor not in original:
        return original
    neighbors = [source for source in original if source != anchor]
    for count in range(len(original), 1, -1):
        wanted = {anchor, *neighbors[: count - 1]}
        sources = tuple(source for source in preview.sources if source.version in wanted)
        versions = tuple(source for source in original if source in wanted)
        leaves = {
            (source.record.source_session_id, sequence)
            for source in sources
            for sequence in source.record.source_event_ids
        }
        value = preview.model_copy(
            update={
                "group": preview.group.model_copy(
                    update={
                        "sources": versions,
                        "input_digest": group_digest(versions),
                    }
                ),
                "sources": sources,
                "excerpts": tuple(
                    excerpt
                    for excerpt in preview.excerpts
                    if (excerpt.session_id, excerpt.event_sequence) in leaves
                ),
            }
        )
        result = prepare_proposal_request(
            principal,
            (value,),
            model=model,
            egress_policy=egress_policy,
            batch_id=UUID(int=0),
            now=now,
        )
        if result.prepared is not None:
            return versions
        if any(item.reason not in {"input_budget", "cost_budget"} for item in result.deferred):
            break
    return original
