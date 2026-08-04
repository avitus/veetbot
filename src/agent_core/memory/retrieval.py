"""Deterministic query formation, lexical recall, ranking, and faithful traces."""

from __future__ import annotations

import hashlib
import html
import math
import re
from collections import defaultdict
from datetime import timedelta
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.context import WorkingState
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    BeliefType,
    EpisodeQuery,
    MemoryAuthority,
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
)
from agent_core.domain.runs import Run
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory

RETRIEVAL_POLICY_VERSION = "retrieval@1"
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
                current_scope=self._scope,
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
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._ranker = ranker or HandWeightedRanker()

    async def recall(
        self,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
    ) -> RecallResult:
        if query.tenant_id != self._principal.tenant_id or (
            query.principal_id != self._principal.principal_id
        ):
            # Isolation is fail-closed before reaching an adapter query.
            records: list[MemoryRecord] = []
        else:
            async with self._uow_factory() as uow:
                records = await uow.memories.query(query)
        recalled = [candidate for record in records if (candidate := _score(record, query))]
        recalled = _rrf_fuse(recalled)
        ranked = self._ranker.rank(recalled, query)
        collapsed: list[RecalledBelief] = []
        subjects: defaultdict[str, int] = defaultdict(int)
        seen: set[tuple[str, str, str]] = set()
        for item in ranked:
            key = (item.subject.casefold(), item.belief_type.value, item.statement.casefold())
            if key in seen or subjects[item.subject.casefold()] >= 3:
                continue
            seen.add(key)
            subjects[item.subject.casefold()] += 1
            collapsed.append(item)
        selected: list[RecalledBelief] = []
        dropped: list[UUID] = []
        used_tokens = 0
        for item in collapsed:
            estimate = _token_estimate(_line(item))
            if len(selected) >= query.max_items or used_tokens + estimate > query.budget_tokens:
                dropped.append(item.belief_id)
                continue
            selected.append(item)
            used_tokens += estimate
        rendered = render_memory(selected, as_of=query.as_of or self._clock.now())
        rendered_bytes = rendered.encode("utf-8")
        trace_id = self._ids.new_id()
        trace = RecallTrace(
            id=trace_id,
            tenant_id=query.tenant_id,
            principal_id=query.principal_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            moment=RecallMoment(moment),
            query=query,
            surface_id=surface_id,
            sensitivity_ceiling=query.sensitivity_ceiling,
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
            operator_fields_expire_at=self._clock.now() + timedelta(days=30),
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
            tokens=_token_estimate(rendered),
            truncated=bool(dropped),
            trace_id=trace_id,
            watermark=max((record.store_position for record in records), default=0),
        )

    async def snapshot(
        self,
        *,
        session_id: UUID,
        current_scope: str,
        max_items: int = 40,
        budget_tokens: int = 1_500,
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
                sensitivity_ceiling=Sensitivity.RESTRICTED,
            ),
            session_id=session_id,
            moment=RecallMoment.SNAPSHOT.value,
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
        async with self._uow_factory() as uow:
            events = await uow.events.list_after(query.session_id, 0, self._principal)
        result = []
        needle = (query.text or "").casefold()
        for event in events:
            if query.since is not None and event.created_at < query.since:
                continue
            if query.until is not None and event.created_at >= query.until:
                continue
            if needle and needle not in str(event.payload).casefold():
                continue
            result.append(event)
            if len(result) >= query.limit:
                break
        return result


def _score(record: MemoryRecord, query: RecallQuery) -> RecalledBelief | None:
    text_terms = _terms(query.text or "")
    subject_terms = {subject.casefold() for subject in query.subjects}
    record_text = f"{record.subject} {record.statement}".casefold()
    lexical = (
        sum(1 for term in text_terms if term in record_text) / len(text_terms) if text_terms else 0
    )
    structured = 1.0 if record.subject.casefold() in subject_terms else 0
    arms = []
    if structured:
        arms.append("structured")
    if lexical:
        arms.append("lexical")
    if query.profile is RecallProfile.CORE:
        match = (
            0.5
            if record.belief_type
            in {
                BeliefType.PREFERENCE,
                BeliefType.USER_MODEL_ATTR,
            }
            or record.scope == "user"
            else 0.15
        )
        arms = ["structured"]
    else:
        match = max(structured, lexical)
        if match == 0:
            return None
    lifecycle = 1.0 if record.status is MemoryStatus.ACTIVE else 0.4
    confidence = record.confidence * lifecycle
    reinforce = min(1.0, math.log1p(record.corroboration_count) / math.log(11))
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
    penalty = 0.2 if record.flagged_for_review else 0
    score = min(
        1.0,
        0.4 * match
        + 0.2 * confidence
        + 0.1 * reinforce
        + 0.15 * authority
        + 0.1 * scope
        + 0.05 * max(0, record.utility)
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
    conflict = (
        f" conflicts={','.join(str(value) for value in sorted(item.conflict_with))}"
        if item.conflict_with
        else ""
    )
    return (
        f"[m:{str(item.belief_id)[:8]}]{origin} {html.escape(item.statement)} "
        f"({item.authority.value}, {item.confidence_band}){conflict}"
    )


def _terms(text: str) -> set[str]:
    return {
        part.strip(".,:;!?()[]{}\"'")
        for part in text.casefold().split()
        if len(part.strip(".,:;!?()[]{}\"'")) >= 3
    }


def _entities(text: str) -> set[str]:
    return {match.group(0) for match in re.finditer(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", text)}


def _token_estimate(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)
