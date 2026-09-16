"""Frozen People fixtures and strict, source-linked comparative scoring.

Fixture validation and scored observations are distinct from release evidence.
Only the separately validated PeopleFormationEvidence can activate capture.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.memory import MemoryAuthority, MemoryDerivation


class Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceEvent(Value):
    session: str
    occurred_at: AwareDatetime
    actor: Literal["owner", "assistant", "tool", "third_party"] = "owner"
    text: str = Field(min_length=1, max_length=8192)


class MentionLabel(Value):
    event: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str
    entity: str | None
    resolvable: bool


class OrganizationLabel(Value):
    event: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)
    entity: str = Field(min_length=1)


class FactEvidenceLabel(Value):
    event: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)


class FactLabel(Value):
    authority: MemoryAuthority = MemoryAuthority.USER
    derivation: MemoryDerivation = MemoryDerivation.DIRECT
    subject: str
    predicate: str
    object: str | None = None
    terms: list[str] = Field(min_length=1)
    evidence_events: list[int] = Field(min_length=1)
    evidence_spans: list[FactEvidenceLabel] = Field(default_factory=list)
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    commitment_state: Literal["proposed", "open", "completed", "cancelled", "uncertain"] | None = (
        None
    )
    due_at: AwareDatetime | None = None
    due_precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    source_timezone: str | None = Field(default=None, min_length=1, max_length=64)


class TaskLabel(Value):
    category: Literal[
        "connections", "background", "last_interaction", "commitments", "changes", "evidence"
    ] = "background"
    question: str
    entity: str
    evidence_events: list[int] = Field(min_length=1)
    terms: list[str] = Field(min_length=1)
    as_of: AwareDatetime | None = None
    known_at: AwareDatetime | None = None


class PeopleCase(Value):
    id: str
    family: str
    collision: bool
    events: list[SourceEvent] = Field(min_length=2, max_length=100)
    mentions: list[MentionLabel] = Field(min_length=1)
    organizations: list[OrganizationLabel] = Field(default_factory=list)
    facts: list[FactLabel]
    tasks: list[TaskLabel]

    @model_validator(mode="after")
    def evidence_exists(self) -> PeopleCase:
        keys = set()
        for mention in self.mentions:
            if mention.event >= len(self.events):
                raise ValueError("mention refers to an absent source")
            if self.events[mention.event].text[mention.start : mention.end] != mention.text:
                raise ValueError("mention must label an exact source span")
            key = (mention.event, mention.start, mention.end)
            if key in keys:
                raise ValueError("duplicate labeled mention")
            keys.add(key)
            if mention.resolvable and mention.entity is None:
                raise ValueError("resolvable mentions need a gold identity")
        items: list[FactLabel | TaskLabel] = [*self.facts, *self.tasks]
        for organization in self.organizations:
            if (
                organization.event >= len(self.events)
                or self.events[organization.event].text[organization.start : organization.end]
                != organization.text
            ):
                raise ValueError("organization must label an exact source span")
            if organization.entity in {mention.entity for mention in self.mentions} | {"owner"}:
                raise ValueError("person and organization labels must be distinct")
        for item in items:
            if any(index >= len(self.events) or index < 0 for index in item.evidence_events):
                raise ValueError("expectation refers to an absent source")
        for fact in self.facts:
            for span in fact.evidence_spans:
                if (
                    span.event not in fact.evidence_events
                    or self.events[span.event].text[span.start : span.end] != span.text
                ):
                    raise ValueError("fact evidence must label an exact source span")
        return self


class Corpus(Value):
    schema_version: Literal[1]
    split: Literal["development", "holdout"]
    provenance: Literal["synthetic-authored-fixtures"]
    review_status: Literal["unreviewed", "reviewed"]
    cases: list[PeopleCase]

    @model_validator(mode="after")
    def unique_cases(self) -> Corpus:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case identifiers must be unique")
        if self.review_status == "reviewed":
            if any(
                {span.event for span in fact.evidence_spans} != set(fact.evidence_events)
                for case in self.cases
                for fact in case.facts
            ):
                raise ValueError("reviewed labels require exact fact evidence for every source")
            if {task.category for case in self.cases for task in case.tasks} != {
                "connections",
                "background",
                "last_interaction",
                "commitments",
                "changes",
                "evidence",
            }:
                raise ValueError("reviewed labels must cover all six product questions")
        return self


def load_corpora(root: Path) -> tuple[Corpus, Corpus, dict[str, str]]:
    corpora = []
    digests: dict[str, str] = {}
    for split, filename in (
        ("development", "people.v1.json"),
        ("holdout", "people.v1-holdout.json"),
    ):
        path = root / "evals/capability" / filename
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if path.with_suffix(".sha256").read_text().strip() != digest:
            raise ValueError(f"frozen {split} corpus digest changed")
        corpus = Corpus.model_validate_json(raw)
        if corpus.split != split:
            raise ValueError("corpus split does not match its path")
        corpora.append(corpus)
        digests[split] = digest
    development, holdout = corpora
    if {case.id for case in development.cases} & {case.id for case in holdout.cases}:
        raise ValueError("holdout overlaps development")
    if {event.text for case in development.cases for event in case.events} & {
        event.text for case in holdout.cases for event in case.events
    }:
        raise ValueError("holdout source text overlaps development")
    if len(development.cases) < 120 or len(holdout.cases) < 60:
        raise ValueError("People corpus has too few scenarios")
    if sum(len(case.mentions) for corpus in corpora for case in corpus.cases) < 1000:
        raise ValueError("People corpus has too few labeled mentions")
    if sum(case.collision for corpus in corpora for case in corpus.cases) < 100:
        raise ValueError("People corpus has too few collision cases")
    return development, holdout, digests


def corpus_report(root: Path) -> dict[str, object]:
    development, holdout, digests = load_corpora(root)
    cases = [*development.cases, *holdout.cases]
    return {
        "scorer_version": "people-scorer@1",
        "development_scenarios": len(development.cases),
        "holdout_scenarios": len(holdout.cases),
        "labeled_mentions": sum(len(case.mentions) for case in cases),
        "collision_cases": sum(case.collision for case in cases),
        "product_questions": dict(Counter(task.category for case in cases for task in case.tasks)),
        "labeled_facts": sum(len(case.facts) for case in cases),
        "corpus_sha256": digests,
        "review_status": [development.review_status, holdout.review_status],
        "provider_calls": 0,
        "activation_evidence": False,
    }


class ObservedMention(Value):
    event: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    person_id: str | None


class ObservedFact(Value):
    authority: MemoryAuthority | None = None
    derivation: MemoryDerivation | None = None
    subject_id: str
    predicate: str
    object_id: str | None = None
    statement: str
    evidence_events: list[int]
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    commitment_state: Literal["proposed", "open", "completed", "cancelled", "uncertain"] | None = (
        None
    )
    due_at: AwareDatetime | None = None
    due_precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    source_timezone: str | None = Field(default=None, min_length=1, max_length=64)


class ObservedOrganization(Value):
    organization_id: str
    display_name: str
    evidence_events: list[int]


class ObservedTask(Value):
    index: int = Field(ge=0)
    answer: str
    evidence_events: list[int]
    retrieved_evidence_events: list[int]
    unsupported_citations: int = Field(default=0, ge=0)


class Observation(Value):
    case_id: str
    pipeline: Literal["current-memory", "identity-links", "full-people"]
    repeat: int = Field(ge=0)
    mentions: list[ObservedMention]
    organizations: list[ObservedOrganization] = Field(default_factory=list)
    facts: list[ObservedFact]
    tasks: list[ObservedTask]
    provider_calls: int = Field(ge=0)
    formation_calls: int = Field(default=0, ge=0)
    formation_segments: int = Field(default=0, ge=0)
    formation_failures: int = Field(default=0, ge=0)
    cost_usd: str = Field(pattern=r"^\d+(?:\.\d+)?$")


def score(case: PeopleCase, observation: Observation) -> dict[str, int | float]:
    if observation.case_id != case.id:
        raise ValueError("observation belongs to another scenario")
    labels = {(m.event, m.start, m.end): m for m in case.mentions}
    observed = {(m.event, m.start, m.end): m for m in observation.mentions}
    if len(observed) != len(observation.mentions):
        raise ValueError("duplicate observation span")
    identities: dict[str, set[str | None]] = {}
    wrong = 0
    predicted = 0
    for key, mention in observed.items():
        if mention.person_id is None:
            continue
        predicted += 1
        label = labels.get(key)
        if label is None or not label.resolvable:
            wrong += 1
        identities.setdefault(mention.person_id, set()).add(
            label.entity if label is not None and label.resolvable else None
        )
    collision_ids = {
        identity for identity, entities in identities.items() if len(entities - {None}) > 1
    }
    wrong += sum(
        m.person_id in collision_ids
        for key, m in observed.items()
        if key in labels and labels[key].resolvable
    )
    mapped = {
        identity: next(iter(entities))
        for identity, entities in identities.items()
        if len(entities) == 1 and None not in entities
    }
    organization_ids: set[str] = set()
    for organization in observation.organizations:
        if (
            organization.organization_id in organization_ids
            or organization.organization_id in identities
        ):
            raise ValueError("duplicate or conflicting observed organization identity")
        organization_ids.add(organization.organization_id)
        organization_candidates = {
            label.entity
            for label in case.organizations
            if label.text.casefold() == organization.display_name.casefold()
            and label.event in organization.evidence_events
        }
        if len(organization_candidates) == 1:
            mapped[organization.organization_id] = next(iter(organization_candidates))
    expected_pairs = 0
    correct_pairs = 0
    eligible = [m for m in case.mentions if m.resolvable]
    for i, left in enumerate(eligible):
        for right in eligible[i + 1 :]:
            if left.entity != right.entity:
                continue
            expected_pairs += 1
            a = observed.get((left.event, left.start, left.end))
            b = observed.get((right.event, right.start, right.end))
            correct_pairs += int(
                a is not None
                and b is not None
                and a.person_id is not None
                and a.person_id == b.person_id
                and a.person_id not in collision_ids
            )
    # One canonical predicted identity per gold person prevents fragmented
    # one-off identities from receiving perfect recall; singleton people count.
    assigned: dict[str, Counter[str]] = {}
    for eligible_mention in eligible:
        actual_eligible_mention = observed.get(
            (eligible_mention.event, eligible_mention.start, eligible_mention.end)
        )
        identity = None if actual_eligible_mention is None else actual_eligible_mention.person_id
        if identity is not None and mapped.get(identity) == eligible_mention.entity:
            assert eligible_mention.entity is not None
            assigned.setdefault(eligible_mention.entity, Counter())[identity] += 1
    correct_decisions = sum(max(counts.values()) for counts in assigned.values())
    matched: set[int] = set()
    correct_facts = direction_correct = attribution_correct = temporal_correct = 0
    for actual in observation.facts:
        candidates = []
        for index, expected in enumerate(case.facts):
            if index in matched or not (
                actual.predicate == expected.predicate
                and all(term.casefold() in actual.statement.casefold() for term in expected.terms)
                and set(expected.evidence_events) <= set(actual.evidence_events)
            ):
                continue
            direction = (
                mapped.get(actual.subject_id, "owner" if actual.subject_id == "owner" else None)
                == expected.subject
                and (
                    mapped.get(actual.object_id, "owner" if actual.object_id == "owner" else None)
                    if actual.object_id
                    else None
                )
                == expected.object
            )
            attribution = (actual.authority, actual.derivation) == (
                expected.authority,
                expected.derivation,
            )
            temporal = (
                actual.valid_from,
                actual.valid_to,
                actual.precision,
                actual.source_timezone,
            ) == (
                expected.valid_from,
                expected.valid_to,
                expected.precision,
                expected.source_timezone,
            )
            if expected.predicate == "commitment":
                temporal = (
                    temporal
                    and expected.commitment_state is not None
                    and (
                        actual.commitment_state,
                        actual.due_at,
                        actual.due_precision,
                        actual.source_timezone,
                    )
                    == (
                        expected.commitment_state,
                        expected.due_at,
                        expected.due_precision,
                        expected.source_timezone,
                    )
                )
            candidates.append(
                (
                    int(direction) + int(attribution) + int(temporal),
                    -index,
                    direction,
                    attribution,
                    temporal,
                )
            )
        if candidates:
            _, negative_index, direction, attribution, temporal = max(candidates)
            matched.add(-negative_index)
            direction_correct += int(direction)
            attribution_correct += int(attribution)
            temporal_correct += int(temporal)
            correct_facts += int(direction and attribution and temporal)
    dimension_count = max(len(case.facts), len(observation.facts))
    task_map = {task.index: task for task in observation.tasks}
    if len(task_map) != len(observation.tasks):
        raise ValueError("duplicate task observation")
    task_success = evidence_hits = evidence_expected = 0
    for index, task in enumerate(case.tasks):
        task_actual = task_map.get(index)
        expected_events = set(task.evidence_events)
        hits = (
            0
            if task_actual is None
            else len(expected_events & set(task_actual.retrieved_evidence_events))
        )
        evidence_hits += hits
        evidence_expected += len(expected_events)
        task_success += int(
            task_actual is not None
            and task_actual.unsupported_citations == 0
            and hits == len(expected_events)
            and expected_events <= set(task_actual.evidence_events)
            and all(term.casefold() in task_actual.answer.casefold() for term in task.terms)
        )
    return {
        "identity_decisions": predicted,
        "identity_errors": wrong,
        "identity_precision": (predicted - wrong) / predicted if predicted else 0,
        "resolvable_identity_recall": correct_decisions / len(eligible) if eligible else 1,
        "identity_pair_recall": correct_pairs / expected_pairs if expected_pairs else 1,
        "resolvable_mentions": len(eligible),
        "correct_identity_mentions": correct_decisions,
        "collision_false_merges": len(collision_ids),
        "direct_fact_recall": correct_facts / len(case.facts) if case.facts else 1,
        "direct_fact_precision": correct_facts / len(observation.facts)
        if observation.facts
        else int(not case.facts),
        "direction_accuracy": direction_correct / dimension_count if dimension_count else 1,
        "attribution_accuracy": attribution_correct / dimension_count if dimension_count else 1,
        "temporal_accuracy": temporal_correct / dimension_count if dimension_count else 1,
        "direction_correct": direction_correct,
        "attribution_correct": attribution_correct,
        "temporal_correct": temporal_correct,
        "fact_dimension_count": dimension_count,
        "expected_facts": len(case.facts),
        "observed_facts": len(observation.facts),
        "correct_facts": correct_facts,
        "tasks_passed": task_success,
        "tasks_count": len(case.tasks),
        "evidence_hits": evidence_hits,
        "evidence_expected": evidence_expected,
        "task_success": task_success / len(case.tasks) if case.tasks else 1,
        "retrieval_evidence_recall": evidence_hits / evidence_expected if evidence_expected else 1,
        "abstentions": sum(
            key not in observed or observed[key].person_id is None for key in labels
        ),
    }


def aggregate_scores(
    cases: dict[str, PeopleCase], observations: list[Observation]
) -> dict[str, int | float | str | dict[str, int]]:
    scored = [score(cases[row.case_id], row) for row in observations]

    def total(key: str) -> int:
        return sum(int(row[key]) for row in scored)

    def ratio(numerator: str, denominator: str, empty: float = 1) -> float:
        return total(numerator) / total(denominator) if total(denominator) else empty

    decisions = total("identity_decisions")
    return {
        "cases": len(observations),
        "families": dict(Counter(cases[row.case_id].family for row in observations)),
        "collision_cases": sum(cases[row.case_id].collision for row in observations),
        "identity_precision": (decisions - total("identity_errors")) / decisions
        if decisions
        else 0,
        "resolvable_identity_recall": ratio("correct_identity_mentions", "resolvable_mentions"),
        "collision_false_merges": total("collision_false_merges"),
        "direct_fact_recall": ratio("correct_facts", "expected_facts"),
        "direct_fact_precision": ratio(
            "correct_facts", "observed_facts", float(not total("expected_facts"))
        ),
        "direction_accuracy": ratio("direction_correct", "fact_dimension_count"),
        "attribution_accuracy": ratio("attribution_correct", "fact_dimension_count"),
        "temporal_accuracy": ratio("temporal_correct", "fact_dimension_count"),
        "task_success": ratio("tasks_passed", "tasks_count"),
        "retrieval_evidence_recall": ratio("evidence_hits", "evidence_expected"),
        "abstentions": total("abstentions"),
        "unambiguous_decisions": total("correct_identity_mentions"),
        "provider_calls": sum(row.provider_calls for row in observations),
        "formation_calls": sum(row.formation_calls for row in observations),
        "formation_segments": sum(row.formation_segments for row in observations),
        "formation_failures": sum(row.formation_failures for row in observations),
        "formation_call_mismatches": sum(
            row.formation_calls != 3 * row.formation_segments for row in observations
        ),
        "cost_usd": format(sum((Decimal(row.cost_usd) for row in observations), Decimal(0)), "f"),
    }


def paired_interval(differences: list[float], *, samples: int = 10000) -> tuple[float, float]:
    if not differences or samples < 1000:
        raise ValueError("paired interval needs measured cases and at least 1000 resamples")
    # Counter-derived draws make the resampling reproducible without ambient RNG state.
    count = len(differences)
    means = sorted(
        sum(
            differences[
                int.from_bytes(
                    hashlib.sha256(f"people-bootstrap@1:{sample}:{draw}".encode()).digest()[:8]
                )
                % count
            ]
            for draw in range(count)
        )
        / count
        for sample in range(samples)
    )
    return means[int(samples * 0.025)], means[int(samples * 0.975)]


def score_observations(root: Path, path: Path) -> dict[str, object]:
    development, holdout, digests = load_corpora(root)
    cases = {case.id: case for case in [*development.cases, *holdout.cases]}
    observations = [Observation.model_validate(row) for row in json.loads(path.read_text())]
    keys = {(row.case_id, row.pipeline, row.repeat) for row in observations}
    if len(keys) != len(observations):
        raise ValueError("duplicate observation")
    repeats = {row.repeat for row in observations}
    required = {
        (case, pipeline, repeat)
        for case in cases
        for repeat in repeats
        for pipeline in ("current-memory", "identity-links", "full-people")
    }
    if len(repeats) < 3 or keys != required:
        raise ValueError(
            "three complete paired repeats across all pipelines and both corpora are required"
        )
    scores = {
        (row.case_id, row.pipeline, row.repeat): score(cases[row.case_id], row)
        for row in observations
    }
    differences = [
        sum(
            scores[(case, "full-people", repeat)]["task_success"]
            - scores[(case, "current-memory", repeat)]["task_success"]
            for repeat in repeats
        )
        / len(repeats)
        for case in cases
    ]
    return {
        "scorer_version": "people-scorer@1",
        "corpus_sha256": digests,
        "repeats": len(repeats),
        "paired_sampling_unit": "scenario_mean_across_repeats",
        "paired_improvement_ci95": paired_interval(differences),
        "summaries": [
            {
                "split": corpus.split,
                "pipeline": pipeline,
                "repeat": repeat,
                **aggregate_scores(
                    cases,
                    [
                        row
                        for row in observations
                        if row.repeat == repeat
                        and row.pipeline == pipeline
                        and row.case_id in {case.id for case in corpus.cases}
                    ],
                ),
            }
            for corpus in (development, holdout)
            for pipeline in ("current-memory", "identity-links", "full-people")
            for repeat in sorted(repeats)
        ],
        "scores": [
            {"case_id": key[0], "pipeline": key[1], "repeat": key[2], **value}
            for key, value in scores.items()
        ],
        "activation_evidence": False,
    }
