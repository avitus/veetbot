"""Offline, label-only M26 scoring; never invokes a model or grants activation.

Private source text stays in the owner's labeling workflow. This module accepts
opaque conversation ids and owner judgments, not messages, addresses or drafts.
Synthetic inputs exercise the checker and cannot establish owner usefulness.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Relationship = Literal[
    "regular_reply_partner",
    "close_collaborator",
    "portfolio_founder",
    "prospective_investment_founder",
    "portfolio_board_member",
    "venture_investor",
    "personal",
    "new_relevant_sender",
    "bulk",
    "low_value",
]
BASELINES = ("newest_first", "gmail_important", "without_personalization")
RELATIONSHIPS = frozenset(get_args(Relationship))


class LabelModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmailQualityPolicy(LabelModel):
    ranker_version: str = Field(min_length=1, max_length=128)
    style_version: str = Field(min_length=1, max_length=128)
    semantic_policy_version: Literal["email-semantic@1"]
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    build_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EmailThreadLabel(LabelModel):
    id: UUID
    conversation_id: UUID
    account: Literal["account_1", "account_2"]
    split: Literal["training", "holdout"]
    source_at: AwareDatetime
    relationship: Relationship
    important: bool
    content_kind: Literal["actionable", "informational", "other"]
    needs_reply: bool
    unambiguous: bool
    fully_supported: bool


class EmailSnapshot(LabelModel):
    id: UUID
    observed_at: AwareDatetime
    profile_evidence_through: AwareDatetime
    profile_thread_ids: list[UUID] = Field(default_factory=list, max_length=100_000)
    complete_candidate_pool: Literal[True]
    independent_snapshot: Literal[True]
    candidate_ids: list[UUID] = Field(min_length=1, max_length=10_000)
    ranked_ids: list[UUID] = Field(min_length=1, max_length=10_000)
    displayed_ids: list[UUID] = Field(max_length=5)
    baselines: dict[str, list[UUID]]
    auto_drafted_ids: list[UUID] = Field(default_factory=list, max_length=3)


class EmailStyleComparison(LabelModel):
    id: UUID
    thread_id: UUID
    blind_paired: Literal[True]
    preferred: Literal["personalized", "baseline", "tie"]
    substantial_rewrite: bool
    factually_faithful: bool
    fabricated_commitment: bool = False
    fabricated_recipient: bool = False


class EmailMemoryJudgment(LabelModel):
    thread_id: UUID
    expected_useful: int = Field(ge=0)
    emitted: int = Field(ge=0)
    supported_useful: int = Field(ge=0)
    attribution_violations: int = Field(default=0, ge=0)
    authority_violations: int = Field(default=0, ge=0)
    useful_beyond_excerpt: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def matched_facts_are_bounded(self) -> EmailMemoryJudgment:
        if self.supported_useful > min(self.expected_useful, self.emitted):
            raise ValueError("matched useful facts cannot exceed labels or emitted facts")
        if self.useful_beyond_excerpt > self.supported_useful:
            raise ValueError("additional utility requires a supported useful fact")
        return self


class EmailQualityCorpus(LabelModel):
    schema_version: Literal[1] = 1
    origin: Literal["owner_private", "synthetic"]
    frozen_at: AwareDatetime
    tuning_started_at: AwareDatetime
    holdout_started_at: AwareDatetime
    policy: EmailQualityPolicy
    threads: list[EmailThreadLabel] = Field(max_length=100_000)
    snapshots: list[EmailSnapshot] = Field(max_length=1_000)
    style: list[EmailStyleComparison] = Field(default_factory=list, max_length=10_000)
    memory: list[EmailMemoryJudgment] = Field(default_factory=list, max_length=100_000)
    existing_memory_benchmark_passed: bool | None = None

    @model_validator(mode="after")
    def validate_frozen_holdout(self) -> EmailQualityCorpus:
        if self.frozen_at > self.tuning_started_at:
            raise ValueError("freeze the corpus before tuning starts")
        by_id = {thread.id: thread for thread in self.threads}
        if len(by_id) != len(self.threads):
            raise ValueError("thread labels must be unique")
        conversations: set[tuple[str, UUID]] = set()
        conversation_splits: dict[UUID, str] = {}
        for thread in self.threads:
            conversation_identity = (thread.account, thread.conversation_id)
            if conversation_identity in conversations:
                raise ValueError("a conversation must have one label and cannot cross splits")
            conversations.add(conversation_identity)
            if conversation_splits.get(thread.conversation_id, thread.split) != thread.split:
                raise ValueError("a conversation cannot cross splits through another account")
            conversation_splits[thread.conversation_id] = thread.split
            if (thread.source_at < self.holdout_started_at) != (thread.split == "training"):
                raise ValueError(
                    "training and holdout sources must respect the chronological split"
                )
        if len({item.id for item in self.snapshots}) != len(self.snapshots) or len(
            {item.observed_at for item in self.snapshots}
        ) != len(self.snapshots):
            raise ValueError("independent snapshots need distinct ids and observation times")
        for snapshot in self.snapshots:
            if (
                not snapshot.profile_evidence_through
                < self.holdout_started_at
                <= snapshot.observed_at
            ):
                raise ValueError("profile evidence must predate the holdout")
            for identity in snapshot.profile_thread_ids:
                evidence = by_id.get(identity)
                if evidence is None or evidence.split != "training":
                    raise ValueError("profiles may reference only declared training threads")
                if evidence.source_at > snapshot.profile_evidence_through:
                    raise ValueError("profile evidence exceeds its declared chronological cutoff")
            pool = set(snapshot.candidate_ids)
            if len(pool) != len(snapshot.candidate_ids):
                raise ValueError("the complete candidate pool cannot contain duplicates")
            for identity in pool:
                candidate = by_id.get(identity)
                if candidate is None or candidate.split != "holdout":
                    raise ValueError("every candidate requires a held-out owner label")
                if candidate.source_at > snapshot.observed_at:
                    raise ValueError("a snapshot cannot observe future correspondence")
            if set(snapshot.baselines) != set(BASELINES):
                raise ValueError("all three declared baselines are required")
            for ranking in [snapshot.ranked_ids, *snapshot.baselines.values()]:
                if len(ranking) != len(pool) or set(ranking) != pool:
                    raise ValueError("each ranking must contain the complete candidate pool once")
            if snapshot.displayed_ids != snapshot.ranked_ids[: len(snapshot.displayed_ids)]:
                raise ValueError("displayed items must be the ranked prefix")
            if (
                len(set(snapshot.auto_drafted_ids)) != len(snapshot.auto_drafted_ids)
                or not set(snapshot.auto_drafted_ids) <= pool
            ):
                raise ValueError("automatic drafts must be distinct candidates")
        if len({item.id for item in self.style}) != len(self.style):
            raise ValueError("blind comparison ids must be unique")
        if len({item.thread_id for item in self.memory}) != len(self.memory):
            raise ValueError("memory facts must be scored once per source thread")
        evaluated = {identity for snapshot in self.snapshots for identity in snapshot.candidate_ids}
        for identity in [item.thread_id for item in self.style] + [
            item.thread_id for item in self.memory
        ]:
            if identity not in evaluated:
                raise ValueError("style and memory judgments require evaluated holdout threads")
        return self


class QualityCheck(LabelModel):
    status: Literal["passed", "failed", "pending"]
    reason: str


class EmailQualityReport(LabelModel):
    scorer_version: Literal["email-quality@1"] = "email-quality@1"
    status: Literal["passed", "failed", "pending"]
    corpus_sha256: str | None = None
    policy: EmailQualityPolicy | None = None
    checks: dict[str, QualityCheck]
    metrics: dict[str, Any] = Field(default_factory=dict)
    uncertainty: str = (
        "Priority intervals use a deterministic 95% snapshot-cluster bootstrap (2000 draws). "
        "Snapshots are declared independent by the labeler; repeated thread appearances and "
        "unique threads are counted separately. Zero observed variation can give a zero-width "
        "bootstrap interval and does not establish certainty. No live quality is inferred "
        "from synthetic inputs."
    )


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _cluster_rate(rows: list[tuple[int, int]]) -> dict[str, Any]:
    result = _rate(sum(hit for hit, _ in rows), sum(count for _, count in rows))
    draws: list[float] = []
    # Counter-derived samples have no ambient entropy, clock, or process state.
    # The versioned seed fixes the exact resampling experiment across machines.
    for draw in range(2000 if rows else 0):
        sample = [
            rows[
                int.from_bytes(
                    hashlib.sha256(f"email-quality@1:26:{draw}:{index}".encode()).digest()
                )
                % len(rows)
            ]
            for index in range(len(rows))
        ]
        denominator = sum(count for _, count in sample)
        if denominator:
            draws.append(sum(hit for hit, _ in sample) / denominator)
    draws.sort()
    result["interval_95"] = (
        [draws[int(0.025 * (len(draws) - 1))], draws[int(0.975 * (len(draws) - 1))]]
        if draws
        else None
    )
    return result


def _check(enough: bool, passed: bool, reason: str) -> QualityCheck:
    return QualityCheck(
        status="pending" if not enough else "passed" if passed else "failed", reason=reason
    )


def score_email_quality(corpus: EmailQualityCorpus | None) -> EmailQualityReport:
    """Score frozen labels without treating this report as activation evidence."""
    if corpus is None:
        return EmailQualityReport(
            status="pending",
            checks={
                "owner_evidence": QualityCheck(status="pending", reason="owner corpus missing")
            },
        )
    labels = {thread.id: thread for thread in corpus.threads}
    pools = {identity for snapshot in corpus.snapshots for identity in snapshot.candidate_ids}
    displayed = {identity for snapshot in corpus.snapshots for identity in snapshot.displayed_ids}
    accounts = {label.account for label in corpus.threads}
    missing_categories = sorted(RELATIONSHIPS - {label.relationship for label in corpus.threads})
    enough = (
        len(corpus.threads) >= 200
        and len(corpus.snapshots) >= 30
        and len(pools) >= 100
        and len(accounts) == 2
        and not missing_categories
    )
    checks = {
        "owner_evidence": _check(
            corpus.origin == "owner_private",
            True,
            "Private owner labels required; synthetic data is checker evidence only.",
        ),
        "corpus_coverage": _check(
            enough,
            True,
            "Require 200 independent threads, 30 snapshots, 100 unique held-out judgments, "
            "both accounts and all ten strata.",
        ),
    }
    precision_rows: list[tuple[int, int]] = []
    account_rows: dict[str, list[tuple[int, int]]] = {"account_1": [], "account_2": []}
    baseline_rows: dict[str, list[tuple[int, int]]] = {name: [] for name in BASELINES}
    baseline_coverage: dict[str, list[int]] = {name: [0, 0] for name in BASELINES}
    coverage = [0, 0]
    overloaded = [0, 0]
    overloaded_snapshots = 0
    subsets = {kind: [0, 0] for kind in ("actionable", "informational", "other")}
    missed_by_relationship: Counter[str] = Counter()
    draft_opportunities = draft_emitted = draft_relevant = draft_covered = 0
    abstentions = capacity_exclusions = unsupported_drafts = 0
    emitted_threads: set[UUID] = set()
    drafted_accounts: set[str] = set()
    for snapshot in corpus.snapshots:
        shown = [labels[identity] for identity in snapshot.displayed_ids]
        precision_rows.append((sum(item.important for item in shown), len(shown)))
        for account, rows in account_rows.items():
            items = [item for item in shown if item.account == account]
            rows.append((sum(item.important for item in items), len(items)))
        important = {identity for identity in snapshot.candidate_ids if labels[identity].important}
        top_ten = set(snapshot.ranked_ids[:10])
        hits = len(important & top_ten)
        destination = coverage if len(important) <= 10 else overloaded
        destination[0] += hits
        destination[1] += len(important)
        overloaded_snapshots += int(len(important) > 10)
        for identity in important:
            counts = subsets[labels[identity].content_kind]
            counts[0] += int(identity in top_ten)
            counts[1] += 1
            if identity not in top_ten:
                missed_by_relationship[labels[identity].relationship] += 1
        for name, ranking in snapshot.baselines.items():
            baseline_rows[name].append(
                (sum(labels[identity].important for identity in ranking[:5]), len(ranking[:5]))
            )
            if len(important) <= 10:
                baseline_coverage[name][0] += len(important & set(ranking[:10]))
                baseline_coverage[name][1] += len(important)
        eligible = [
            identity
            for identity in snapshot.ranked_ids
            if labels[identity].needs_reply
            and labels[identity].unambiguous
            and labels[identity].fully_supported
        ]
        slots = set(eligible[:3])
        emitted = set(snapshot.auto_drafted_ids)
        draft_opportunities += len(slots)
        draft_covered += len(slots & emitted)
        draft_emitted += len(emitted)
        draft_relevant += sum(labels[identity].needs_reply for identity in emitted)
        unsupported_drafts += sum(
            not labels[identity].unambiguous or not labels[identity].fully_supported
            for identity in emitted
        )
        abstentions += len(slots - emitted)
        capacity_exclusions += max(0, len(eligible) - 3)
        emitted_threads.update(emitted)
        drafted_accounts.update(labels[identity].account for identity in emitted)
    precision = _cluster_rate(precision_rows)
    per_account = {name: _cluster_rate(rows) for name, rows in account_rows.items()}
    baseline_precision = {name: _cluster_rate(rows) for name, rows in baseline_rows.items()}
    strongest = max((value["value"] or 0 for value in baseline_precision.values()), default=0)
    baseline_best_coverage = max(
        (hits / total if total else 0 for hits, total in baseline_coverage.values()), default=0
    )
    precision_value = precision["value"] or 0
    useful_coverage = coverage[0] / coverage[1] if coverage[1] else 0
    lift = precision_value - strongest
    ceiling_rule = strongest > 0.85
    personalization_passed = (
        precision_value >= 0.95 and useful_coverage >= baseline_best_coverage
        if ceiling_rule
        else lift >= 0.10 - 1e-12
    )
    checks["priority_precision"] = _check(
        enough and bool(displayed) and all(value["denominator"] for value in per_account.values()),
        precision_value >= 0.90
        and all(
            value["value"] >= 0.85 for value in per_account.values() if value["value"] is not None
        ),
        "Top-five precision >=90%; each account >=85%.",
    )
    checks["useful_coverage"] = _check(
        enough and bool(coverage[1]),
        useful_coverage >= 0.90,
        "First-ten recall >=90% on snapshots with <=10 important items.",
    )
    checks["personalization"] = _check(
        enough and bool(displayed),
        personalization_passed,
        "Lift >=10 percentage points; if strongest baseline >85%, precision >=95% "
        "and no coverage regression.",
    )
    draft_sufficient = (
        len(pools) >= 50 and len(emitted_threads) >= 30 and len(drafted_accounts) == 2
    )
    checks["draft_relevance"] = _check(
        enough and draft_sufficient and draft_opportunities > 0,
        draft_relevant / draft_emitted >= 0.90 if draft_emitted else False,
        "At least 50 cases and 30 distinct emitted drafts across both accounts; "
        "relevance >=90%, top-three opportunity coverage >=80%.",
    )
    if checks["draft_relevance"].status == "passed" and draft_covered / draft_opportunities < 0.8:
        checks["draft_relevance"] = checks["draft_relevance"].model_copy(
            update={"status": "failed"}
        )
    if unsupported_drafts:
        checks["draft_relevance"] = QualityCheck(
            status="failed", reason="Automatic drafts require unambiguous, fully supported replies."
        )
    style_count = len(corpus.style)
    style_preferred = sum(item.preferred == "personalized" for item in corpus.style)
    style_usable = sum(not item.substantial_rewrite for item in corpus.style)
    checks["style_usefulness"] = _check(
        enough and style_count >= 30,
        style_count > 0
        and style_preferred / style_count >= 0.70
        and style_usable / style_count >= 0.70,
        "At least 30 blind pairs; personalized preference and no-substantial-rewrite "
        "rates each >=70%.",
    )
    unfaithful = sum(
        not item.factually_faithful or item.fabricated_commitment or item.fabricated_recipient
        for item in corpus.style
    )
    checks["faithfulness"] = _check(
        bool(corpus.style),
        unfaithful == 0,
        "Factual failures, fabricated commitments and fabricated recipients block correctness.",
    )
    expected = sum(item.expected_useful for item in corpus.memory)
    emitted_facts = sum(item.emitted for item in corpus.memory)
    supported = sum(item.supported_useful for item in corpus.memory)
    violations = sum(
        item.attribution_violations + item.authority_violations for item in corpus.memory
    )
    additional = sum(item.useful_beyond_excerpt for item in corpus.memory)
    checks["semantic_memory"] = _check(
        enough
        and expected > 0
        and emitted_facts > 0
        and corpus.existing_memory_benchmark_passed is not None,
        (
            supported / emitted_facts >= 0.95
            and supported / expected >= 0.85
            and violations == 0
            and additional > 0
            and corpus.existing_memory_benchmark_passed is True
        )
        if expected and emitted_facts
        else False,
        "Precision >=95%, recall >=85%, zero attribution/authority failures, "
        "utility beyond excerpts and existing benchmark preservation.",
    )
    if violations or corpus.existing_memory_benchmark_passed is False:
        checks["semantic_memory"] = QualityCheck(
            status="failed",
            reason="Known attribution/authority or existing benchmark failures block correctness.",
        )
    metrics: dict[str, Any] = {
        "corpus": {
            "threads": len(corpus.threads),
            "independent_snapshots": len(corpus.snapshots),
            "holdout_judgments": len(pools),
            "candidate_appearances": sum(len(s.candidate_ids) for s in corpus.snapshots),
            "missing_strata": missing_categories,
            "threads_by_account": dict(Counter(item.account for item in corpus.threads)),
            "threads_by_relationship": dict(Counter(item.relationship for item in corpus.threads)),
        },
        "priority": {
            "precision": precision,
            "by_account": per_account,
            "displayed_appearances": precision["denominator"],
            "independent_displayed_threads": len(displayed),
        },
        "coverage": {
            "first_ten": _rate(*coverage),
            "overloaded_recall_at_ten": _rate(*overloaded),
            "overloaded_snapshots": overloaded_snapshots,
            "overflow": overloaded[1] - overloaded[0],
            "subsets": {name: _rate(*value) for name, value in subsets.items()},
            "misses_by_relationship": dict(missed_by_relationship),
        },
        "personalization": {
            "baselines": baseline_precision,
            "strongest_baseline_precision": strongest,
            "precision_lift": lift,
            "ceiling_rule_applied": ceiling_rule,
            "strongest_baseline_coverage": baseline_best_coverage,
        },
        "draft": {
            "relevance": _rate(draft_relevant, draft_emitted),
            "opportunity_coverage": _rate(draft_covered, draft_opportunities),
            "emitted_independent_threads": len(emitted_threads),
            "abstentions": abstentions,
            "capacity_exclusions": capacity_exclusions,
            "unsupported_draft_appearances": unsupported_drafts,
        },
        "style": {
            "preference": _rate(style_preferred, style_count),
            "no_substantial_rewrite": _rate(style_usable, style_count),
            "unfaithful": unfaithful,
        },
        "memory": {
            "precision": _rate(supported, emitted_facts),
            "recall": _rate(supported, expected),
            "attribution_or_authority_violations": violations,
            "useful_beyond_excerpt": additional,
        },
    }
    status: Literal["passed", "failed", "pending"] = (
        "failed"
        if any(check.status == "failed" for check in checks.values())
        else "pending"
        if any(check.status == "pending" for check in checks.values())
        else "passed"
    )
    return EmailQualityReport(
        status=status,
        checks=checks,
        metrics=metrics,
        policy=corpus.policy,
        corpus_sha256=hashlib.sha256(corpus.model_dump_json().encode()).hexdigest(),
    )
