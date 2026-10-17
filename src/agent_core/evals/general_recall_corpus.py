"""Isolated M33 histories and mechanically scored answer/task fixtures."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_core.context.working_state import WorkingStateUpdate
from agent_core.domain.general_recall_release import Action, Family
from agent_core.domain.memory import (
    BeliefType,
    MemoryClaimKind,
    MemoryStatus,
    Portability,
    Sensitivity,
)
from agent_core.evals.general_memory_recall import FAMILIES
from agent_core.memory.general_recall_evidence import _unique_object


class FixtureValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HistoryTurn(FixtureValue):
    id: str = Field(min_length=1, max_length=128)
    session: str = Field(min_length=1, max_length=128)
    at: AwareDatetime
    content: str = Field(min_length=1, max_length=8192)
    scope: str = "general"
    speaker: Literal["owner", "assistant", "external", "other_owner"] = "owner"


class TaskInput(FixtureValue):
    question: str = Field(min_length=1, max_length=8192)
    at: AwareDatetime
    scope: str = "general"
    # Names and descriptions specify the requested answer or proposed tool
    # arguments. They never contain expected values or relevant-memory labels.
    output_fields: dict[str, str] = Field(min_length=1, max_length=20)
    output_kind: Literal["answer", "proposed_tool"] = "answer"
    # Authored preceding task state, separate from source histories and gold.
    context_update: WorkingStateUpdate | None = None


class EvidenceFixture(FixtureValue):
    """Gold/seed state, never supplied to query planning or the answer reader."""

    id: str = Field(min_length=1, max_length=128)
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=512)
    statement: str = Field(min_length=1, max_length=8192)
    equivalent_subjects: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = Field(
        default=(), max_length=20
    )
    equivalent_statements: tuple[Annotated[str, Field(min_length=1, max_length=8192)], ...] = Field(
        default=(), max_length=20
    )
    belief_type: BeliefType = BeliefType.FACT
    claim_kind: MemoryClaimKind = MemoryClaimKind.PROJECT_FACT
    status: MemoryStatus = MemoryStatus.ACTIVE
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    portability: Portability = Portability.CONTEXTUAL
    required: bool = True
    invalid_current: bool = False
    invalid_answer_values: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    valid_to: AwareDatetime | None = None
    superseded_by: str | None = None


class RecallFixture(FixtureValue):
    id: str = Field(min_length=1, max_length=128)
    family: Family
    split: Literal["development", "holdout"]
    scenario_lineage: str = Field(min_length=1, max_length=128)
    query_lineage: str = Field(min_length=1, max_length=128)
    implicit_task: bool
    known_incident: bool = False
    history: tuple[HistoryTurn, ...] = Field(min_length=1, max_length=100)
    task: TaskInput
    evidence: tuple[EvidenceFixture, ...] = Field(default=(), max_length=100)
    expected_action: Action
    # Each slot accepts explicitly authored equivalent values. Empty means null.
    expected_values: dict[str, tuple[str, ...]]

    def reader_input(self) -> dict[str, object]:
        return self.task.model_dump(mode="json")


def validate_corpus(cases: tuple[RecallFixture, ...], *, release: bool = False) -> None:
    cases = tuple(RecallFixture.model_validate(case.model_dump()) for case in cases)
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Empty corpus or duplicate scenario id")
    if len({case.scenario_lineage for case in cases}) != len(cases):
        raise ValueError("Scenario variants cannot inflate the independent census")
    for case in cases:
        if (
            len({turn.id for turn in case.history}) != len(case.history)
            or tuple(sorted(case.history, key=lambda turn: turn.at)) != case.history
            or any(turn.at >= case.task.at for turn in case.history)
        ):
            raise ValueError("Source history requires unique ids and ordered earlier events")
        if not case.task.output_fields or set(case.task.output_fields) != set(case.expected_values):
            raise ValueError("Expected values must label exactly the requested output fields")
        if any(not value.strip() for values in case.expected_values.values() for value in values):
            raise ValueError("Expected aliases cannot be empty")
        if case.expected_action == "answer" and not any(case.expected_values.values()):
            raise ValueError("An answer case requires at least one mechanically scored value")
        source_ids = {turn.id for turn in case.history}
        if (
            len({label.id for label in case.evidence}) != len(case.evidence)
            or any(not set(label.source_ids) <= source_ids for label in case.evidence)
            or any(len(set(label.source_ids)) != len(label.source_ids) for label in case.evidence)
            or any(label.required and label.invalid_current for label in case.evidence)
        ):
            raise ValueError("Invalid or duplicate evidence provenance")
        evidence_ids = {label.id for label in case.evidence}
        by_id = {label.id: label for label in case.evidence}
        sources = {turn.id: turn for turn in case.history}
        for label in case.evidence:
            if label.status is MemoryStatus.SUPERSEDED and (
                label.valid_to is None
                or label.superseded_by not in evidence_ids
                or label.superseded_by == label.id
            ):
                raise ValueError("A superseded seed requires a dated replacement")
            start = max(sources[key].at for key in label.source_ids)
            if label.valid_to is not None and not start <= label.valid_to <= case.task.at:
                raise ValueError("Seed validity closure lies outside its observed interval")
            if label.superseded_by is not None:
                if label.superseded_by not in by_id or label.valid_to is None:
                    raise ValueError("A replacement requires a known successor and closure")
                successor = by_id[label.superseded_by]
                if max(sources[key].at for key in successor.source_ids) < label.valid_to:
                    raise ValueError("Replacement source precedes the declared closure")
                visited = {label.id}
                while successor.superseded_by is not None:
                    if successor.id in visited or successor.superseded_by not in by_id:
                        raise ValueError("Replacement chain is cyclic or incomplete")
                    visited.add(successor.id)
                    successor = by_id[successor.superseded_by]
            if label.invalid_answer_values and (
                not label.invalid_current
                or not set(label.invalid_answer_values) <= set(case.task.output_fields)
                or any(
                    not values or any(not value.strip() for value in values)
                    for values in label.invalid_answer_values.values()
                )
            ):
                raise ValueError("Invalid-current labels require declared output slots and values")
    for key in ("query_lineage", "history"):

        def identity(case: RecallFixture, field: str = key) -> str:
            if field == "query_lineage":
                return case.query_lineage
            return hashlib.sha256(
                json.dumps([(t.speaker, t.scope, t.content) for t in case.history]).encode()
            ).hexdigest()

        development = {identity(c) for c in cases if c.split == "development"}
        holdout = {identity(c) for c in cases if c.split == "holdout"}
        if development & holdout:
            raise ValueError("Development and holdout lineage overlap")
    if release:
        counts = Counter(case.family for case in cases)
        if len(cases) < 400 or set(counts) != FAMILIES or min(counts.values()) < 50:
            raise ValueError("Release requires 400 independent scenarios and 50 per family")
        if not any(case.known_incident for case in cases):
            raise ValueError("Release requires known incident regressions")
        for family in FAMILIES:
            if {c.split for c in cases if c.family == family} != {"development", "holdout"}:
                raise ValueError("Each family needs development and holdout observations")
            for split in ("development", "holdout"):
                if (
                    sum(c.implicit_task for c in cases if c.family == family and c.split == split)
                    < 2
                ):
                    raise ValueError("Each family and split needs at least two implicit tasks")


def load_corpus(path: Path, *, release: bool = False) -> tuple[RecallFixture, ...]:
    cases = tuple(
        RecallFixture.model_validate(json.loads(line, object_pairs_hook=_unique_object))
        for line in path.read_text().splitlines()
        if line.strip()
    )
    validate_corpus(cases, release=release)
    return cases


def corpus_digest(cases: tuple[RecallFixture, ...]) -> str:
    return hashlib.sha256(
        b"".join(case.model_dump_json().encode() + b"\n" for case in cases)
    ).hexdigest()


class ReaderAnswer(FixtureValue):
    action: Action
    values: dict[str, str | None]


class TaskScore(FixtureValue):
    correct: bool
    answer_atoms: tuple[str, ...]
    unsupported_atoms: tuple[str, ...]


def score_answer(case: RecallFixture, raw: str) -> TaskScore:
    try:
        answer = ReaderAnswer.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
    except (ValueError, TypeError):
        return TaskScore(correct=False, answer_atoms=(), unsupported_atoms=("malformed_output",))
    found, unsupported = [], []
    if set(case.expected_values) - set(answer.values):
        unsupported.append("missing_output_fields")
    for key, value in answer.values.items():
        if key not in case.expected_values:
            unsupported.append(key)
        elif value is not None:
            normalized = " ".join(value.casefold().split())
            if normalized in {" ".join(v.casefold().split()) for v in case.expected_values[key]}:
                found.append(key)
            else:
                unsupported.append(key)
    expected = {key for key, values in case.expected_values.items() if values}
    return TaskScore(
        correct=answer.action == case.expected_action
        and set(answer.values) == set(case.expected_values)
        and set(found) == expected
        and not unsupported,
        answer_atoms=tuple(sorted(found)),
        unsupported_atoms=tuple(sorted(unsupported)),
    )
