"""Frozen M32 evaluation inputs and pure scoring; never activation evidence.

Runtime observations must come from store/trace collection, not provider claims.
This module neither runs reconsolidation nor calls a model or mutates memory.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, fields
from itertools import combinations
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_core.evals.memory_benchmark import ProbeAnswer, score_answer_f1

SCORER_VERSION = "reconsolidation-scorer@1"
CORPUS_PATH = Path("evals/capability/memory-reconsolidation.v1.json")
MANIFEST_PATH = Path("evals/capability/memory-reconsolidation.manifest.json")
CONTROL_PATHS = (
    "evals/capability/memory-benchmark.v1.json",
    "evals/capability/memory-benchmark.baseline.json",
)
SCORER_PATHS = (
    "src/agent_core/evals/memory_reconsolidation.py",
    "src/agent_core/evals/memory_benchmark.py",
)
CATEGORIES = frozenset(
    {
        "equivalent",
        "same_evidence",
        "quantity",
        "time",
        "attribution",
        "negation",
        "scope",
        "connection_focus",
        "connection_travel",
        "summary",
        "expiry",
        "correction",
    }
)


def normalized(text: str) -> str:
    """Whitespace and Unicode composition only; preserve semantic distinctions."""
    return " ".join(unicodedata.normalize("NFC", text).split())


class StrictValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Seed(StrictValue):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    session: str = Field(min_length=1)
    event: int = Field(ge=1)
    evidence_at: AwareDatetime
    scope: str = "user"
    attribution: str = "owner"
    status: Literal["live", "expired", "deleted"] = "live"


class Hypothesis(StrictValue):
    statement: str = Field(min_length=1)
    support: tuple[str, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def distinct_support(self) -> Hypothesis:
        if len(set(self.support)) != len(self.support):
            raise ValueError("hypothesis support must be distinct")
        return self


class Probe(StrictValue):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: ProbeAnswer

    @model_validator(mode="after")
    def exact_or_aliases(self) -> Probe:
        if self.answer.kind not in {"exact", "alternatives"}:
            raise ValueError("M32 probes require exact answers or aliases")
        if any(not value.strip() for value in self.answer.values):
            raise ValueError("empty answer label")
        return self


class Case(StrictValue):
    id: str = Field(min_length=1)
    split: Literal["development", "holdout"]
    category: str
    seeds: tuple[Seed, ...] = Field(min_length=2, max_length=128)
    duplicate_classes: tuple[tuple[str, ...], ...] = ()
    required_survivors: tuple[str, ...]
    hypotheses: tuple[Hypothesis, ...] = ()
    probes: tuple[Probe, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def labels_reference_real_sources(self) -> Case:
        seeds = {seed.id: seed for seed in self.seeds}
        if len(seeds) != len(self.seeds) or self.category not in CATEGORIES:
            raise ValueError("duplicate seed ID or unknown category")
        live = {seed.id for seed in self.seeds if seed.status == "live"}
        if set(self.required_survivors) != live or len(self.required_survivors) != len(live):
            raise ValueError("required survivors must name every live original exactly once")
        seen: set[str] = set()
        for group in self.duplicate_classes:
            if len(group) < 2 or len(set(group)) != len(group) or not set(group) <= live:
                raise ValueError("duplicate class needs distinct live originals")
            if seen.intersection(group):
                raise ValueError("duplicate classes must be disjoint")
            seen.update(group)
            meanings = {
                (
                    normalized(seeds[key].statement),
                    seeds[key].subject,
                    seeds[key].scope,
                    seeds[key].attribution,
                )
                for key in group
            }
            if len(meanings) != 1:
                raise ValueError("duplicate label changes meaning or visibility")
        signatures: set[tuple[str, frozenset[str]]] = set()
        for label in self.hypotheses:
            if not set(label.support) <= live:
                raise ValueError("hypothesis label names missing or invalid sources")
            evidence = {(seeds[key].session, seeds[key].event) for key in label.support}
            if len(evidence) < 2:
                raise ValueError("hypothesis label needs independent original events")
            signature = (normalized(label.statement), frozenset(label.support))
            if signature in signatures:
                raise ValueError("duplicate hypothesis label")
            signatures.add(signature)
        if len({probe.id for probe in self.probes}) != len(self.probes):
            raise ValueError("duplicate probe ID")
        return self


class Corpus(StrictValue):
    schema_version: Literal[1]
    cases: tuple[Case, ...] = Field(min_length=24)

    @model_validator(mode="after")
    def separate_covered_splits(self) -> Corpus:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs overlap")
        for split in ("development", "holdout"):
            if {case.category for case in self.cases if case.split == split} != CATEGORIES:
                raise ValueError("each split must cover every category")
        return self


class Answer(StrictValue):
    probe_id: str
    text: str


class Observation(StrictValue):
    case_id: str
    merges: tuple[tuple[str, ...], ...] = Field(default=(), max_length=8)
    surviving_ids: tuple[str, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = Field(default=(), max_length=8)
    answers: tuple[Answer, ...] = ()


class Manifest(StrictValue):
    schema_version: Literal[1]
    scorer_version: str
    purpose: str
    sha256: dict[str, str]


@dataclass(frozen=True)
class Score:
    false_merge_pairs: int = 0
    lost_facts: int = 0
    duplicate_sets: int = 0
    covered_duplicate_sets: int = 0
    hypothesis_labels: int = 0
    hypothesis_outputs: int = 0
    hypothesis_matches: int = 0
    probes: int = 0
    covered_probes: int = 0
    invalid_observations: int = 0


def load_corpus(root: Path) -> Corpus:
    """Refuse drift in fixtures, scorer or the unchanged M16 control references."""
    manifest = Manifest.model_validate_json((root / MANIFEST_PATH).read_text())
    if manifest.scorer_version != SCORER_VERSION:
        raise ValueError("scorer version mismatch")
    paths = (str(CORPUS_PATH), *CONTROL_PATHS, *SCORER_PATHS)
    if set(manifest.sha256) != set(paths):
        raise ValueError("manifest must pin exactly the corpus, controls and scorer")
    for path in paths:
        digest = hashlib.sha256((root / path).read_bytes()).hexdigest()
        if digest != manifest.sha256[path]:
            raise ValueError(f"frozen evaluation input changed: {path}")
    return Corpus.model_validate_json((root / CORPUS_PATH).read_text())


def score_case(case: Case, observed: Observation) -> Score:
    """Count harmful and useful outcomes without letting omissions improve ratios."""
    if observed.case_id != case.id:
        raise ValueError("observation belongs to another case")
    known = {seed.id for seed in case.seeds}
    invalid = len(set(observed.surviving_ids) - known)
    invalid += len(observed.surviving_ids) - len(set(observed.surviving_ids))
    components: list[set[str]] = []
    for group in observed.merges:
        if len(group) > 32:
            raise ValueError("merge observation exceeds group bound")
        members = set(group)
        invalid += int(len(members) < 2 or len(members) != len(group))
        invalid += len(members - known)
        intersecting = [part for part in components if part.intersection(members)]
        for part in intersecting:
            members.update(part)
            components.remove(part)
        components.append(members)
    allowed_pairs = {
        pair for group in case.duplicate_classes for pair in combinations(sorted(group), 2)
    }
    actual_pairs = {pair for group in components for pair in combinations(sorted(group), 2)}
    available = {
        (normalized(label.statement), frozenset(label.support)) for label in case.hypotheses
    }
    matches = 0
    for hypothesis in observed.hypotheses:
        signature = (normalized(hypothesis.statement), frozenset(hypothesis.support))
        invalid += len(set(hypothesis.support) - known)
        if signature in available:
            available.remove(signature)
            matches += 1
    answers = Counter(answer.probe_id for answer in observed.answers)
    probes = {probe.id: probe for probe in case.probes}
    invalid += sum(count for key, count in answers.items() if key not in probes)
    invalid += sum(count - 1 for key, count in answers.items() if key in probes)
    covered_probes = sum(
        1
        for answer in observed.answers
        if answer.probe_id in probes
        and answers[answer.probe_id] == 1
        and score_answer_f1(answer.text, probes[answer.probe_id].answer) == 1
    )
    return Score(
        false_merge_pairs=len(actual_pairs - allowed_pairs),
        lost_facts=len(set(case.required_survivors) - set(observed.surviving_ids)),
        duplicate_sets=len(case.duplicate_classes),
        covered_duplicate_sets=sum(set(group) in components for group in case.duplicate_classes),
        hypothesis_labels=len(case.hypotheses),
        hypothesis_outputs=len(observed.hypotheses),
        hypothesis_matches=matches,
        probes=len(case.probes),
        covered_probes=covered_probes,
        invalid_observations=invalid,
    )


def score_split(corpus: Corpus, observations: Sequence[Observation], split: str) -> Score:
    """Score a complete split; missing and duplicate runs cannot disappear."""
    if split not in {"development", "holdout"}:
        raise ValueError("unknown split")
    cases = {case.id: case for case in corpus.cases if case.split == split}
    if len({observed.case_id for observed in observations}) != len(observations):
        raise ValueError("observations must be unique")
    if {observed.case_id for observed in observations} != cases.keys():
        raise ValueError("observations must cover exactly the complete split")
    scores = [score_case(cases[observed.case_id], observed) for observed in observations]
    return Score(
        **{
            field.name: sum(getattr(score, field.name) for score in scores)
            for field in fields(Score)
        }
    )
