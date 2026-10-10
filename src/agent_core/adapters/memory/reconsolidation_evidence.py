"""Bounded authenticated original evidence for merge and summary preparation."""

import hashlib
from datetime import datetime
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import SENSITIVITY_ORDER, MemoryRecord, Portability, Sensitivity
from agent_core.domain.people import PeopleSource, Person, PersonMemoryLink, PersonMention
from agent_core.domain.people_sources import source_id
from agent_core.domain.reconsolidation import ReconsolidationGroup, SourceVersion
from agent_core.domain.reconsolidation_inputs import OriginalExcerpt, ReconsolidationInput
from agent_core.domain.reconsolidation_merge import (
    AttributionRole,
    MergePlan,
    MergeSource,
    normalize_claim,
    prepare_merge,
    source_admissible,
    source_dependency,
)
from agent_core.domain.reconsolidation_operations import StoredOperation, StoredSummary
from agent_core.domain.reconsolidation_summary import SummaryClause, SummaryMemory, prepare_summary
from agent_core.domain.reconsolidation_views import OperationContent, OperationSource, OperationView
from agent_core.ports.events import EventRepository
from agent_core.ports.memory import MemoryStore
from agent_core.ports.people import PeopleStore


class ReconsolidationEvidence:
    """Call while holding the memory owner guard followed by the People lock.

    This is a preview, not a commit token. A future commit must resolve all
    evidence again and apply persisted membership and rejection blocks.
    """

    def __init__(self, events: EventRepository, people: PeopleStore, memories: MemoryStore) -> None:
        self.events = events
        self.people = people
        self.memories = memories

    async def original_input(
        self,
        principal: Principal,
        group: ReconsolidationGroup,
        originals: tuple[tuple[MemoryRecord, SourceVersion], ...],
        now: datetime,
    ) -> ReconsolidationInput | None:
        snapshots = await self.sources(principal, originals)
        if snapshots is None or any(not source_admissible(s, principal, now) for s in snapshots):
            return None
        excerpts: list[OriginalExcerpt] = []
        leaves = sorted(
            {
                (record.source_session_id, sequence)
                for record, _ in originals
                for sequence in record.source_event_ids
            }
        )
        for session_id, sequence in leaves:
            try:
                events = await self.events.list_after(session_id, sequence - 1, principal, limit=1)
            except NotFoundError:
                return None
            if (
                not events
                or events[0].sequence != sequence
                or not _owner_event(events[0], principal)
            ):
                return None
            event = events[0]
            content = event.payload["content"]
            parts = [content] if isinstance(content, str) else [part["text"] for part in content]
            if len(parts) > 32 or len(excerpts) + len(parts) > 256:
                return None
            # Retain whole string/text-part boundaries, including whitespace.
            # Do not cut a clause or silently discard another part of its event.
            try:
                if any(len(part.encode("utf-8")) > 2048 for part in parts):
                    return None
            except UnicodeError:
                return None
            event_id = source_id(principal, session_id, sequence)
            excerpts.extend(
                OriginalExcerpt(
                    id=uuid5(event_id, f"reconsolidation-excerpt@1:{index}"),
                    event_id=event_id,
                    session_id=session_id,
                    event_sequence=sequence,
                    part_index=index,
                    part_count=len(parts),
                    occurred_at=event.created_at,
                    text=part,
                )
                for index, part in enumerate(parts)
            )
        if any(
            sum(
                excerpt.session_id == source.record.source_session_id
                and excerpt.event_sequence in source.record.source_event_ids
                for excerpt in excerpts
            )
            > 32
            for source in snapshots
        ):
            return None
        return ReconsolidationInput(
            group=group, sources=snapshots, excerpts=tuple(excerpts)
        ).model_copy(deep=True)

    async def plan(
        self,
        principal: Principal,
        originals: tuple[tuple[MemoryRecord, SourceVersion], ...],
        now: datetime,
        *,
        known_at: datetime | None = None,
    ) -> MergePlan | None:
        snapshots = await self.sources(principal, originals, known_at=known_at)
        if snapshots is None:
            return None
        return prepare_merge(
            principal,
            tuple(version for _, version in originals),
            snapshots,
            now=now,
            blocked_pairs=frozenset(),
            active_members=frozenset(),
        )

    async def sources(
        self,
        principal: Principal,
        originals: tuple[tuple[MemoryRecord, SourceVersion], ...],
        *,
        known_at: datetime | None = None,
    ) -> tuple[MergeSource, ...] | None:
        if not 2 <= len(originals) <= 32:
            return None
        leaves = {
            (record.source_session_id, sequence)
            for record, _ in originals
            for sequence in record.source_event_ids
        }
        if not leaves or len(leaves) > 256:
            return None
        evidence_times: dict[tuple[UUID, int], datetime] = {}
        for session_id, sequence in sorted(leaves):
            if sequence < 1:
                return None
            try:
                events = await self.events.list_after(session_id, sequence - 1, principal, limit=1)
            except NotFoundError:
                return None
            if (
                not events
                or events[0].sequence != sequence
                or not _owner_event(events[0], principal)
            ):
                return None
            evidence_times[session_id, sequence] = events[0].created_at
            if await self.people.source_suppressed(
                principal, source_id(principal, session_id, sequence)
            ):
                return None
        rejections = await self.memories.outstanding_rejections(
            principal.tenant_id, principal.principal_id
        )
        # Same casefold/whitespace comparison as the formation duplicate resolver;
        # subject, belief ID and model identity cannot bypass an owner rejection.
        rejected_texts = {
            " ".join(item.statement.casefold().split())
            for item in rejections
            if item.statement is not None
        }
        rejected_hashes = {item.statement_sha256 for item in rejections}
        snapshots: list[MergeSource] = []
        for record, version in originals:
            attribution: set[tuple[AttributionRole, str]] = {("speaker", "owner")}
            identities = tuple(
                source_id(principal, record.source_session_id, n)
                for n in sorted(set(record.source_event_ids))
            )
            records = await self.people.memory_attribution_records(principal, record.id, identities)
            if records is None:
                return None
            for item in records:
                if known_at is not None:
                    # Current heads certify privacy; historical revisions supply
                    # attribution. Unavailable or moved assignments abstain.
                    if item.created_at > known_at:
                        continue
                    historical = await self.people.get(
                        principal, item.id, ceiling=record.sensitivity, known_at=known_at
                    )
                    if historical is None:
                        return None
                    item = historical
                if await self.people.get(principal, item.id, ceiling=record.sensitivity) is None:
                    return None
                if isinstance(item, PeopleSource):
                    if (
                        item.excluded
                        or item.source_kind != "owner"
                        or item.session_id != record.source_session_id
                        or item.event_sequence not in record.source_event_ids
                        or item.id != source_id(principal, item.session_id, item.event_sequence)
                    ):
                        return None
                    continue
                if not isinstance(item, (PersonMemoryLink, PersonMention)):
                    return None
                if item.person_id is None or (
                    isinstance(item, PersonMemoryLink) and item.unresolved
                ):
                    return None
                if not item.support_ids or not set(item.support_ids).issubset(identities):
                    return None
                person = await self.people.get(
                    principal, item.person_id, ceiling=record.sensitivity, known_at=known_at
                )
                if not isinstance(person, Person) or person.state == "merged":
                    return None
                # An owner event does not authorize assigning its speaker to a
                # third party; ambiguous or contradictory attribution abstains.
                if item.role == "speaker":
                    return None
                attribution.add((item.role, str(person.id)))
            if len(attribution) > 64:
                return None
            normalized = " ".join(record.statement.casefold().split())
            rejected = (
                normalized in rejected_texts
                or hashlib.sha256(normalized.encode()).hexdigest() in rejected_hashes
            )
            snapshots.append(
                MergeSource(
                    record=record,
                    original_evidence_at=max(
                        evidence_times[record.source_session_id, n] for n in record.source_event_ids
                    ),
                    version=version,
                    attribution=tuple(sorted(attribution)),
                    fenced=False,
                    rejected=rejected,
                )
            )
        return tuple(snapshots)

    async def claim_rejected(self, principal: Principal, statements: tuple[str, ...]) -> bool:
        rejections = await self.memories.outstanding_rejections(
            principal.tenant_id, principal.principal_id
        )
        normalized = {" ".join(text.casefold().split()) for text in statements}
        hashes = {hashlib.sha256(text.encode()).hexdigest() for text in normalized}
        hashes.update(hashlib.sha256(text.casefold().encode()).hexdigest() for text in statements)
        return any(
            item.statement_sha256 in hashes
            or (
                item.statement is not None
                and " ".join(item.statement.casefold().split()) in normalized
            )
            for item in rejections
        )

    async def historical(
        self, principal: Principal, plan: MergePlan, as_of: datetime, known_at: datetime | None
    ) -> bool:
        """Recheck all retained originals without changing current operation state."""
        async with self.people.lock(principal):
            originals = []
            for dependency in plan.dependencies:
                try:
                    record = (
                        await self.memories.get(dependency.source.belief_id, principal)
                        if known_at is None
                        else await self.memories.get_at(
                            dependency.source.belief_id, principal, known_at=known_at
                        )
                    )
                except NotFoundError:
                    return False
                originals.append((record, dependency.source))
            return await self.plan(principal, tuple(originals), as_of, known_at=known_at) == plan

    async def historical_summary(
        self,
        principal: Principal,
        operation: StoredSummary,
        as_of: datetime,
        known_at: datetime | None,
        *,
        ceiling: Sensitivity,
        current_scope: str,
    ) -> SummaryMemory | None:
        """Reconstruct from exact permitted atoms; never retain or revive old text."""
        async with self.people.lock(principal):
            originals = []
            for dependency in operation.plan.dependencies:
                try:
                    current = await self.memories.get(dependency.source.belief_id, principal)
                    record = (
                        current
                        if known_at is None
                        else await self.memories.get_at(
                            dependency.source.belief_id, principal, known_at=known_at
                        )
                    )
                except NotFoundError:
                    return None
                if any(
                    SENSITIVITY_ORDER[item.sensitivity] > SENSITIVITY_ORDER[ceiling]
                    or (item.portability == Portability.LOCAL and item.scope != current_scope)
                    for item in (current, record)
                ):
                    return None
                originals.append((record, dependency.source))
            snapshots = await self.sources(principal, tuple(originals), known_at=known_at)
            if snapshots is None:
                return None
            records = {record.id: record for record, _ in originals}
            clauses = tuple(
                SummaryClause(
                    text=normalize_claim(records[ref.source_ids[0]].statement),
                    source_ids=ref.source_ids,
                )
                for ref in operation.plan.clauses
            )
            prepared = prepare_summary(
                principal,
                tuple(dep.source for dep in operation.plan.dependencies),
                snapshots,
                clauses,
                now=as_of,
            )
            if prepared is None or prepared.plan != operation.plan:
                return None
            return SummaryMemory(
                id=operation.id,
                operation_id=operation.id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                content=prepared,
                created_at=operation.committed_at,
                store_position=operation.store_position,
            )

    async def operation_view(
        self,
        principal: Principal,
        operation: StoredOperation,
        now: datetime,
        *,
        versions_match: bool,
        projection: SummaryMemory | None = None,
    ) -> tuple[OperationView, Sensitivity]:
        """Unavailable support has no safe lower ceiling, titles, links or counts."""
        view = OperationView(
            id=operation.id,
            kind=operation.kind,
            state=(
                "proposed"
                if operation.state == "committed"
                and operation.owner_review is not None
                and operation.owner_review.decision == "pending"
                else "rejected"
                if operation.owner_review is not None
                and operation.owner_review.decision == "rejected"
                else operation.state
            ),
            revision=operation.revision,
            reason=operation.reason,
            policy=operation.policy,
            model_identity=operation.model_identity,
            created_at=operation.created_at,
            committed_at=operation.committed_at,
            invalidated_at=operation.invalidated_at,
            undone_at=operation.undone_at if operation.kind == "merge" else None,
        )
        removed = isinstance(operation, StoredSummary) and operation.owner_removed is not None
        if not versions_match or (operation.state == "invalidated" and not removed):
            return view, Sensitivity.RESTRICTED
        async with self.people.lock(principal):
            originals = []
            for dep in operation.plan.dependencies:
                try:
                    originals.append(
                        (await self.memories.get(dep.source.belief_id, principal), dep.source)
                    )
                except NotFoundError:
                    return view, Sensitivity.RESTRICTED
            records = {record.id: record for record, _ in originals}
            if operation.kind == "merge":
                if await self.plan(principal, tuple(originals), now) != operation.plan:
                    return view, Sensitivity.RESTRICTED
                canonical = records[operation.plan.canonical_id]
                content = OperationContent(
                    memory_id=canonical.id, subject=canonical.subject, statement=canonical.statement
                )
                omitted: tuple[UUID, ...] = ()
            elif operation.kind == "conflict" or removed:
                snapshots = await self.sources(principal, tuple(originals))
                if (
                    snapshots is None
                    or any(not source_admissible(item, principal, now) for item in snapshots)
                    or tuple(source_dependency(item) for item in snapshots)
                    != operation.plan.dependencies
                ):
                    return view, Sensitivity.RESTRICTED
                content = OperationContent(
                    memory_id=operation.id,
                    subject="Possible conflict",
                    statement="These memories may conflict. Neither has been selected as true.",
                )
                omitted = ()
            else:
                summary = (
                    projection
                    if operation.kind == "hypothesis"
                    else await self.historical_summary(
                        principal,
                        operation,
                        now,
                        None,
                        ceiling=Sensitivity.RESTRICTED,
                        current_scope=originals[0][0].scope,
                    )
                )
                if summary is None:
                    return view, Sensitivity.RESTRICTED
                content = OperationContent(
                    memory_id=operation.id,
                    subject=summary.content.subject,
                    statement=summary.content.rendered,
                    clauses=summary.content.clauses,
                )
                omitted = operation.plan.omitted_source_ids
            sources = tuple(
                OperationSource(
                    belief_id=dep.source.belief_id,
                    content_revision=dep.source.content_revision,
                    subject=records[dep.source.belief_id].subject,
                    statement=records[dep.source.belief_id].statement,
                    session_id=dep.source_session_id,
                    event_ids=dep.source_event_ids,
                    omitted=dep.source.belief_id in omitted,
                )
                for dep in operation.plan.dependencies
            )
            return view.model_copy(
                update={
                    "content": None if removed else content,
                    "sources": () if removed else sources,
                }
            ), max((r.sensitivity for r, _ in originals), key=SENSITIVITY_ORDER.__getitem__)


def _owner_event(event: EventEnvelope, principal: Principal) -> bool:
    content = event.payload.get("content")
    text_present = isinstance(content, str) and bool(content.strip())
    if isinstance(content, list):
        text_present = bool(content) and all(
            isinstance(part, dict)
            and part.get("kind") == "text"
            and isinstance(part.get("text"), str)
            and bool(part["text"].strip())
            for part in content
        )
    # Only the retained authenticated owner message lane is currently resolvable.
    # Schedules, external input, tools and assistant prose never imply an owner.
    return (
        event.event_type == "user.message.created"
        and event.actor_type == "principal"
        and event.actor_id == principal.principal_id
        and event.payload.get("trust", "user") == "user"
        and text_present
    )
