"""Multi-session memory benchmark: corpus schema, deterministic scoring, baseline.

The benchmark measures what the memory subsystem forms from a multi-session
conversation, what it recalls when a later probe asks, and what it must never
render.  This module holds the pure, deterministic core of that harness: the
corpus schema and its loader, the scoring functions that turn a probe's recall
traces into integers, the aggregate over a whole run, and the comparison of a
run against the recorded baseline.

Everything here is a pure function of its arguments.  The driver that runs a
scenario against a live composition, the command-line entry point, and the live
model arm are added by later tasks; nothing in this module imports the
composition root.
"""

from __future__ import annotations

import hashlib
import html
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from string import punctuation
from typing import Literal, get_args
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import MemoryRecord, RecallTrace
from agent_core.domain.runs import RunStatus
from agent_core.evals.memory_formation import (
    EvaluationBelief,
    ExpectedBelief,
    _normalized,
)
from agent_core.memory.formation import MAX_AUTOMATIC_CANDIDATES

CORPUS_PATH = Path("evals/capability/memory-benchmark.v1.json")
BASELINE_PATH = Path("evals/capability/memory-benchmark.baseline.json")
BENCHMARK_VERSION = "memory-benchmark@1"

ProbeCategory = Literal[
    "single_hop",
    "multi_hop",
    "temporal",
    "update",
    "abstention",
    "preference",
    "transfer",
    "correction",
]
PROBE_CATEGORIES: tuple[ProbeCategory, ...] = get_args(ProbeCategory)

MINIMUM_SCENARIO_COUNT = 12
MINIMUM_PROBES_PER_CATEGORY = 3
MINIMUM_PROTECTED_SCENARIOS = 4
MAXIMUM_PROBES_PER_SCENARIO = 6
# A session identifier carries two digits, so a scenario cannot hold a hundred.
MAXIMUM_SESSIONS_PER_SCENARIO = 99
TEMPORAL_MINIMUM_SECONDS = 30 * 24 * 60 * 60

SCOPE_PATTERN = r"^[a-z][a-z0-9-]{1,63}$"

UNDEFINED_RATIO = "n/a"
_RATIO_PLACES = Decimal("0.0001")

# The external datasets publish token F1; an answer counts as correct at a half.
F1_CORRECT_THRESHOLD = Decimal("0.5")
_F1_PLACES = Decimal("0.0001")
_ZERO_F1 = Decimal(0).quantize(_F1_PLACES)
_ARTICLES = frozenset({"a", "an", "the"})

_DRIFT_IDENTITY = (
    "benchmark_version",
    "corpus_sha256",
    "formation_policy_version",
    "provider_formation_policy_version",
    "retrieval_policy_version",
    "extractor_name",
)
_DRIFT_COUNTS = ("scenario_count", "probe_count", "expected_beliefs", "needed_total")
_HIGHER_IS_BETTER = (
    "supported_beliefs",
    "needed_formed",
    "needed_recalled",
    "probe_runs_completed",
)
# The attribution counts partition `needed_recalled` by the moment that found
# the belief. A belief moving from one bucket to another says where recall
# happened, not how much of it did, so a move is reported and never judged.
_ATTRIBUTION_PARTITION = (
    "recalled_snapshot_only",
    "recalled_in_turn_only",
    "recalled_both",
)
_LOWER_IS_BETTER = (
    "fabricated_beliefs",
    "stale_live_beliefs",
    "noise_total",
    "dropped_for_budget",
    "dropped_for_ceiling",
    "blocked_rendered",
    "currency_violations",
    "currency_unformed",
    "abstention_leaks",
    "false_transfers",
    "run_policy_failures",
    "max_distinct_prefixes_per_probe",
)


class BenchmarkTurn(BaseModel):
    """One utterance; the clock advances before it is appended.

    Authored corpus turns are the principal's own, which is why `role` defaults
    to `user`.  A dataset-derived scenario also carries the assistant's turns,
    because an evidence reference counts turns as the dataset numbered them and
    because a conversation with the replies cut out is not the conversation the
    dataset recorded.  An assistant turn is appended as an assistant message and
    is never a formation source: the extractor reads principal user messages
    only (`memory/formation.py:196-201`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    role: Literal["user", "assistant"] = "user"
    advance_seconds: int = Field(default=0, ge=0)


class BenchmarkSession(BaseModel):
    """One conversation; the clock advances before the session is created."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^s\d{2}$")
    project_scope: str | None = Field(default=None, pattern=SCOPE_PATTERN)
    advance_seconds: int = Field(default=0, ge=0)
    turns: list[BenchmarkTurn] = Field(min_length=1)


class LabeledBelief(ExpectedBelief):
    """An expected belief a probe can name, and the session that states it."""

    label: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    session: str
    supersedes: str | None = None


class ProbeAnswer(BaseModel):
    """The gold answer a live arm scores against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["exact", "alternatives", "all_of", "abstain"]
    values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def values_match_kind(self) -> ProbeAnswer:
        count = len(self.values)
        satisfied = {
            "abstain": count == 0,
            "exact": count == 1,
            "alternatives": count >= 1,
            "all_of": count >= 2,
        }
        wanted = {
            "abstain": "no values",
            "exact": "exactly one value",
            "alternatives": "at least one value",
            "all_of": "at least two values",
        }
        if not satisfied[self.kind]:
            raise ValueError(f"a {self.kind} answer carries {wanted[self.kind]}, got {count}")
        return self


class EvidenceRef(BaseModel):
    """A pointer to the turn that supports a probe, for external datasets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    turn_index: int | None = Field(default=None, ge=0)


class SessionEvents(BaseModel):
    """The events one replayed session appended, in turn order.

    Event sequence numbers restart at one in every session, so a sequence means
    nothing without the session it was appended to; this pairs the two, and the
    evidence-provenance metric joins on both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    turn_sequences: list[int] = Field(default_factory=list)


class BenchmarkProbe(BaseModel):
    """One question asked after the conversations, with its category rule.

    `source_dataset` marks a probe an external adapter derived from a public
    dataset: it names its own evidence turns instead of corpus labels, so the
    label-arity rules the categories carry cannot apply to it.  `source_category`
    keeps the dataset's own category verbatim, so a mapping can be re-read
    without going back to the data, and `excluded_by_design` marks a probe this
    platform cannot answer by construction — a LongMemEval question whose answer
    lies in an assistant turn, which is never a formation source — so it is
    reported apart from the numbers it would otherwise depress.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^p\d{2}$")
    category: ProbeCategory
    question: str = Field(min_length=1)
    project_scope: str | None = Field(default=None, pattern=SCOPE_PATTERN)
    advance_seconds: int = Field(default=0, ge=0)
    needed: list[str] = Field(default_factory=list)
    answer: ProbeAnswer
    forbidden_statements: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    source_dataset: str | None = Field(default=None, min_length=1)
    source_category: str | None = Field(default=None, min_length=1)
    excluded_by_design: bool = False


class BenchmarkScenario(BaseModel):
    """Several sessions, the beliefs they state, and the probes that follow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^mb-[a-z0-9]+(?:-[a-z0-9]+)*-\d{3}$")
    title: str = Field(min_length=1)
    start_at: AwareDatetime
    sessions: list[BenchmarkSession] = Field(min_length=2, max_length=MAXIMUM_SESSIONS_PER_SCENARIO)
    beliefs: list[LabeledBelief]
    protected_statements: list[str] = Field(default_factory=list)
    probes: list[BenchmarkProbe] = Field(min_length=1, max_length=MAXIMUM_PROBES_PER_SCENARIO)

    @model_validator(mode="after")
    def scenario_is_coherent(self) -> BenchmarkScenario:
        session_order = {session.id: index for index, session in enumerate(self.sessions)}
        if len(session_order) != len(self.sessions):
            raise ValueError(f"scenario {self.id} repeats a session id")
        labels = {belief.label: belief for belief in self.beliefs}
        if len(labels) != len(self.beliefs):
            raise ValueError(f"scenario {self.id} repeats a belief label")
        if len({probe.id for probe in self.probes}) != len(self.probes):
            raise ValueError(f"scenario {self.id} repeats a probe id")
        for belief in self.beliefs:
            if belief.session not in session_order:
                raise ValueError(f"belief {belief.label} names unknown session {belief.session}")
            if belief.supersedes is None:
                continue
            superseded = labels.get(belief.supersedes)
            if superseded is None:
                raise ValueError(
                    f"belief {belief.label} supersedes unknown label {belief.supersedes}"
                )
            if session_order[belief.session] <= session_order[superseded.session]:
                raise ValueError(
                    f"belief {belief.label} must be stated after {superseded.label}, "
                    "not in the same session or an earlier one"
                )
        for probe in self.probes:
            unknown = [name for name in probe.needed if name not in labels]
            if unknown:
                raise ValueError(f"probe {probe.id} needs unknown labels {', '.join(unknown)}")
            _check_probe_category(self, probe, labels, session_order)
        return self


class MemoryBenchmarkCorpus(BaseModel):
    """The checked-in benchmark corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    probe_instruction: str = Field(min_length=1)
    abstain_phrase: str = Field(min_length=1)
    scenarios: list[BenchmarkScenario] = Field(min_length=MINIMUM_SCENARIO_COUNT)

    @model_validator(mode="after")
    def corpus_covers_the_categories_and_protected_content(self) -> MemoryBenchmarkCorpus:
        if len({scenario.id for scenario in self.scenarios}) != len(self.scenarios):
            raise ValueError("benchmark scenario ids must be unique")
        if not _normalized(self.abstain_phrase):
            raise ValueError("benchmark abstain phrase must normalize to a non-empty string")
        derived = [
            f"{scenario.id}/{probe.id}"
            for scenario in self.scenarios
            for probe in scenario.probes
            if probe.source_dataset is not None
            or probe.source_category is not None
            or probe.excluded_by_design
            or probe.evidence
        ]
        if derived:
            raise ValueError(
                "the benchmark corpus is authored, so no probe carries dataset provenance; "
                f"{', '.join(derived)} do"
            )
        counts = Counter(probe.category for scenario in self.scenarios for probe in scenario.probes)
        thin = [
            category
            for category in PROBE_CATEGORIES
            if counts[category] < MINIMUM_PROBES_PER_CATEGORY
        ]
        if thin:
            raise ValueError(
                f"benchmark corpus needs {MINIMUM_PROBES_PER_CATEGORY} probes in every category; "
                f"short of it: {', '.join(thin)}"
            )
        protected = sum(1 for scenario in self.scenarios if _guards_protected_content(scenario))
        if protected < MINIMUM_PROTECTED_SCENARIOS:
            raise ValueError(
                f"benchmark corpus needs {MINIMUM_PROTECTED_SCENARIOS} scenarios pairing "
                f"protected statements with an abstention probe, found {protected}"
            )
        return self


def _guards_protected_content(scenario: BenchmarkScenario) -> bool:
    """Report whether a scenario pairs protected content with an abstention probe."""

    protected = {value for value in map(_normalized, scenario.protected_statements) if value}
    if not protected:
        return False
    return any(
        probe.category == "abstention"
        and protected & set(map(_normalized, probe.forbidden_statements))
        for probe in scenario.probes
    )


def _check_probe_category(
    scenario: BenchmarkScenario,
    probe: BenchmarkProbe,
    labels: dict[str, LabeledBelief],
    session_order: dict[str, int],
) -> None:
    """Enforce the rule the probe's category carries, or raise ValueError."""

    if probe.source_dataset is not None:
        _check_dataset_probe(probe)
        return
    needed = [labels[name] for name in probe.needed]
    match probe.category:
        case "single_hop":
            if len(needed) != 1:
                raise ValueError(
                    f"single_hop probe {probe.id} needs exactly one belief, got {len(needed)}"
                )
        case "multi_hop":
            if len(needed) < 2 or probe.answer.kind != "all_of":
                raise ValueError(
                    f"multi_hop probe {probe.id} needs two or more beliefs and an all_of answer"
                )
        case "temporal":
            for label in needed:
                elapsed = _cumulative_advance_seconds(scenario, label, probe)
                if elapsed < TEMPORAL_MINIMUM_SECONDS:
                    raise ValueError(
                        f"temporal probe {probe.id} follows {label.label} by {elapsed} seconds, "
                        f"less than the required {TEMPORAL_MINIMUM_SECONDS}"
                    )
        case "update" | "correction":
            replaced = needed[0].supersedes if len(needed) == 1 else None
            if replaced is None:
                raise ValueError(
                    f"{probe.category} probe {probe.id} needs exactly one superseding belief"
                )
            superseded = labels[replaced]
            forbidden = set(map(_normalized, probe.forbidden_statements))
            missing = set(map(_normalized, superseded.statements)) - forbidden
            if missing:
                raise ValueError(
                    f"{probe.category} probe {probe.id} must forbid every statement of "
                    f"{superseded.label}"
                )
        case "preference":
            offenders = [
                label.label for label in needed if label.belief_type.casefold() != "preference"
            ]
            if offenders:
                raise ValueError(
                    f"preference probe {probe.id} needs preference beliefs, "
                    f"but {', '.join(offenders)} are not"
                )
        case "transfer":
            if probe.project_scope is None:
                raise ValueError(f"transfer probe {probe.id} requires a project scope")
            if needed:
                same = [
                    label.label
                    for label in needed
                    if scenario.sessions[session_order[label.session]].project_scope
                    == probe.project_scope
                ]
                if same:
                    raise ValueError(
                        f"transfer probe {probe.id} needs beliefs from another project scope, "
                        f"but {', '.join(same)} were stated in its own"
                    )
            elif probe.answer.kind != "abstain" or not probe.forbidden_statements:
                raise ValueError(
                    f"transfer probe {probe.id} without needed beliefs must abstain and "
                    "forbid a statement"
                )
        case "abstention":
            if needed or probe.answer.kind != "abstain" or not probe.forbidden_statements:
                raise ValueError(
                    f"abstention probe {probe.id} needs no beliefs, an abstain answer, "
                    "and forbidden statements"
                )


def _check_dataset_probe(probe: BenchmarkProbe) -> None:
    """Enforce what a dataset-derived probe answers to, or raise ValueError.

    The category rules are corpus-authoring rules: they hold an authored probe
    to the labels its category implies.  A dataset supplies neither labels nor
    the protected content the forbidden statements guard, so a probe an adapter
    derived carries its own evidence and keeps only the rules that still mean
    something — no corpus label, no project transfer the datasets never scope,
    and an abstention category exactly when the gold answer abstains.
    """

    if probe.needed:
        raise ValueError(
            f"dataset probe {probe.id} names no corpus label, but names {', '.join(probe.needed)}"
        )
    if probe.category == "transfer":
        raise ValueError(
            f"dataset probe {probe.id} cannot be a transfer probe: "
            "the external datasets carry no project scopes"
        )
    if (probe.category == "abstention") != (probe.answer.kind == "abstain"):
        raise ValueError(
            f"dataset probe {probe.id} must abstain exactly when its category does, but "
            f"category {probe.category} carries a {probe.answer.kind} answer"
        )


def _cumulative_advance_seconds(
    scenario: BenchmarkScenario, label: LabeledBelief, probe: BenchmarkProbe
) -> int:
    """Seconds of clock advance between a belief's session and a probe.

    The driver advances the clock before every session, before every turn, and
    before every probe.  The gap counted here starts at the first turn of the
    session that states the belief and ends at the probe: the turn advances
    that follow that session's first turn, every session and turn advance of
    the sessions after it, and the probe's own advance.  Advances belonging to
    probes that run before this one are deliberately excluded, so the number is
    a lower bound on the real gap and a temporal probe has to stand on its own
    advance rather than on a sibling's.
    """

    index = next(
        position
        for position, session in enumerate(scenario.sessions)
        if session.id == label.session
    )
    total = sum(turn.advance_seconds for turn in scenario.sessions[index].turns[1:])
    for session in scenario.sessions[index + 1 :]:
        total += session.advance_seconds
        total += sum(turn.advance_seconds for turn in session.turns)
    return total + probe.advance_seconds


class ConsolidationCounts(BaseModel):
    """What one consolidation run proposed and committed for a session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    candidates_proposed: int = Field(ge=0)
    committed: int = Field(ge=0)
    reinforced: int = Field(ge=0)
    superseded: int = Field(ge=0)
    rejected: int = Field(ge=0)


class FormationMetrics(BaseModel):
    """What a scenario's conversations left in the store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: int = Field(ge=0)
    supported: int = Field(ge=0)
    formed: int = Field(ge=0)
    fabricated: int = Field(ge=0)
    stale_live: int = Field(ge=0)
    policy_failures: int = Field(ge=0)


class DistanceRecall(BaseModel):
    """Needed-label recall at one evidence distance.

    Distance is counted in sessions: a label stated in the scenario's last
    session sits one session back from the probe, which always runs after
    every session.  Binning recall by it makes long-range degradation visible
    — a ranking change can hold `needed_recalled` steady overall while losing
    exactly the labels whose evidence is oldest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    needed_total: int = Field(ge=0)
    needed_recalled: int = Field(ge=0)


class ProbeRetrievalResult(BaseModel):
    """What one probe formed, recalled, returned, and rendered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(min_length=1)
    category: ProbeCategory
    run_completed: bool
    needed_total: int = Field(ge=0)
    needed_formed: int = Field(ge=0)
    needed_recalled: int = Field(ge=0)
    needed_by_distance: dict[str, DistanceRecall] = Field(default_factory=dict)
    recalled_snapshot_only: int = Field(ge=0)
    recalled_in_turn_only: int = Field(ge=0)
    recalled_both: int = Field(ge=0)
    returned_snapshot: int = Field(ge=0)
    returned_in_turn: int = Field(ge=0)
    returned_total: int = Field(ge=0)
    noise_snapshot: int = Field(ge=0)
    noise_in_turn: int = Field(ge=0)
    noise_total: int = Field(ge=0)
    dropped_for_budget: int = Field(ge=0)
    blocked_rendered: int = Field(ge=0)
    currency_violations: int = Field(ge=0)
    currency_unformed: int = Field(ge=0)
    abstention_leaks: int = Field(ge=0)
    false_transfers: int = Field(ge=0)
    other_forbidden_rendered: int = Field(ge=0)
    forbidden_rendered: int = Field(ge=0)
    policy_failures: int = Field(ge=0)
    distinct_prefixes: int = Field(ge=0)
    evidence_total: int = Field(default=0, ge=0)
    evidence_recalled: int = Field(default=0, ge=0)
    snapshot_trace_id: UUID | None = None
    in_turn_trace_ids: list[UUID] = Field(default_factory=list)


class DeterministicScenarioResult(BaseModel):
    """One scenario's formation, consolidation, and probe results.

    `extractor_name` names the candidate extractor the composition wired for
    this scenario, which is the identity a baseline records; it is carried per
    scenario because the driver that owns the composition is the only place
    that can read it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str = Field(min_length=1)
    extractor_name: str = Field(min_length=1)
    formation: FormationMetrics
    consolidations: list[ConsolidationCounts] = Field(default_factory=list)
    probes: list[ProbeRetrievalResult] = Field(default_factory=list)
    beliefs: list[EvaluationBelief] = Field(default_factory=list)


class CategoryMetrics(BaseModel):
    """The per-category slice of the aggregate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probes: int = Field(ge=0)
    needed_total: int = Field(ge=0)
    needed_formed: int = Field(ge=0)
    needed_recalled: int = Field(ge=0)
    returned_total: int = Field(ge=0)
    noise_total: int = Field(ge=0)
    forbidden_rendered: int = Field(ge=0)


class DeterministicMetrics(BaseModel):
    """The whole deterministic run as integers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_count: int = Field(ge=0)
    probe_count: int = Field(ge=0)
    expected_beliefs: int = Field(ge=0)
    supported_beliefs: int = Field(ge=0)
    formed_beliefs: int = Field(ge=0)
    fabricated_beliefs: int = Field(ge=0)
    stale_live_beliefs: int = Field(ge=0)
    formation_policy_failures: int = Field(ge=0)
    needed_total: int = Field(ge=0)
    needed_formed: int = Field(ge=0)
    needed_recalled: int = Field(ge=0)
    recall_by_distance: dict[str, DistanceRecall] = Field(default_factory=dict)
    recalled_snapshot_only: int = Field(ge=0)
    recalled_in_turn_only: int = Field(ge=0)
    recalled_both: int = Field(ge=0)
    returned_total: int = Field(ge=0)
    noise_total: int = Field(ge=0)
    dropped_for_budget: int = Field(ge=0)
    dropped_for_ceiling: int = Field(default=0, ge=0)
    blocked_rendered: int = Field(ge=0)
    currency_violations: int = Field(ge=0)
    currency_unformed: int = Field(ge=0)
    abstention_leaks: int = Field(ge=0)
    false_transfers: int = Field(ge=0)
    run_policy_failures: int = Field(ge=0)
    probe_runs_completed: int = Field(ge=0)
    max_distinct_prefixes_per_probe: int = Field(ge=0)
    per_category: dict[str, CategoryMetrics] = Field(default_factory=dict)


class DeterministicBenchmarkResult(BaseModel):
    """One deterministic benchmark run and the identity it was run under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark_version: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    formation_policy_version: str = Field(min_length=1)
    provider_formation_policy_version: str = Field(min_length=1)
    retrieval_policy_version: str = Field(min_length=1)
    extractor_name: str = Field(min_length=1)
    scenarios: list[DeterministicScenarioResult] = Field(default_factory=list)
    metrics: DeterministicMetrics


class BaselineProbeRow(BaseModel):
    """One probe's recorded counts, without the trace identifiers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str = Field(min_length=1)
    probe_id: str = Field(min_length=1)
    category: ProbeCategory
    needed_total: int = Field(ge=0)
    needed_formed: int = Field(ge=0)
    needed_recalled: int = Field(ge=0)
    returned_total: int = Field(ge=0)
    noise_total: int = Field(ge=0)
    forbidden_rendered: int = Field(ge=0)
    distinct_prefixes: int = Field(ge=0)


class MemoryBenchmarkBaseline(BaseModel):
    """The recorded deterministic run a later run is compared against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    benchmark_version: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    formation_policy_version: str = Field(min_length=1)
    provider_formation_policy_version: str = Field(min_length=1)
    retrieval_policy_version: str = Field(min_length=1)
    extractor_name: str = Field(min_length=1)
    build_ref: str = Field(min_length=1)
    recorded_at: datetime
    metrics: DeterministicMetrics
    probes: list[BaselineProbeRow] = Field(default_factory=list)


class BaselineComparison(BaseModel):
    """Why a run differs from its baseline, sorted three ways."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    drift: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    shifts: list[str] = Field(default_factory=list)


def load_corpus(repository_root: Path) -> tuple[MemoryBenchmarkCorpus, str]:
    """Read the checked-in corpus and the digest that travels with a result."""

    root = repository_root.resolve()
    path = (root / CORPUS_PATH).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("memory-benchmark corpus path escapes the repository") from exc
    raw = path.read_bytes()
    corpus = MemoryBenchmarkCorpus.model_validate_json(raw)
    return corpus, hashlib.sha256(raw).hexdigest()


def load_baseline(repository_root: Path) -> MemoryBenchmarkBaseline | None:
    """Read the recorded baseline, or None when it has not been recorded."""

    root = repository_root.resolve()
    path = (root / BASELINE_PATH).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("memory-benchmark baseline path escapes the repository") from exc
    if not path.is_file():
        return None
    return MemoryBenchmarkBaseline.model_validate_json(path.read_bytes())


def match_label(belief_type: str, subject: str, statement: str, label: LabeledBelief) -> bool:
    """Report whether a belief satisfies a label under the formation triple."""

    return (
        belief_type.casefold() == label.belief_type.casefold()
        and _normalized(subject) in set(map(_normalized, label.subjects))
        and _normalized(statement) in set(map(_normalized, label.statements))
    )


def forbidden_rendered(trace: RecallTrace, statement: str) -> bool:
    """Report whether a forbidden statement reached one recall trace."""

    target = _normalized(statement)
    if not target:
        return False
    if any(_normalized(belief.statement) == target for belief in trace.beliefs):
        return True
    return target in _normalized(html.unescape(trace.rendered))


def evidence_event_refs(
    probe: BenchmarkProbe, session_events: Mapping[str, SessionEvents]
) -> set[tuple[UUID, int]]:
    """Resolve a probe's evidence references to the events the run appended.

    A reference carrying no turn index names its whole session, which is the
    granularity a dataset gives when it points at a session rather than at a
    turn.  A reference the run never replayed resolves to nothing, and a probe
    resolving to nothing is not counted: provenance is undefined for it.
    """

    events: set[tuple[UUID, int]] = set()
    for reference in probe.evidence:
        recorded = session_events.get(reference.session_id)
        if recorded is None:
            continue
        if reference.turn_index is None:
            events.update((recorded.session_id, sequence) for sequence in recorded.turn_sequences)
        elif reference.turn_index < len(recorded.turn_sequences):
            events.add((recorded.session_id, recorded.turn_sequences[reference.turn_index]))
    return events


def evidence_provenance_recalled(
    probe: BenchmarkProbe,
    traces: Sequence[RecallTrace],
    session_events: Mapping[str, SessionEvents],
    *,
    store: Sequence[MemoryRecord],
) -> bool:
    """Report whether recall returned a belief formed from the probe's evidence.

    This is the metric the internal corpus cannot express: not whether the right
    fact came back, but whether it came back for the right reason.  The traces
    say which beliefs were returned and the store says which session and which
    event each of those was formed from, because a recalled belief carries its
    source sequences without the session they belong to and a sequence restarts
    at one in every session.
    """

    targets = evidence_event_refs(probe, session_events)
    if not targets:
        return False
    returned = {identifier for trace in traces for identifier in trace.returned}
    return any(
        (record.source_session_id, sequence) in targets
        for record in store
        if record.id in returned
        for sequence in record.source_event_ids
    )


def score_answer_f1(answer: str | None, gold: ProbeAnswer) -> Decimal:
    """Score one answer by normalized token F1 against the best gold value.

    The external datasets publish token F1 rather than exact match, so their
    adapters score with it and count an answer correct at
    `F1_CORRECT_THRESHOLD`; the authored corpus keeps the stricter mechanical
    scoring in `score_answer`, which admits no partial credit.  Normalization
    is the published one: casefold, drop punctuation and the articles, and
    compare the token multisets.
    """

    predicted = _answer_tokens(answer or "")
    return max(
        (_token_f1(predicted, _answer_tokens(value)) for value in gold.values),
        default=_ZERO_F1,
    )


def _answer_tokens(value: str) -> list[str]:
    """Normalize one answer into the tokens token F1 compares."""

    stripped = "".join(character for character in value.casefold() if character not in punctuation)
    return [token for token in stripped.split() if token not in _ARTICLES]


def _token_f1(predicted: Sequence[str], gold: Sequence[str]) -> Decimal:
    """The harmonic mean of token precision and recall, to four places."""

    if not predicted or not gold:
        return Decimal(1).quantize(_F1_PLACES) if predicted == gold else _ZERO_F1
    common = sum((Counter(predicted) & Counter(gold)).values())
    if not common:
        return _ZERO_F1
    precision = Decimal(common) / Decimal(len(predicted))
    recall = Decimal(common) / Decimal(len(gold))
    return (2 * precision * recall / (precision + recall)).quantize(_F1_PLACES)


def current_labels(scenario: BenchmarkScenario) -> list[LabeledBelief]:
    """The labels a later label does not supersede."""

    replaced = _replaced_label_names(scenario)
    return [belief for belief in scenario.beliefs if belief.label not in replaced]


def superseded_labels(scenario: BenchmarkScenario) -> list[LabeledBelief]:
    """The labels a later label supersedes."""

    replaced = _replaced_label_names(scenario)
    return [belief for belief in scenario.beliefs if belief.label in replaced]


def _replaced_label_names(scenario: BenchmarkScenario) -> set[str]:
    return {belief.supersedes for belief in scenario.beliefs if belief.supersedes is not None}


def score_formation(
    scenario: BenchmarkScenario,
    live_beliefs: Sequence[MemoryRecord],
    all_beliefs: Sequence[MemoryRecord],
) -> FormationMetrics:
    """Score what a scenario's conversations left in the store.

    Current labels are matched greedily one-to-one against the live beliefs, as
    the formation evaluation matches its cases, and the matched beliefs are
    `supported`.  Of the beliefs left over, the ones matching a label a later
    label supersedes are `stale_live` and the rest are `fabricated`, so the
    three partition the live store.  A policy failure is a belief of any status
    whose statement contains a protected fragment, which is why the whole store
    is passed alongside the live slice.
    """

    current = current_labels(scenario)
    occupied: set[int] = set()
    supported = 0
    for label in current:
        for index, record in enumerate(live_beliefs):
            if index not in occupied and _record_matches(record, label):
                occupied.add(index)
                supported += 1
                break
    superseded = superseded_labels(scenario)
    stale = sum(
        1
        for index, record in enumerate(live_beliefs)
        if index not in occupied and any(_record_matches(record, label) for label in superseded)
    )
    protected = {value for value in map(_normalized, scenario.protected_statements) if value}
    return FormationMetrics(
        expected=len(current),
        supported=supported,
        formed=len(live_beliefs),
        fabricated=len(live_beliefs) - len(occupied) - stale,
        stale_live=stale,
        policy_failures=sum(
            1
            for record in all_beliefs
            if any(fragment in _normalized(record.statement) for fragment in protected)
        ),
    )


def probe_run_facts(
    events: Sequence[EventEnvelope], run_status: RunStatus
) -> tuple[int, int, bool]:
    """Read the prefix, policy, and completion facts out of a probe run.

    Returns the number of distinct prompt prefixes the run's model requests
    reported, the number of policy failures — a denied tool call, or an event
    whose reason code is in the policy namespace — and whether the run reached
    COMPLETED.  The two counts follow the rules the evaluation runner already
    applies to a case.
    """

    prefixes = {
        value
        for event in events
        if event.event_type == "model.request.started"
        and isinstance((value := event.payload.get("prefix_sha256")), str)
    }
    failures = sum(
        event.event_type == "tool.call.denied"
        or (
            isinstance((reason := event.payload.get("reason_code")), str)
            and reason.startswith("policy.")
        )
        for event in events
    )
    return len(prefixes), failures, run_status is RunStatus.COMPLETED


def score_probe(
    probe: BenchmarkProbe,
    scenario: BenchmarkScenario,
    *,
    store_live: Sequence[MemoryRecord],
    snapshot: RecallTrace | None,
    in_turn: Sequence[RecallTrace],
    distinct_prefixes: int,
    policy_failures: int,
    run_completed: bool,
    evidence_total: int = 0,
    evidence_recalled: int = 0,
    snapshot_trace_id: UUID | None = None,
    in_turn_trace_ids: Sequence[UUID] = (),
) -> ProbeRetrievalResult:
    """Score one probe's formation, recall, noise, and forbidden rendering.

    A needed label is *formed* when a live belief matches it at probe time and
    *recalled* when a belief in the snapshot trace or in any in-turn trace
    matches it; a trace carries exactly the beliefs it returned within budget,
    so recall counts returned items only.  Returned counts are distinct belief
    identifiers per moment and across both.  A returned belief matching no
    needed label is noise, which makes every returned belief noise for a probe
    that needs nothing; `noise_total` counts the distinct noisy beliefs across
    both moments, so a belief both moments returned counts once and the noise
    ratio it feeds stays a ratio, while `noise_snapshot` and `noise_in_turn`
    attribute that noise to each moment.  Each forbidden statement the probe
    rendered anywhere is counted once and bucketed by the probe's category.

    `evidence_total` and `evidence_recalled` are the evidence-provenance pair,
    and they count probes rather than beliefs: a probe naming evidence the run
    replayed counts one, and it counts as recalled when a returned belief was
    formed from one of those events.  Both are zero for the authored corpus,
    which names labels instead of evidence; the caller derives them with
    :func:`evidence_provenance_recalled`, which needs the store this function
    is not given.
    """

    labels = {belief.label: belief for belief in scenario.beliefs}
    needed = [labels[name] for name in probe.needed]
    snapshots: list[RecallTrace] = [] if snapshot is None else [snapshot]
    traces = [*snapshots, *in_turn]

    formed = {
        label.label
        for label in needed
        if any(_record_matches(record, label) for record in store_live)
    }
    from_snapshot = {label.label for label in needed if _trace_matches(snapshots, label)}
    from_in_turn = {label.label for label in needed if _trace_matches(in_turn, label)}

    snapshot_returned = {identifier for value in snapshots for identifier in value.returned}
    in_turn_returned = {identifier for value in in_turn for identifier in value.returned}

    counts = _forbidden_counts(probe, needed, formed, traces)
    return ProbeRetrievalResult(
        probe_id=probe.id,
        category=probe.category,
        run_completed=run_completed,
        needed_total=len(needed),
        needed_formed=len(formed),
        needed_recalled=len(from_snapshot | from_in_turn),
        needed_by_distance=_needed_by_distance(scenario, needed, from_snapshot | from_in_turn),
        recalled_snapshot_only=len(from_snapshot - from_in_turn),
        recalled_in_turn_only=len(from_in_turn - from_snapshot),
        recalled_both=len(from_snapshot & from_in_turn),
        returned_snapshot=len(snapshot_returned),
        returned_in_turn=len(in_turn_returned),
        returned_total=len(snapshot_returned | in_turn_returned),
        noise_snapshot=_noise_count(snapshots, needed),
        noise_in_turn=_noise_count(in_turn, needed),
        noise_total=_noise_count(traces, needed),
        dropped_for_budget=sum(len(value.dropped_for_budget) for value in traces),
        blocked_rendered=sum(len(value.blocked) for value in traces),
        currency_violations=counts["currency_violations"],
        currency_unformed=counts["currency_unformed"],
        abstention_leaks=counts["abstention_leaks"],
        false_transfers=counts["false_transfers"],
        other_forbidden_rendered=counts["other_forbidden_rendered"],
        forbidden_rendered=sum(counts.values()),
        policy_failures=policy_failures,
        distinct_prefixes=distinct_prefixes,
        evidence_total=evidence_total,
        evidence_recalled=evidence_recalled,
        snapshot_trace_id=snapshot_trace_id,
        in_turn_trace_ids=list(in_turn_trace_ids),
    )


def _needed_by_distance(
    scenario: BenchmarkScenario,
    needed: Sequence[LabeledBelief],
    recalled: set[str],
) -> dict[str, DistanceRecall]:
    """Bin a probe's needed labels by how many sessions back each is stated.

    Every probe runs after the scenario's last session, so a label stated in
    that session is one session away and the scenario's first session is
    `len(sessions)` away.  Keys are the decimal distance, ordered nearest
    first so the serialized mapping is deterministic.
    """

    session_order = {session.id: index for index, session in enumerate(scenario.sessions)}
    totals: Counter[int] = Counter()
    hits: Counter[int] = Counter()
    for label in needed:
        distance = len(scenario.sessions) - session_order[label.session]
        totals[distance] += 1
        hits[distance] += int(label.label in recalled)
    return {
        str(distance): DistanceRecall(needed_total=totals[distance], needed_recalled=hits[distance])
        for distance in sorted(totals)
    }


def _record_matches(record: MemoryRecord, label: LabeledBelief) -> bool:
    return match_label(record.belief_type, record.subject, record.statement, label)


def _trace_matches(traces: Sequence[RecallTrace], label: LabeledBelief) -> bool:
    return any(
        match_label(belief.belief_type, belief.subject, belief.statement, label)
        for value in traces
        for belief in value.beliefs
    )


def _noise_count(traces: Sequence[RecallTrace], needed: Sequence[LabeledBelief]) -> int:
    """Count distinct returned beliefs that match none of the needed labels."""

    beliefs = {belief.belief_id: belief for value in traces for belief in value.beliefs}
    returned = {identifier for value in traces for identifier in value.returned}
    noise = 0
    for identifier in returned:
        belief = beliefs.get(identifier)
        if belief is None or not any(
            match_label(belief.belief_type, belief.subject, belief.statement, label)
            for label in needed
        ):
            noise += 1
    return noise


def _forbidden_counts(
    probe: BenchmarkProbe,
    needed: Sequence[LabeledBelief],
    formed: set[str],
    traces: Sequence[RecallTrace],
) -> dict[str, int]:
    """Bucket the forbidden statements this probe rendered by its category."""

    buckets = dict.fromkeys(
        (
            "currency_violations",
            "currency_unformed",
            "abstention_leaks",
            "false_transfers",
            "other_forbidden_rendered",
        ),
        0,
    )
    rendered = sum(
        any(forbidden_rendered(value, statement) for value in traces)
        for statement in probe.forbidden_statements
    )
    if not rendered:
        return buckets
    if probe.category in {"update", "correction"}:
        superseding = [label for label in needed if label.supersedes is not None]
        current = bool(superseding) and all(label.label in formed for label in superseding)
        buckets["currency_violations" if current else "currency_unformed"] = rendered
    elif probe.category == "abstention":
        buckets["abstention_leaks"] = rendered
    elif probe.category == "transfer":
        buckets["false_transfers"] = rendered
    else:
        buckets["other_forbidden_rendered"] = rendered
    return buckets


def aggregate_deterministic(
    scenarios: Sequence[DeterministicScenarioResult],
) -> DeterministicMetrics:
    """Sum every scenario and probe into the run's integer metrics."""

    probes = [probe for scenario in scenarios for probe in scenario.probes]
    formations = [scenario.formation for scenario in scenarios]
    return DeterministicMetrics(
        scenario_count=len(scenarios),
        probe_count=len(probes),
        expected_beliefs=sum(value.expected for value in formations),
        supported_beliefs=sum(value.supported for value in formations),
        formed_beliefs=sum(value.formed for value in formations),
        fabricated_beliefs=sum(value.fabricated for value in formations),
        stale_live_beliefs=sum(value.stale_live for value in formations),
        formation_policy_failures=sum(value.policy_failures for value in formations),
        needed_total=sum(probe.needed_total for probe in probes),
        needed_formed=sum(probe.needed_formed for probe in probes),
        needed_recalled=sum(probe.needed_recalled for probe in probes),
        recall_by_distance=_sum_distance_bins(probes),
        recalled_snapshot_only=sum(probe.recalled_snapshot_only for probe in probes),
        recalled_in_turn_only=sum(probe.recalled_in_turn_only for probe in probes),
        recalled_both=sum(probe.recalled_both for probe in probes),
        returned_total=sum(probe.returned_total for probe in probes),
        noise_total=sum(probe.noise_total for probe in probes),
        dropped_for_budget=sum(probe.dropped_for_budget for probe in probes),
        dropped_for_ceiling=sum(
            max(0, consolidation.candidates_proposed - MAX_AUTOMATIC_CANDIDATES)
            for scenario in scenarios
            for consolidation in scenario.consolidations
        ),
        blocked_rendered=sum(probe.blocked_rendered for probe in probes),
        currency_violations=sum(probe.currency_violations for probe in probes),
        currency_unformed=sum(probe.currency_unformed for probe in probes),
        abstention_leaks=sum(probe.abstention_leaks for probe in probes),
        false_transfers=sum(probe.false_transfers for probe in probes),
        run_policy_failures=sum(probe.policy_failures for probe in probes),
        probe_runs_completed=sum(1 for probe in probes if probe.run_completed),
        max_distinct_prefixes_per_probe=max(
            (probe.distinct_prefixes for probe in probes), default=0
        ),
        per_category=_per_category(probes),
    )


def _sum_distance_bins(probes: Sequence[ProbeRetrievalResult]) -> dict[str, DistanceRecall]:
    """Sum every probe's distance bins, ordered nearest first."""

    totals: Counter[int] = Counter()
    hits: Counter[int] = Counter()
    for probe in probes:
        for key, bin_ in probe.needed_by_distance.items():
            totals[int(key)] += bin_.needed_total
            hits[int(key)] += bin_.needed_recalled
    return {
        str(distance): DistanceRecall(needed_total=totals[distance], needed_recalled=hits[distance])
        for distance in sorted(totals)
    }


def _per_category(probes: Sequence[ProbeRetrievalResult]) -> dict[str, CategoryMetrics]:
    """Slice the probe counts by category, in a stable alphabetical order."""

    return {
        category: CategoryMetrics(
            probes=len(members),
            needed_total=sum(probe.needed_total for probe in members),
            needed_formed=sum(probe.needed_formed for probe in members),
            needed_recalled=sum(probe.needed_recalled for probe in members),
            returned_total=sum(probe.returned_total for probe in members),
            noise_total=sum(probe.noise_total for probe in members),
            forbidden_rendered=sum(probe.forbidden_rendered for probe in members),
        )
        for category in sorted({probe.category for probe in probes})
        if (members := [probe for probe in probes if probe.category == category])
    }


def ratios(metrics: DeterministicMetrics) -> dict[str, str]:
    """Derive the display ratios, each named by its own denominator.

    `formation_precision` divides supported beliefs by the beliefs formed and
    `formation_recall` divides them by the labels expected.  `end_to_end_recall`
    divides recalled needed labels by every needed label and
    `retrieval_recall_given_formed` divides them by the needed labels that
    formed, which separates a retrieval miss from an extractor gap.
    `noise_ratio` divides noise by the beliefs returned, both counted as
    distinct beliefs across the two moments so the ratio cannot exceed one, and
    `snapshot_share`
    divides the labels the snapshot recalled, including those the in-turn
    recall also found, by the labels recalled at all.  A ratio with a zero
    denominator is reported as "n/a" rather than as zero.
    """

    return {
        "formation_precision": _ratio(metrics.supported_beliefs, metrics.formed_beliefs),
        "formation_recall": _ratio(metrics.supported_beliefs, metrics.expected_beliefs),
        "end_to_end_recall": _ratio(metrics.needed_recalled, metrics.needed_total),
        "retrieval_recall_given_formed": _ratio(metrics.needed_recalled, metrics.needed_formed),
        "noise_ratio": _ratio(metrics.noise_total, metrics.returned_total),
        "snapshot_share": _ratio(
            metrics.recalled_snapshot_only + metrics.recalled_both, metrics.needed_recalled
        ),
    }


def _ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return UNDEFINED_RATIO
    return str((Decimal(numerator) / Decimal(denominator)).quantize(_RATIO_PLACES))


def baseline_probe_rows(result: DeterministicBenchmarkResult) -> list[BaselineProbeRow]:
    """Flatten a run's probes into the rows a baseline records."""

    return [
        BaselineProbeRow(
            scenario_id=scenario.scenario_id,
            probe_id=probe.probe_id,
            category=probe.category,
            needed_total=probe.needed_total,
            needed_formed=probe.needed_formed,
            needed_recalled=probe.needed_recalled,
            returned_total=probe.returned_total,
            noise_total=probe.noise_total,
            forbidden_rendered=probe.forbidden_rendered,
            distinct_prefixes=probe.distinct_prefixes,
        )
        for scenario in result.scenarios
        for probe in scenario.probes
    ]


def compare_to_baseline(
    result: DeterministicBenchmarkResult, baseline: MemoryBenchmarkBaseline
) -> BaselineComparison:
    """Sort every difference from the baseline into drift, regression, or gain.

    Drift means the baseline is not comparable at all: the corpus digest, a
    version, the extractor, or a structural count changed, so the recorded run
    measured something else.  A regression is a higher-is-better count that
    fell, a lower-is-better count that rose, or a probe that recalls fewer of
    the labels it needs; an improvement is each of those moving the other way.
    A shift is a move inside the attribution partition, which is neither.
    """

    drift: list[str] = []
    regressions: list[str] = []
    improvements: list[str] = []
    shifts: list[str] = []
    for field in _DRIFT_IDENTITY:
        recorded, observed = getattr(baseline, field), getattr(result, field)
        if recorded != observed:
            drift.append(_entry(field, "drifted", recorded, observed))
    for field in _DRIFT_COUNTS:
        recorded, observed = getattr(baseline.metrics, field), getattr(result.metrics, field)
        if recorded != observed:
            drift.append(_entry(field, "drifted", recorded, observed))
    for field in _HIGHER_IS_BETTER:
        recorded, observed = getattr(baseline.metrics, field), getattr(result.metrics, field)
        if observed < recorded:
            regressions.append(_entry(field, "regressed", recorded, observed))
        elif observed > recorded:
            improvements.append(_entry(field, "improved", recorded, observed))
    for field in _LOWER_IS_BETTER:
        recorded, observed = getattr(baseline.metrics, field), getattr(result.metrics, field)
        if observed > recorded:
            regressions.append(_entry(field, "regressed", recorded, observed))
        elif observed < recorded:
            improvements.append(_entry(field, "improved", recorded, observed))
    distances = sorted(
        {*baseline.metrics.recall_by_distance, *result.metrics.recall_by_distance}, key=int
    )
    for distance in distances:
        recorded_bin = baseline.metrics.recall_by_distance.get(distance)
        observed_bin = result.metrics.recall_by_distance.get(distance)
        recorded_total = 0 if recorded_bin is None else recorded_bin.needed_total
        observed_total = 0 if observed_bin is None else observed_bin.needed_total
        field = f"recall_by_distance[{distance}]"
        # A bin's population is a function of the corpus and the session
        # mapping, so a moved population is drift; only the recall inside a
        # stable population has a good direction.
        if recorded_total != observed_total:
            drift.append(_entry(f"{field} needed_total", "drifted", recorded_total, observed_total))
        recorded_recalled = 0 if recorded_bin is None else recorded_bin.needed_recalled
        observed_recalled = 0 if observed_bin is None else observed_bin.needed_recalled
        if observed_recalled < recorded_recalled:
            regressions.append(
                _entry(
                    f"{field} needed_recalled", "regressed", recorded_recalled, observed_recalled
                )
            )
        elif observed_recalled > recorded_recalled:
            improvements.append(
                _entry(f"{field} needed_recalled", "improved", recorded_recalled, observed_recalled)
            )
    for field in _ATTRIBUTION_PARTITION:
        recorded, observed = getattr(baseline.metrics, field), getattr(result.metrics, field)
        if recorded != observed:
            shifts.append(_entry(field, "shifted", recorded, observed))
    observed_rows = {(row.scenario_id, row.probe_id): row for row in baseline_probe_rows(result)}
    for row in baseline.probes:
        current = observed_rows.get((row.scenario_id, row.probe_id))
        if current is None:
            continue
        field = f"{row.scenario_id}/{row.probe_id} needed_recalled"
        if current.needed_recalled < row.needed_recalled:
            regressions.append(
                _entry(field, "regressed", row.needed_recalled, current.needed_recalled)
            )
        elif current.needed_recalled > row.needed_recalled:
            improvements.append(
                _entry(field, "improved", row.needed_recalled, current.needed_recalled)
            )
    return BaselineComparison(
        drift=drift, regressions=regressions, improvements=improvements, shifts=shifts
    )


def _entry(field: str, kind: str, recorded: object, observed: object) -> str:
    return f"{field} {kind}: baseline {recorded}, current {observed}"
