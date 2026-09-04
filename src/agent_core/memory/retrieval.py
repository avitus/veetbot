"""Deterministic query formation, lexical recall, ranking, and faithful traces."""

from __future__ import annotations

import hashlib
import html
import math
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.context import WorkingState
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    BeliefType,
    EpisodeQuery,
    MemoryAuthority,
    MemoryCorrection,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RecalledBelief,
    RecallMoment,
    RecallProfile,
    RecallQuery,
    RecallResult,
    RecallTrace,
    Sensitivity,
    lexical_query_terms,
    lexical_term_lexemes,
    lexical_tokens,
)
from agent_core.domain.runs import Run
from agent_core.memory.profiles import (
    DEFAULT_RETRIEVAL_PROFILE,
    DEFAULT_TRACE_PROFILE,
    RetrievalProfile,
    TraceProfile,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory

RETRIEVAL_POLICY_VERSION = "retrieval@3"
# Episode search reads the session stream in bounded pages: never the whole
# stream at once, and never more pages than a bounded read is worth.
EPISODE_PAGE_MINIMUM = 256
EPISODE_MAX_PAGES = 64
# Two statements about one subject that share this much of their token set are
# the same belief said twice, and the second one is demoted rather than lost.
NEAR_DUPLICATE_SIMILARITY = 0.8
_DURABLE_TYPES = frozenset({BeliefType.PREFERENCE, BeliefType.USER_MODEL_ATTR})
_STALE_STATUSES = frozenset({MemoryStatus.EXPIRED, MemoryStatus.RETIRED})
# The three ways a belief stops holding without being deleted, and therefore
# the three a snapshot member can need a correction line for.
_CORRECTED_STATUSES = frozenset(
    {MemoryStatus.SUPERSEDED, MemoryStatus.EXPIRED, MemoryStatus.RETIRED}
)
_INJECTION = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|"
    r"<\s*/?\s*(?:system|memory|untrusted)|override\s+(?:policy|instructions))",
    re.I,
)


class DeterministicQueryFormer:
    def __init__(
        self,
        principal: Principal,
        *,
        current_scope: str = "general",
        budget_tokens: int = 2_000,
        max_items: int = 20,
        min_score: float = 0.12,
    ) -> None:
        self._principal = principal
        self._scope = current_scope
        self._budget_tokens = budget_tokens
        self._max_items = max_items
        self._min_score = min_score

    def form(
        self,
        run: Run,
        working_state: WorkingState,
        message: str | None,
        *,
        current_scope: str | None = None,
    ) -> list[RecallQuery]:
        del run
        fragments = [working_state.objective or "", *working_state.open_questions, message or ""]
        text = " ".join(fragment.strip() for fragment in fragments if fragment.strip())
        subjects = sorted(_entities(text))
        if not text and not subjects:
            return []
        return [
            RecallQuery(
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                # The turn's session names the project; the scope the former
                # was constructed with is the default a caller may override.
                current_scope=current_scope or self._scope,
                text=text or None,
                subjects=subjects,
                profile=RecallProfile.TASK,
                budget_tokens=self._budget_tokens,
                max_items=self._max_items,
                min_score=self._min_score,
            )
        ]


class HandWeightedRanker:
    def rank(self, candidates: list[RecalledBelief], query: RecallQuery) -> list[RecalledBelief]:
        del query
        return sorted(candidates, key=lambda item: (-item.score, str(item.belief_id)))


class HybridMemoryRetriever:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        *,
        ranker: HandWeightedRanker | None = None,
        profile: RetrievalProfile = DEFAULT_RETRIEVAL_PROFILE,
        trace_retention: TraceProfile = DEFAULT_TRACE_PROFILE,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._ranker = ranker or HandWeightedRanker()
        self._profile = profile
        self._trace_retention = trace_retention

    @property
    def retrieval_profile(self) -> RetrievalProfile:
        """Expose the ranking profile the composition wired in."""

        return self._profile

    async def recall(
        self,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
        measure_rendered_tokens: Callable[[str], int] | None = None,
    ) -> RecallResult:
        authorized = query.tenant_id == self._principal.tenant_id and (
            query.principal_id == self._principal.principal_id
        )
        effective_query = (
            query
            if authorized
            else query.model_copy(
                update={
                    "tenant_id": self._principal.tenant_id,
                    "principal_id": self._principal.principal_id,
                }
            )
        )
        if not authorized:
            # Isolation is fail-closed before reaching an adapter query.
            records: list[MemoryRecord] = []
            head = 0
        else:
            async with self._uow_factory() as uow:
                records = await uow.memories.query(query)
                # The watermark is the store's own head, read in the same unit
                # of work as the query: a belief this query did not match still
                # occupies a position, and calling the highest position the
                # recall returned the watermark would make every belief above
                # it look new to the next turn's delta.
                head = await uow.memories.head_position(self._principal)
        # One instant governs the whole recall: time decay, the stale penalty,
        # and the rendered stamp all read the query's as-of or the clock, so a
        # historical query is scored as of the moment it asks about.
        now = effective_query.as_of or self._clock.now()
        recalled: list[RecalledBelief] = []
        durable_ids: set[UUID] = set()
        excluded = set(effective_query.exclude_ids)
        for record in records:
            # A hard predicate, not a score: the persona row already carries
            # these beliefs at higher trust (persona-surface.md).
            if record.id in excluded:
                continue
            candidate = _score(record, effective_query, now=now, profile=self._profile)
            if candidate is None:
                continue
            if _is_durable(record):
                durable_ids.add(record.id)
            recalled.append(candidate)
        recalled = _rrf_fuse(recalled, k=self._profile.reciprocal_rank_fusion_k)
        recalled = _penalize_near_duplicates(recalled, penalty=self._profile.near_duplicate_penalty)
        ranked = self._ranker.rank(recalled, effective_query)
        collapsed: list[RecalledBelief] = []
        subjects: defaultdict[str, int] = defaultdict(int)
        seen: set[tuple[str, str, str]] = set()
        chosen: set[UUID] = set()
        partners: set[UUID] = set()
        for item in ranked:
            key = (item.subject.casefold(), item.belief_type.value, item.statement.casefold())
            if key in seen:
                continue
            # A conflict means nothing with one half missing, and the per-subject
            # cap would cut exactly that half: two beliefs in conflict share a
            # subject by construction. The link is read in both directions so
            # the partner is kept whichever of the two the ranking preferred.
            partner = item.belief_id in partners or not chosen.isdisjoint(item.conflict_with)
            if subjects[item.subject.casefold()] >= 3 and not partner:
                continue
            seen.add(key)
            subjects[item.subject.casefold()] += 1
            chosen.add(item.belief_id)
            partners.update(item.conflict_with)
            collapsed.append(item)
        selected: list[RecalledBelief] = []
        dropped: list[UUID] = []
        measure_tokens = measure_rendered_tokens or _token_estimate
        reserve = _durable_reserve(
            effective_query,
            collapsed,
            durable_ids,
            share=self._profile.durable_item_share,
        )
        durable_ahead = _durable_ahead(collapsed, durable_ids)
        durable_selected = 0
        for index, item in enumerate(collapsed):
            durable = item.belief_id in durable_ids
            # A slot is held for a durable belief further down the ranking only
            # while one is actually still pending, so the reservation never
            # shrinks a snapshot that has no durable belief left to seat.
            held = 0 if durable else min(max(reserve - durable_selected, 0), durable_ahead[index])
            candidate_tokens = measure_tokens(render_memory([*selected, item], as_of=now))
            if (
                len(selected) + held >= effective_query.max_items
                or candidate_tokens > effective_query.budget_tokens
            ):
                dropped.append(item.belief_id)
                continue
            selected.append(item)
            durable_selected += int(durable)
        rendered = render_memory(selected, as_of=now)
        rendered_tokens = measure_tokens(rendered) if selected else 0
        rendered_bytes = rendered.encode("utf-8")
        trace_id = self._ids.new_id()
        trace = RecallTrace(
            id=trace_id,
            tenant_id=self._principal.tenant_id,
            principal_id=self._principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            moment=RecallMoment(moment),
            query=effective_query,
            surface_id=surface_id,
            sensitivity_ceiling=effective_query.sensitivity_ceiling,
            rendered=rendered,
            rendered_sha256=hashlib.sha256(rendered_bytes).hexdigest(),
            arm_latencies_ms={"structured": 0, "lexical": 0},
            candidates=len(records),
            returned=[item.belief_id for item in selected],
            dropped_for_budget=dropped,
            blocked=[item.belief_id for item in selected if item.blocked],
            carried_in=[item.belief_id for item in selected if item.carried],
            beliefs=[item.model_copy(deep=True) for item in selected],
            retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
            created_at=self._clock.now(),
            operator_fields_expire_at=(
                self._clock.now() + timedelta(days=self._trace_retention.operator_retention_days)
            ),
        )
        async with self._uow_factory() as uow:
            await uow.traces.record(trace)
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="memory.recalled",
                    actor_type="memory",
                    payload={
                        "trace_id": str(trace_id),
                        "rendered_sha256": trace.rendered_sha256,
                        "returned": [str(item.belief_id) for item in selected],
                    },
                )
            )
        return RecallResult(
            items=selected,
            rendered=rendered,
            tokens=rendered_tokens,
            truncated=bool(dropped),
            trace_id=trace_id,
            watermark=head,
        )

    async def corrections(
        self,
        *,
        snapshot_id: UUID,
        watermark: int,
        as_of: datetime | None = None,
    ) -> list[MemoryCorrection]:
        """List the snapshot's own beliefs that stopped holding after it froze.

        The snapshot is rendered inside the cached prefix and is never
        rewritten, so a belief it captured goes on being stated until the
        session ends. The correction is what overrides it, and only a belief
        the snapshot actually returned can need one: a closure elsewhere in the
        store is news the delta carries, not a correction to anything the turn
        is being told.

        Closure is read from the store position rather than from the instant,
        because the position is the same ordering the delta is bounded by: a
        belief superseded, expired, or retired at a position past the watermark
        is exactly a change this session has not seen. A snapshot trace that
        has expired or was never recorded yields nothing, which leaves the turn
        with its base recall rather than failing it.
        """

        async with self._uow_factory() as uow:
            try:
                trace = await uow.traces.get(snapshot_id, self._principal)
            except NotFoundError:
                return []
            returned = set(trace.returned)
            if not returned:
                return []
            records: list[MemoryRecord] = []
            for belief_id in sorted(returned, key=str):
                try:
                    records.append(await uow.memories.get(belief_id, self._principal))
                except NotFoundError:
                    continue
        instant = as_of or self._clock.now()
        corrections = [
            MemoryCorrection(
                belief_id=record.id,
                replacement_id=record.superseded_by,
                ended_at=record.valid_to or record.updated_at,
            )
            for record in records
            if record.id in returned
            and record.status in _CORRECTED_STATUSES
            and record.store_position > watermark
            and (record.valid_to or record.updated_at) <= instant
        ]
        return sorted(corrections, key=lambda item: str(item.belief_id))

    async def snapshot(
        self,
        *,
        session_id: UUID,
        current_scope: str,
        max_items: int = 40,
        budget_tokens: int = 1_500,
        sensitivity_ceiling: Sensitivity = Sensitivity.RESTRICTED,
        surface_id: str = "private",
    ) -> RecallResult:
        return await self.recall(
            RecallQuery(
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                current_scope=current_scope,
                profile=RecallProfile.CORE,
                budget_tokens=budget_tokens,
                max_items=max_items,
                min_score=0.1,
                sensitivity_ceiling=sensitivity_ceiling,
            ),
            session_id=session_id,
            moment=RecallMoment.SNAPSHOT.value,
            surface_id=surface_id,
        )


class EventEpisodeSearch:
    def __init__(self, uow_factory: UnitOfWorkFactory, principal: Principal) -> None:
        self._uow_factory = uow_factory
        self._principal = principal

    async def search(self, query: EpisodeQuery) -> list[EventEnvelope]:
        if query.tenant_id != self._principal.tenant_id or (
            query.principal_id != self._principal.principal_id
        ):
            return []
        # The text predicate is applied to explicit payload fields below, so a
        # match can sit anywhere in the stream. The stream is read in bounded
        # pages rather than whole: the cursor walks the event sequence until
        # the caller's limit is met, a short page proves the stream is spent,
        # or the page budget runs out.
        result: list[EventEnvelope] = []
        needle = (query.text or "").casefold()
        page = max(query.limit * 8, EPISODE_PAGE_MINIMUM)
        cursor = 0
        for _ in range(EPISODE_MAX_PAGES):
            async with self._uow_factory() as uow:
                events = await uow.events.list_after(
                    query.session_id,
                    cursor,
                    self._principal,
                    created_at_or_after=query.since,
                    created_before=query.until,
                    limit=page,
                )
            for event in events:
                if needle and not _payload_contains(event.payload, needle):
                    continue
                result.append(event)
                if len(result) >= query.limit:
                    return result
            if len(events) < page:
                break
            cursor = events[-1].sequence
        return result


_EPISODE_TEXT_FIELDS = frozenset({"content", "question", "statement", "summary", "text", "title"})


def _payload_contains(payload: object, needle: str, *, field: str | None = None) -> bool:
    if isinstance(payload, dict):
        return any(
            _payload_contains(value, needle, field=str(key)) for key, value in payload.items()
        )
    if isinstance(payload, list):
        return any(_payload_contains(value, needle, field=field) for value in payload)
    return (
        field in _EPISODE_TEXT_FIELDS and isinstance(payload, str) and needle in payload.casefold()
    )


def _is_durable(record: MemoryRecord) -> bool:
    """Name the beliefs the snapshot reserves its durable share for."""

    return record.belief_type in _DURABLE_TYPES or record.scope == "user"


def _durable_reserve(
    query: RecallQuery,
    candidates: list[RecalledBelief],
    durable_ids: set[UUID],
    *,
    share: float,
) -> int:
    """Reserve the CORE snapshot's durable share, bounded by what is available."""

    if query.profile is not RecallProfile.CORE:
        return 0
    available = sum(1 for item in candidates if item.belief_id in durable_ids)
    return min(math.ceil(share * query.max_items), query.max_items, available)


def _durable_ahead(candidates: list[RecalledBelief], durable_ids: set[UUID]) -> list[int]:
    """Count the durable beliefs at or after each position in the ranking."""

    ahead = [0] * (len(candidates) + 1)
    for index in range(len(candidates) - 1, -1, -1):
        durable = candidates[index].belief_id in durable_ids
        ahead[index] = ahead[index + 1] + int(durable)
    return ahead[:-1]


def _score(
    record: MemoryRecord,
    query: RecallQuery,
    *,
    now: datetime | None = None,
    profile: RetrievalProfile = DEFAULT_RETRIEVAL_PROFILE,
) -> RecalledBelief | None:
    """Score one candidate against one query, as of `now`.

    The lexical arm counts whole lexemes over the tokenizer both belief stores
    filter with, so a term the store would not match cannot score here either.
    Evidence decays with the time since the belief was last supported, at
    the time constant its belief type carries: a stable preference fades far
    more slowly than a situational fact. Without `now` no time has passed,
    which is what a unit scoring a record against a query measures.

    The stale penalty demotes expired and retired rows. The hard filter keeps
    them out of an ordinary recall entirely, so the penalty is reachable only
    through an as-of or include-superseded query, where history is the point
    and the ranking still has to say which rows are no longer current.
    """

    terms = lexical_query_terms(query.text)
    subject_terms = {subject.casefold() for subject in query.subjects}
    record_tokens = lexical_tokens(f"{record.subject} {record.statement}")
    lexical = (
        sum(1 for lexemes in lexical_term_lexemes(terms) if lexemes and lexemes <= record_tokens)
        / len(terms)
        if terms
        else 0
    )
    structured = 1.0 if record.subject.casefold() in subject_terms else 0
    arms = []
    if structured:
        arms.append("structured")
    if lexical:
        arms.append("lexical")
    if query.profile is RecallProfile.CORE:
        match = 0.5 if _is_durable(record) else 0.15
        arms = ["structured"]
    else:
        match = max(structured, lexical)
        if match == 0:
            return None
    weights = profile.lifecycle_weights
    lifecycle = weights.active if record.status is MemoryStatus.ACTIVE else weights.provisional
    confidence = record.confidence * lifecycle
    age_days = max(0, (now - record.last_evidence_at).days) if now is not None else 0
    tau_days = profile.decay_tau_days.for_belief_type(record.belief_type)
    reinforce = min(1.0, math.log1p(record.evidence_count) / math.log(11)) * math.exp(
        -age_days / tau_days
    )
    authority = {
        MemoryAuthority.USER: 1.0,
        MemoryAuthority.AFFIRMED: 0.7,
        MemoryAuthority.INFERRED: 0.4,
    }[record.authority]
    carried = record.scope not in {query.current_scope, "user", "global"}
    scope = 1.0
    if carried:
        scope = {
            Portability.PORTABLE: 0.8,
            Portability.CONTEXTUAL: 0.55,
            Portability.LOCAL: 0.15,
        }[record.portability]
    stale = record.status in _STALE_STATUSES or (
        now is not None and record.expires_at is not None and record.expires_at <= now
    )
    penalty = (0.2 if record.flagged_for_review else 0) + (profile.stale_penalty if stale else 0)
    ranking = profile.ranking_weights
    score = min(
        1.0,
        ranking.match * match
        + ranking.confidence * confidence
        + ranking.reinforce * reinforce
        + ranking.authority * authority
        + ranking.scope * scope
        + ranking.utility * max(0, record.utility)
        - penalty,
    )
    if score < query.min_score:
        return None
    blocked = _INJECTION.search(record.statement) is not None
    statement = "[BLOCKED]" if blocked else record.statement
    band = "high" if record.confidence >= 0.8 else "medium" if record.confidence >= 0.55 else "low"
    if carried:
        band = "medium" if band == "high" else "low"
    return RecalledBelief(
        belief_id=record.id,
        subject=record.subject,
        statement=statement,
        belief_type=record.belief_type,
        claim_kind=record.claim_kind,
        derivation=record.derivation,
        longevity=record.longevity,
        status=record.status,
        confidence_band=band,
        authority=record.authority,
        origin_scope=record.origin_scopes[0],
        portability=record.portability,
        sensitivity=record.sensitivity,
        carried=carried,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
        score=score,
        arms=arms,
        conflict_with=list(record.conflicts_with),
        blocked=blocked,
        source_event_ids=list(record.source_event_ids),
    )


def _penalize_near_duplicates(
    candidates: list[RecalledBelief], *, penalty: float
) -> list[RecalledBelief]:
    """Demote a restatement of a belief a higher-scored candidate already made.

    Applied between fusion and ranking, so the demotion is measured against
    fused scores. The exact-statement collapse further down still drops a
    verbatim repeat; this one keeps the second phrasing, because a genuine
    second fact about the same subject reads as a near-duplicate of the first
    often enough that deleting it loses real beliefs.
    """

    ordered = sorted(candidates, key=lambda item: (-item.score, str(item.belief_id)))
    seen: defaultdict[tuple[str, BeliefType], list[frozenset[str]]] = defaultdict(list)
    penalized: list[RecalledBelief] = []
    for item in ordered:
        key = (item.subject.casefold(), item.belief_type)
        tokens = frozenset(lexical_tokens(item.statement))
        duplicate = any(_jaccard(other, tokens) >= NEAR_DUPLICATE_SIMILARITY for other in seen[key])
        seen[key].append(tokens)
        penalized.append(
            item.model_copy(update={"score": max(0.0, item.score - penalty)}) if duplicate else item
        )
    return penalized


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Token-set similarity, zero when neither statement yields a token."""

    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _rrf_fuse(candidates: list[RecalledBelief], *, k: int = 60) -> list[RecalledBelief]:
    """Fuse independently ranked retrieval arms before the hand-weighted reranker."""

    arm_ranks: dict[tuple[str, UUID], int] = {}
    arms = sorted({arm for item in candidates for arm in item.arms})
    for arm in arms:
        members = sorted(
            (item for item in candidates if arm in item.arms),
            key=lambda item: (-item.score, str(item.belief_id)),
        )
        arm_ranks.update(
            {
                (
                    arm,
                    item.belief_id,
                ): index
                for index, item in enumerate(members, 1)
            }
        )
    ceiling = max(1, len(arms)) / (k + 1)
    fused: list[RecalledBelief] = []
    for item in candidates:
        rrf = sum(1 / (k + arm_ranks[(arm, item.belief_id)]) for arm in item.arms)
        fused.append(
            item.model_copy(update={"score": min(1.0, 0.8 * item.score + 0.2 * rrf / ceiling)})
        )
    return fused


def render_memory(items: list[RecalledBelief], *, as_of: object) -> str:
    stamp = as_of.isoformat().replace("+00:00", "Z") if hasattr(as_of, "isoformat") else str(as_of)
    lines = [f'<memory as_of="{html.escape(stamp)}" policy="{RETRIEVAL_POLICY_VERSION}">']
    for item in items:
        lines.append(f"  {_line(item)}")
    lines.append("</memory>")
    return "\n".join(lines)


def _line(item: RecalledBelief) -> str:
    origin = f" (learned in {html.escape(item.origin_scope)})" if item.carried else ""
    # A partner is named the way a citation is, so the only identifier the
    # model ever sees for a belief is the eight-digit one it can cite back.
    conflict = (
        f" conflicts=[{','.join(f'm:{str(value)[:8]}' for value in sorted(item.conflict_with))}]"
        if item.conflict_with
        else ""
    )
    return (
        f"[m:{str(item.belief_id)[:8]}]{origin} {html.escape(item.statement)} "
        f"({item.authority.value}, {item.confidence_band}; "
        f"{item.derivation.value}, {item.longevity.value}){conflict}"
    )


def _entities(text: str) -> set[str]:
    return {match.group(0) for match in re.finditer(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", text)}


def _token_estimate(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)
