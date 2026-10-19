"""Compile mechanically scored release observations from recorded runtime facts."""

import hashlib
import html
import json
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_core.domain.agents import Principal
from agent_core.domain.general_recall_release import (
    Channel,
    Digest,
    PathKind,
    Pipeline,
    RecallObservation,
    ReleaseValue,
)
from agent_core.domain.memory import (
    MemoryRecord,
    RecallProfile,
    RecallQuery,
    RecallResult,
    RecallStages,
)
from agent_core.domain.messages import ContextRecallRanking, ModelUsage
from agent_core.domain.runs import RecallWorkBudget
from agent_core.evals.general_memory_recall_driver import OWNER
from agent_core.evals.general_recall_corpus import ReaderAnswer, RecallFixture, score_answer
from agent_core.evals.general_recall_formation_bank import _Bank, _read_bank
from agent_core.evals.general_recall_stages import (
    answer_violations,
    authored_source_evidence,
    evidence_labels,
    observed_ranked_ids,
    recovered_source_witnesses,
    rendered_noise_items,
)
from agent_core.memory.general_recall_evidence import _unique_object, source_labels


class SourceEvent(ReleaseValue):
    session_id: UUID
    sequence: int = Field(gt=0)


class SearchObservation(BaseModel):
    # Development-only convenience summaries are deliberately ignored: scoring
    # derives from the actual returned originals, source witnesses and ordering.
    model_config = ConfigDict(frozen=True, extra="ignore")
    query: RecallQuery
    policy: str = Field(min_length=1, max_length=4096)
    result: RecallResult
    stages: RecallStages
    rendered_in_request: bool = Field(strict=True)


class ProviderAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    request_sha256: Digest
    completed: Literal[True]
    usage: ModelUsage


class RuntimeObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    observation_version: Literal["general-recall-runtime-observation@1"]
    scenario_id: str
    pipeline: Pipeline
    path: PathKind
    channel: Channel
    execution: Literal["runtime"]
    storage: Literal["postgres"]
    clock: Literal["elapsed"]
    runtime_status: Literal["COMPLETED"]
    observed_at: AwareDatetime
    source_events: dict[str, SourceEvent] = Field(max_length=100)
    formed_records: tuple[MemoryRecord, ...] = Field(max_length=10000)
    recall_observations: tuple[SearchObservation, ...] = Field(min_length=1, max_length=4)
    context_ranking: ContextRecallRanking | None = None
    attributed_usage: tuple[ModelUsage, ...] = Field(min_length=1, max_length=130)
    provider_attempts: tuple[ProviderAttempt, ...] = Field(min_length=1, max_length=130)
    formation_bank_sha256: Digest | None
    formation_replayed_calls: int = Field(ge=0, le=128)
    recall_work: RecallWorkBudget
    executed_searches: int = Field(ge=1, le=4)
    rendered_memory: str = Field(max_length=100000)
    answer: str = Field(max_length=100000)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(f"Recall observation: {reason}")


def compile_runtime_observation(
    case: RecallFixture,
    raw: dict[str, Any],
    *,
    identity_sha256: str,
    formation_bank: Path | None = None,
    principal: Principal = OWNER,
) -> RecallObservation:
    """Rescore evidence and answers; never accept the runner's aggregate pass fields.

    This creates one measurement, not activation evidence. Complete reviewed
    matrix coverage, exact run/configuration identities, cost journals and gate
    receipts remain the collection and publication layer's responsibilities.
    """
    observed = RuntimeObservation.model_validate(raw)
    _require(observed.scenario_id == case.id, "scenario identity changed")
    replayed: tuple[ModelUsage, ...] = ()
    if observed.formation_replayed_calls:
        _require(formation_bank is not None, "replayed formation requires its original bank")
        assert formation_bank is not None
        bank_bytes = _read_bank(formation_bank)
        _require(
            hashlib.sha256(bank_bytes).hexdigest() == observed.formation_bank_sha256,
            "formation bank changed",
        )
        bank = _Bank.model_validate_json(bank_bytes)
        replayed = tuple(call.usage for call in bank.calls)
        _require(
            observed.pipeline == "formation" and len(replayed) == observed.formation_replayed_calls,
            "replayed formation call count differs from its bank",
        )
    _require(
        observed.attributed_usage
        == (*replayed, *(attempt.usage for attempt in observed.provider_attempts)),
        "attributed usage differs from completed provider attempts and verified replay",
    )
    _require(
        set(observed.source_events) == {turn.id for turn in case.history}
        and len({(s.session_id, s.sequence) for s in observed.source_events.values()})
        == len(observed.source_events),
        "source event mapping is incomplete or duplicated",
    )
    _require(
        len({record.id for record in observed.formed_records}) == len(observed.formed_records),
        "formed records are duplicated",
    )
    _require(
        len(observed.recall_observations)
        <= observed.executed_searches
        <= observed.recall_work.searches,
        "executed searches exceed the durable reservation",
    )
    _require(
        all(usage.cost.is_finite() and usage.cost >= 0 for usage in observed.attributed_usage),
        "usage contains an unknown or negative cost",
    )
    if observed.channel != "sparse":
        relevant = [
            search
            for search in observed.recall_observations
            if search.query.profile is not RecallProfile.CORE
        ]
        _require(
            bool(relevant) and all("+semantic@" in search.policy for search in relevant),
            "semantic fallback cannot certify a dense or hybrid measurement",
        )
        if observed.channel == "dense":
            _require(
                all("+dense@" in search.policy for search in relevant),
                "dense channel was not observed",
            )
    results = []
    visible = []
    boundary = set()
    records = {record.id: record for record in observed.formed_records}
    for record in observed.formed_records:
        if (record.tenant_id, record.principal_id) != (principal.tenant_id, principal.principal_id):
            boundary.add("foreign_formed_record")
    for search in observed.recall_observations:
        result = search.result.model_copy(update={"stages": search.stages})
        _require(
            len(set(search.stages.candidate_ids)) == len(search.stages.candidate_ids)
            and len(set(search.stages.ranked_ids)) == len(search.stages.ranked_ids)
            and set(search.stages.ranked_ids) <= set(search.stages.candidate_ids)
            and {item.belief_id for item in result.items} <= set(search.stages.ranked_ids),
            "returned originals disagree with observed candidate or ranking IDs",
        )
        if (search.query.tenant_id, search.query.principal_id) != (
            principal.tenant_id,
            principal.principal_id,
        ):
            boundary.add("foreign_search_principal")
        results.append(result)
        for item in result.items:
            original = records.get(item.belief_id)
            _require(
                item.blocked
                or original is None
                or (item.subject, item.statement) == (original.subject, original.statement),
                "returned statement differs from the recorded original",
            )
            _require(
                html.escape(item.statement) in result.rendered,
                "returned item is absent from its rendered block",
            )
        if search.rendered_in_request:
            _require(bool(result.rendered), "an empty block cannot claim rendered evidence")
            visible.append(result)
    if observed.context_ranking is not None:
        by_trace = {result.trace_id: result for result in visible}
        _require(
            set(by_trace) == {block.trace_id for block in observed.context_ranking.blocks},
            "assembly ranking differs from visible search blocks",
        )
        visible = [by_trace[block.trace_id] for block in observed.context_ranking.blocks]
    _require(
        "\n".join(result.rendered for result in visible) == observed.rendered_memory,
        "rendered evidence differs from the recorded reader context",
    )
    _require(
        len({result.rendered for result in visible}) == len(visible)
        and sum(len(result.items) + len(result.people) for result in visible) <= 20,
        "rendered blocks duplicate or exceed the shared item allowance",
    )
    order = observed_ranked_ids(results, ranking=observed.context_ranking)
    _require(order is not None, "no certifiable global ranking was observed")
    assert order is not None
    sources = {
        key: (source.session_id, source.sequence) for key, source in observed.source_events.items()
    }
    labels = evidence_labels(case, observed.formed_records, sources)

    def labeled(keys: Iterable[UUID]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(label for key in keys for label in labels.get(key, ())))

    rendered = labeled(
        item.belief_id for result in visible for item in result.items if not item.blocked
    )
    witnesses = recovered_source_witnesses(
        case, results, visible, sources, ranking=observed.context_ranking
    )
    recovered = source_labels(authored_source_evidence(case), witnesses, "rendered")
    supported = set(rendered) | recovered
    useful = {
        witness.belief_id
        for witness in witnesses
        if witness.rendered
        and any(label.required and witness.source_id in label.source_ids for label in case.evidence)
    }
    foreign_sources = {turn.id for turn in case.history if turn.speaker != "owner"}
    if any(witness.source_id in foreign_sources for witness in witnesses):
        boundary.add("unauthorized_source_recovery")
    scored = score_answer(case, observed.answer)
    try:
        action = ReaderAnswer.model_validate(
            json.loads(observed.answer, object_pairs_hook=_unique_object)
        ).action
    except (ValueError, TypeError):
        action = None  # Malformed output is never silently treated as abstention.
    return RecallObservation(
        identity_sha256=identity_sha256,
        scenario_id=case.id,
        path=observed.path,
        pipeline=observed.pipeline,
        channel=observed.channel,
        observed_at=observed.observed_at,
        formed=labeled(labels),
        candidates=labeled(
            key
            for result in results
            if result.stages is not None
            for key in result.stages.candidate_ids
        ),
        ranked=labeled(order[:20]),
        rendered=rendered,
        recovered_sources=witnesses,
        global_rank_certifiable=True,
        rendered_noise_items=rendered_noise_items(
            visible,
            labels=labels,
            required={label.id for label in case.evidence if label.required},
            useful_anchors=useful,
        ),
        answer_atoms=scored.answer_atoms,
        action=action,
        unsupported_atoms=scored.unsupported_atoms,
        invalid_current_ids=tuple(
            sorted(
                set(answer_violations(case, observed.answer))
                | {
                    label.id
                    for label in case.evidence
                    if label.invalid_current and label.id in supported
                }
            )
        ),
        boundary_failures=tuple(sorted(boundary)),
        searches=observed.executed_searches,
        rounds=observed.recall_work.rounds,
        context_tokens=sum(
            max(result.tokens, (len(result.rendered.encode()) + 3) // 4) for result in visible
        ),
        input_tokens=sum(usage.input_tokens for usage in observed.attributed_usage),
        output_tokens=sum(usage.output_tokens for usage in observed.attributed_usage),
        cost_usd=float(sum((usage.cost for usage in observed.attributed_usage), Decimal(0))),
        elapsed_ms=observed.elapsed_ms,
    )
