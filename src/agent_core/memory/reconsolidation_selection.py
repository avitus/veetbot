"""Fit complete original evidence before group membership becomes durable."""

from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation import SourceVersion, group_digest
from agent_core.domain.reconsolidation_inputs import ReconsolidationInput
from agent_core.memory.reconsolidation_inputs import EgressPolicy, _egress, prepare_proposal_request


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
    allowed = []
    for source in original:
        value = _subset(preview, (source,))
        _, _, reason = _egress(principal, model, egress_policy, value, now)
        if reason == "egress_unavailable":
            return original
        if reason is None:
            allowed.append(source)
    # Refused anchors still produce the explicit refusal. Eligible anchors can
    # select their own siblings without exporting any part of a refused source.
    if anchor not in allowed or len(allowed) < 2:
        return original
    neighbors = [source for source in allowed if source != anchor]
    for count in range(len(allowed), 1, -1):
        wanted = {anchor, *neighbors[: count - 1]}
        versions = tuple(source for source in original if source in wanted)
        value = _subset(preview, versions)
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


def _subset(
    preview: ReconsolidationInput, versions: tuple[SourceVersion, ...]
) -> ReconsolidationInput:
    sources = tuple(source for source in preview.sources if source.version in versions)
    leaves = {
        (source.record.source_session_id, sequence)
        for source in sources
        for sequence in source.record.source_event_ids
    }
    return preview.model_copy(
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
