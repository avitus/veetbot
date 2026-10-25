"""Owner recall probes: where an expected belief stops on its way to an answer.

A probe is a fabricated or owner-private analogue of a real retrieval
incident. The driver replays it through the composed production run loop and
reports, per expected belief, the furthest stage it reached:

    Saved -> Stored -> Eligible -> Candidate -> Ranked -> Packed -> Delivered -> Used

The report carries identifiers, ranks, stages and latencies, never belief
text. It is diagnostic evidence for development; it is not activation or
release evidence, and the loader refuses the frozen Milestone 16 and 33
controls so a probe file can never be mistaken for one.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, model_validator

from agent_core.domain.memory import (
    MemoryRecord,
    MemoryStatus,
    Portability,
    RecallStages,
    RecallTrace,
    Sensitivity,
)
from agent_core.evals.general_memory_recall import FAMILIES

PROBE_REPORT_VERSION: Final = "owner-recall-probes@1"
PROBE_PATH_KINDS = ("automatic", "deliberate")
STAGE_ORDER = (
    "not_stored",
    "stored_but_retired",
    "stored_but_superseded",
    "ineligible",
    "stored",
    "candidate",
    "ranked",
    "packed",
    "delivered",
    "used",
)
_FROZEN_CONTROL_PATTERNS = (
    re.compile(r"evals/capability/general-recall(/|$)"),
    re.compile(r"evals/capability/memory-benchmark"),
    re.compile(r"evals/observations(/|$)"),
)
_SHORT_ID = re.compile(r"\[m:([0-9a-f]{8})\]")

PathKind = Literal["automatic", "deliberate"]


class ExpectedEvidence(BaseModel):
    """One belief the answer needs, named by identifier or by a text it contains."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    belief_id: UUID | None = None
    statement_contains: str | None = Field(default=None, min_length=1)
    required: bool = True

    @model_validator(mode="after")
    def one_locator(self) -> ExpectedEvidence:
        if (self.belief_id is None) == (self.statement_contains is None):
            raise ValueError(
                "expected evidence names exactly one of belief_id or statement_contains"
            )
        return self


class ForbiddenEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    statement_contains: str = Field(min_length=1)


class WriteProbe(BaseModel):
    """The Saved stage: an owner save proposed after web results in the same turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    statement: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    scope: str = "general"
    web_results: bool = True


class Probe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    incident: str = Field(min_length=1)
    family: str
    question: str = Field(min_length=1)
    search_text: str | None = None
    scope: str = "general"
    paths: tuple[PathKind, ...] = ("automatic", "deliberate")
    expected: tuple[ExpectedEvidence, ...] = ()
    min_required_rendered: NonNegativeInt = 0
    forbidden: tuple[ForbiddenEvidence, ...] = ()
    answer_must_contain: tuple[str, ...] = ()
    answer_must_not_contain: tuple[str, ...] = ()
    latency_ceiling_ms: int | None = Field(default=None, gt=0)
    write: WriteProbe | None = None
    notes: str = ""

    @model_validator(mode="after")
    def known_family(self) -> Probe:
        if self.family not in FAMILIES:
            raise ValueError(f"unknown probe family {self.family!r}")
        if len(set(self.paths)) != len(self.paths):
            raise ValueError("probe paths repeat")
        return self


class LoadedProbes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    probes: tuple[Probe, ...]


def load_probes(path: Path) -> LoadedProbes:
    """Read one probe per JSONL line; refuse the frozen evaluation controls."""

    resolved = path.resolve()
    posix = resolved.as_posix()
    if any(pattern.search(posix) for pattern in _FROZEN_CONTROL_PATTERNS):
        raise ValueError(f"{path} is a frozen evaluation control, not a probe file")
    raw = resolved.read_bytes()
    probes: list[Probe] = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            probes.append(Probe.model_validate(json.loads(line)))
        except ValueError as error:
            raise ValueError(f"{path}:{number}: {error}") from error
    identifiers = [probe.id for probe in probes]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"{path}: probe identifiers repeat")
    if not probes:
        raise ValueError(f"{path}: no probes")
    return LoadedProbes(path=posix, digest=hashlib.sha256(raw).hexdigest(), probes=tuple(probes))


def evidence_stage(
    record: MemoryRecord | None,
    stages: RecallStages | None,
    trace: RecallTrace | None,
    *,
    delivered: set[UUID],
    answer: str | None,
    ceiling: Sensitivity = Sensitivity.RESTRICTED,
) -> str:
    """Name the furthest stage one expected belief reached on one path.

    The stages are read from what the run left behind: the store for Stored
    and Eligible, the retriever's transient stage identifiers for Candidate
    and Ranked, the trace for Packed, the runtime's delivery record for
    Delivered, and the answer's citation form for Used.
    """

    if record is None:
        return "not_stored"
    if record.status in {MemoryStatus.RETIRED, MemoryStatus.EXPIRED}:
        return "stored_but_retired"
    if record.status is MemoryStatus.SUPERSEDED:
        return "stored_but_superseded"
    order = [
        Sensitivity.PUBLIC,
        Sensitivity.INTERNAL,
        Sensitivity.SENSITIVE,
        Sensitivity.RESTRICTED,
    ]
    if order.index(record.sensitivity) > order.index(ceiling):
        return "ineligible:sensitivity"
    if trace is not None:
        query = trace.query
        if (
            record.portability is Portability.LOCAL
            and record.scope != query.current_scope
            and record.subject.casefold() not in {subject.casefold() for subject in query.subjects}
        ):
            return "ineligible:portability"
        if record.expires_at is not None and record.expires_at <= trace.created_at:
            return "ineligible:expired"
    if stages is None or record.id not in stages.candidate_ids:
        return "stored"
    if record.id not in stages.ranked_ids:
        return "candidate"
    if trace is None or record.id not in trace.returned:
        return "ranked"
    if record.id not in delivered:
        return "packed"
    if answer is not None and str(record.id)[:8] in set(_SHORT_ID.findall(answer)):
        return "used"
    return "delivered"


def stage_rank(stage: str) -> int:
    name = stage.split(":", 1)[0]
    return STAGE_ORDER.index(name)


class EvidenceObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_index: NonNegativeInt
    required: bool
    belief_id: UUID | None
    stage: str
    rank: int | None = Field(default=None, ge=1)


class PathObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: PathKind
    run_id: UUID | None
    run_status: str
    trace_ids: tuple[UUID, ...]
    delivered_trace_ids: tuple[UUID, ...]
    arms: tuple[str, ...]
    elapsed_ms: NonNegativeInt
    recall_latency_ms: NonNegativeInt
    within_latency_ceiling: bool | None
    evidence: tuple[EvidenceObservation, ...]
    required_delivered: NonNegativeInt
    forbidden_delivered: NonNegativeInt
    answer_correct: bool | None


class SaveObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: UUID | None
    status: str
    belief_id: UUID | None = None


class ProbeObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    probe_id: str
    incident: str
    family: str
    paths: tuple[PathObservation, ...]
    save: SaveObservation | None = None


class ProbeSummary(BaseModel):
    """Primary outcomes first; citation counts are diagnostics elsewhere."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    probes: NonNegativeInt
    required_evidence: NonNegativeInt
    required_delivered: NonNegativeInt
    facts_missed: NonNegativeInt
    wrong_or_outdated: NonNegativeInt
    answers_checked: NonNegativeInt
    answers_correct: NonNegativeInt
    saves_committed: NonNegativeInt
    saves_refused: NonNegativeInt
    latency_breaches: NonNegativeInt
    stages: dict[str, int]


class ProbeReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal["owner-recall-probes@1"] = PROBE_REPORT_VERSION
    activation_evidence: Literal[False] = False
    release_evidence: Literal[False] = False
    generated_at: datetime
    probe_digest: str | None
    live_model: bool
    implementation: dict[str, str]
    store: dict[str, int]
    probes: tuple[ProbeObservation, ...]
    summary: ProbeSummary


def summarize(observations: tuple[ProbeObservation, ...]) -> ProbeSummary:
    required = delivered = missed = wrong = checked = correct = 0
    committed = refused = breaches = 0
    stages: dict[str, int] = {}
    for observation in observations:
        for path in observation.paths:
            for evidence in path.evidence:
                stages[evidence.stage] = stages.get(evidence.stage, 0) + 1
                if not evidence.required:
                    continue
                required += 1
                if stage_rank(evidence.stage) >= stage_rank("delivered"):
                    delivered += 1
                else:
                    missed += 1
            wrong += path.forbidden_delivered
            if path.answer_correct is not None:
                checked += 1
                correct += int(path.answer_correct)
            if path.within_latency_ceiling is False:
                breaches += 1
        if observation.save is not None:
            if observation.save.status == "committed":
                committed += 1
            elif observation.save.status.startswith("save_refused:"):
                refused += 1
    return ProbeSummary(
        probes=len(observations),
        required_evidence=required,
        required_delivered=delivered,
        facts_missed=missed,
        wrong_or_outdated=wrong,
        answers_checked=checked,
        answers_correct=correct,
        saves_committed=committed,
        saves_refused=refused,
        latency_breaches=breaches,
        stages=dict(sorted(stages.items())),
    )
