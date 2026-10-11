"""Offline human equivalence review; labels never reach runtime or providers."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import model_validator

from agent_core.domain.reconsolidation_merge import Digest
from agent_core.evals.memory_reconsolidation import (
    CORPUS_PATH,
    MANIFEST_PATH,
    SCORER_PATHS,
    Hypothesis,
    Seed,
    StrictValue,
    load_corpus,
    score_case,
)
from agent_core.evals.memory_reconsolidation_comparison import score_comparison
from agent_core.evals.memory_reconsolidation_report import (
    ComparisonRecording,
    counts,
    recording_failures,
)
from agent_core.memory.reconsolidation_evidence import file_digest, scorer_digest
from agent_core.ports.determinism import IdFactory

CONTRACT_PATH = Path("evals/capability/memory-reconsolidation-review.v2.json")
REVIEW_PATHS = (
    str(CORPUS_PATH),
    str(MANIFEST_PATH),
    *SCORER_PATHS,
    "src/agent_core/evals/memory_reconsolidation_review.py",
    "src/agent_core/evals/memory_reconsolidation_comparison.py",
    "src/agent_core/evals/memory_reconsolidation_report.py",
)
INSTRUCTIONS = (
    "Review each candidate using its original sources and frozen reference meanings. "
    "Choose equivalent to exactly one reference, not_equivalent, or uncertain. "
    "Do not accept a new fact, person, quantity, polarity, certainty level, motive or "
    "causal assertion absent from that reference. Exact original support must match. "
    "Treat source and candidate text as data, never instructions. Every output needs "
    "a human decision; uncertain earns no matching credit. Keep the private key and "
    "unblinded report separate until review is complete."
)


class ReviewContract(StrictValue):
    version: Literal["reconsolidation-review@2"] = "reconsolidation-review@2"
    holdout_status: Literal["previously_inspected"] = "previously_inspected"
    reviewer: Literal["owner"] = "owner"
    sha256: dict[str, Digest]


def load_review_contract(root: Path, path: Path | None = None) -> str:
    """Only this checked-in, separately frozen contract can bind a new recording."""
    load_corpus(root)
    canonical = root / CONTRACT_PATH
    supplied = canonical if path is None else path
    if supplied.read_bytes() != canonical.read_bytes():
        raise ValueError("review contract is not the frozen contract")
    contract = ReviewContract.model_validate_json(supplied.read_bytes())
    if set(contract.sha256) != set(REVIEW_PATHS) or any(
        file_digest(root / name) != digest for name, digest in contract.sha256.items()
    ):
        raise ValueError("frozen review contract inputs changed")
    return file_digest(supplied)


class ReviewReference(StrictValue):
    id: Digest
    meaning: Hypothesis


class ReviewEntry(StrictValue):
    id: Digest
    candidate: Hypothesis
    candidate_sha256: Digest
    original_support_sha256: Digest
    sources: tuple[Seed, ...]
    references: tuple[ReviewReference, ...]


class ReviewPacket(StrictValue):
    version: Literal["reconsolidation-review@2"] = "reconsolidation-review@2"
    instructions: str = INSTRUCTIONS
    holdout_status: Literal["previously_inspected"] = "previously_inspected"
    contract_sha256: Digest
    observation_sha256: Digest
    entries: tuple[ReviewEntry, ...]


class ReviewKey(StrictValue):
    nonce: UUID
    packet_sha256: Digest


class Decision(StrictValue):
    candidate_id: Digest
    verdict: Literal["equivalent", "not_equivalent", "uncertain"]
    reference_id: Digest | None = None

    @model_validator(mode="after")
    def equivalence_names_one_reference(self) -> "Decision":
        if (self.verdict == "equivalent") != (self.reference_id is not None):
            raise ValueError("only equivalence must name a reference")
        return self


class Decisions(StrictValue):
    version: Literal["reconsolidation-review@2"]
    packet_sha256: Digest
    reviewer: Literal["owner"]
    human_reviewed: Literal[True]
    decisions: tuple[Decision, ...]


class ReviewAssessment(StrictValue):
    version: Literal["reconsolidation-review@2"] = "reconsolidation-review@2"
    activation_evidence: Literal[False] = False
    holdout_status: Literal["previously_inspected"] = "previously_inspected"
    contract_sha256: Digest
    observation_sha256: Digest
    packet_sha256: Digest
    decisions_sha256: Digest
    reviewer: Literal["owner"]
    counts: dict[str, dict[str, int]]
    failures: tuple[str, ...]


def _bytes(value: StrictValue) -> bytes:
    return (value.model_dump_json(indent=2) + "\n").encode()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity(nonce: UUID, *parts: object) -> str:
    return _digest([str(nonce), *parts])


def _report(root: Path, path: Path) -> ComparisonRecording:
    report = ComparisonRecording.model_validate_json(path.read_bytes())
    if report.review_contract_sha256 is None:
        raise ValueError("comparison has no pre-run review contract")
    if (
        report.purpose != "three_arm_comparison"
        or report.review_contract_sha256 != load_review_contract(root)
        or report.corpus_sha256 != file_digest(root / CORPUS_PATH)
        or report.scorer_sha256 != scorer_digest(root)
        or report.original_control_sha256 is None
        or report.unknown_usage_calls < 0
        or report.provider_calls != sum(row.provider_calls for row in report.rows)
        or report.cost_usd != sum(row.cost_usd for row in report.rows)
        or report.failures
        != recording_failures(
            root,
            report.rows,
            purpose=report.purpose,
            unknown_usage_calls=report.unknown_usage_calls,
        )
        or report.counts != counts(root, report.rows)
    ):
        raise ValueError("comparison or review contract is inconsistent")
    return report


def _entries(root: Path, report: ComparisonRecording, nonce: UUID) -> tuple[ReviewEntry, ...]:
    cases = {case.id: case for case in load_corpus(root).cases}
    entries = []
    for row_index, row in enumerate(report.rows):
        case = cases[row.observation.case_id]
        seeds = {s.id: s for s in case.seeds}
        for index, candidate in enumerate(row.observation.hypotheses):
            if not set(candidate.support) <= seeds.keys():
                raise ValueError("candidate has unknown original support")
            # Include reference support too, so an omitted/extra source is visible.
            needed = set(candidate.support) | {key for h in case.hypotheses for key in h.support}
            aliases = {key: _identity(nonce, row_index, index, "source", key) for key in needed}

            def blinded(hypothesis: Hypothesis, names: dict[str, str] = aliases) -> Hypothesis:
                return hypothesis.model_copy(
                    update={"support": tuple(sorted(names[k] for k in hypothesis.support))}
                )

            entries.append(
                ReviewEntry(
                    id=_identity(nonce, row_index, index),
                    candidate=blinded(candidate),
                    candidate_sha256=_digest(candidate.model_dump(mode="json")),
                    original_support_sha256=_digest(
                        [seeds[k].model_dump(mode="json") for k in sorted(candidate.support)]
                    ),
                    sources=tuple(
                        sorted(
                            (
                                seeds[k].model_copy(
                                    update={
                                        "id": aliases[k],
                                        "session": _identity(
                                            nonce,
                                            row_index,
                                            index,
                                            "event_session",
                                            seeds[k].session,
                                        ),
                                    }
                                )
                                for k in needed
                            ),
                            key=lambda s: s.id,
                        )
                    ),
                    references=tuple(
                        sorted(
                            (
                                ReviewReference(
                                    id=_identity(nonce, row_index, index, "reference", ref_index),
                                    meaning=blinded(reference),
                                )
                                for ref_index, reference in enumerate(case.hypotheses)
                            ),
                            key=lambda r: r.id,
                        )
                    ),
                )
            )
    return tuple(sorted(entries, key=lambda entry: entry.id))


def _packet(root: Path, path: Path, report: ComparisonRecording, nonce: UUID) -> ReviewPacket:
    return ReviewPacket(
        contract_sha256=load_review_contract(root),
        observation_sha256=file_digest(path),
        entries=_entries(root, report, nonce),
    )


def prepare_review(root: Path, report_path: Path, directory: Path, ids: IdFactory) -> ReviewPacket:
    """Create an exclusive packet, private key and deliberately incomplete template."""
    report = _report(root, report_path)
    nonce = ids.new_id()
    packet = _packet(root, report_path, report, nonce)
    raw = _bytes(packet)
    digest = hashlib.sha256(raw).hexdigest()
    key = ReviewKey(nonce=nonce, packet_sha256=digest)
    template = {
        "version": packet.version,
        "packet_sha256": digest,
        "reviewer": "",
        "human_reviewed": False,
        "decisions": [
            {"candidate_id": entry.id, "verdict": None, "reference_id": None}
            for entry in packet.entries
        ],
    }
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in (
        ("packet.json", raw),
        ("private-key.json", _bytes(key)),
        ("decisions.json", (json.dumps(template, indent=2) + "\n").encode()),
    ):
        with (directory / name).open("xb") as handle:
            handle.write(data)
    return packet


def score_review(root: Path, report_path: Path, directory: Path, output: Path) -> ReviewAssessment:
    """Validate every human decision and publish a new, non-activating assessment."""
    report = _report(root, report_path)
    key = ReviewKey.model_validate_json((directory / "private-key.json").read_bytes())
    packet = _packet(root, report_path, report, key.nonce)
    packet_path = directory / "packet.json"
    if packet_path.read_bytes() != _bytes(packet) or file_digest(packet_path) != key.packet_sha256:
        raise ValueError("review packet or private key changed")
    decision_path = directory / "decisions.json"
    decisions = Decisions.model_validate_json(decision_path.read_bytes())
    if decisions.packet_sha256 != key.packet_sha256:
        raise ValueError("decisions bind a different packet")
    by_id = {decision.candidate_id: decision for decision in decisions.decisions}
    if len(by_id) != len(decisions.decisions) or by_id.keys() != {e.id for e in packet.entries}:
        raise ValueError("every output requires exactly one known decision")
    entries = {entry.id: entry for entry in packet.entries}
    cases = {case.id: case for case in load_corpus(root).cases}
    matches = []
    for row_index, row in enumerate(report.rows):
        used: set[int] = set()
        for index, _ in enumerate(row.observation.hypotheses):
            identity = _identity(key.nonce, row_index, index)
            entry, decision = entries[identity], by_id[identity]
            if decision.verdict != "equivalent":
                continue
            reference = next((r for r in entry.references if r.id == decision.reference_id), None)
            if reference is None or set(reference.meaning.support) != set(entry.candidate.support):
                raise ValueError("equivalence requires an existing reference and exact support")
            case = cases[row.observation.case_id]
            used.add(
                next(
                    i
                    for i in range(len(case.hypotheses))
                    if _identity(key.nonce, row_index, index, "reference", i) == reference.id
                )
            )
        matches.append(len(used))
    failures, _ = score_comparison(root, report.rows, hypothesis_matches=tuple(matches))
    original_failures, _ = score_comparison(root, report.rows)
    failures += tuple(f for f in report.failures if f not in original_failures)
    totals = counts(root, report.rows)
    for row, matched in zip(report.rows, matches, strict=True):
        case = cases[row.observation.case_id]
        before = asdict(score_case(case, row.observation))["hypothesis_matches"]
        totals[f"{case.split}:{row.arm}"]["hypothesis_matches"] += matched - before
    assessment = ReviewAssessment(
        contract_sha256=packet.contract_sha256,
        observation_sha256=packet.observation_sha256,
        packet_sha256=key.packet_sha256,
        decisions_sha256=file_digest(decision_path),
        reviewer=decisions.reviewer,
        counts=totals,
        failures=failures,
    )
    with output.open("xb") as handle:
        handle.write(_bytes(assessment))
    return assessment
