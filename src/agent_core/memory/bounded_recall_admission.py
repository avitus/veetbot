"""Explicit owner-reviewed, expiring ADR-0220 admission; no legacy waiver.

Like legacy release evidence, operator-supplied hashes bind retained originals;
these files cannot authenticate human authorship or prove an operator was honest.
"""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, Field

from agent_core.domain.agents import Principal
from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.domain.general_recall_release import Digest, Identifier, ReleaseValue
from agent_core.memory.bounded_recall_evidence import assess_bounded_comparison
from agent_core.memory.general_recall_evidence import (
    _answer,
    _read_bounded,
    _require,
    _stage,
    _unique_object,
)
from agent_core.memory.recall_release_binding import (
    InputFile,
    RecallInputManifest,
    capture_inputs,
    input_stamps,
)
from agent_core.ports.determinism import Clock


class BoundedHumanReview(ReleaseValue):
    version: Literal["recall-owner-review-submission@1"]
    reviewer: str = Field(min_length=2, max_length=256)
    reviewer_kind: Literal["human"]
    independent_of_implementation: Literal[True]
    reviewed_at: AwareDatetime
    decision: Literal["accept_bounded_rollout"]
    findings: str = Field(min_length=20, max_length=100000)
    observations_sha256: Digest
    review_items_sha256: Digest
    reviewed_scenarios: tuple[Identifier, ...] = Field(min_length=1, max_length=400)
    reviewed_items: tuple[Digest, ...] = Field(max_length=20000)


class CompatibilityCheck(ReleaseValue):
    id: Identifier
    command: str = Field(min_length=2, max_length=4096)
    exit_code: Literal[0]
    inputs_sha256: Digest
    report_sha256: Digest


class InputDisposition(ReleaseValue):
    before: InputFile | None
    after: InputFile | None
    rationale: str = Field(min_length=20, max_length=10000)
    checks: tuple[Identifier, ...] = Field(min_length=1, max_length=100)


class DependencyDisposition(ReleaseValue):
    before: Digest | None
    after: Digest | None
    rationale: str = Field(min_length=20, max_length=10000)
    checks: tuple[Identifier, ...] = Field(min_length=1, max_length=100)


class CompatibilityAssessment(ReleaseValue):
    target_inputs_sha256: Digest
    changes: dict[str, InputDisposition] = Field(max_length=20000)
    dependency_changes: dict[str, DependencyDisposition] = Field(max_length=100)
    checks: tuple[CompatibilityCheck, ...] = Field(min_length=1, max_length=100)


class BoundedRecallEvidence(ReleaseValue):
    version: Literal["recall-bounded-evidence@1"]
    bundle: CheckpointReleaseBundle
    operational_gate_report: str | None = Field(default=None, max_length=2 * 1024 * 1024)
    review_items: tuple[dict[str, Any], ...] = Field(max_length=20000)
    human_review: BoundedHumanReview
    owner_authorization_sha256: Digest
    provenance_verification_sha256: Digest
    retained_archives: tuple[Digest, ...] = Field(min_length=1, max_length=100)
    measured_inputs: RecallInputManifest
    compatibility: CompatibilityAssessment


class BoundedRecallManifest(ReleaseValue):
    version: Literal["recall-bounded-admission@1"]
    deployment_build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    evidence_sha256: Digest
    target_inputs: RecallInputManifest
    tenant_id: Identifier
    principal_id: Identifier
    activated_at: AwareDatetime
    expires_at: AwareDatetime
    rollback_build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")


@dataclass(frozen=True)
class BoundedAdmission:
    """Marker used by composition to require local, interactive owner queries."""

    check: Callable[[], bool]

    def __call__(self) -> bool:
        return self.check()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def validate_bounded_evidence(
    evidence: BoundedRecallEvidence,
    target: RecallInputManifest,
    clock: Clock,
) -> dict[str, Any]:
    """Recompute benefit and require explicit findings and complete dispositions."""
    evidence = BoundedRecallEvidence.model_validate(evidence.model_dump())
    bundle, review, compatibility = evidence.bundle, evidence.human_review, evidence.compatibility
    report = assess_bounded_comparison(
        bundle, operational_gate_report=evidence.operational_gate_report
    )
    _require(report["comparative_benefit_satisfied"], "bounded rollout has no comparative benefit")
    _require(
        evidence.measured_inputs.digest == bundle.lineage.evaluated_inputs_sha256,
        "original measured inputs changed",
    )
    observed = b"".join(r.model_dump_json().encode() + b"\n" for r in bundle.observations)
    scenarios = {s.id: s for s in bundle.scenarios}
    difficult = {
        r.scenario_id
        for r in bundle.observations
        if not _answer(r, scenarios[r.scenario_id])
        or any(
            not set(scenarios[r.scenario_id].required_evidence)
            <= _stage(r, scenarios[r.scenario_id], stage)
            for stage in ("formed", "candidates", "ranked", "rendered")
        )
    }
    for item in evidence.review_items:
        if item.get("kind") == "interrupted_attempt":
            difficult.add(item["attempt"]["key"][0])
        elif item.get("kind") == "parent_preparation_replacement":
            difficult.add(item["approval"]["scenario_id"])
    item_hashes = {_digest(_encoded(item)) for item in evidence.review_items}
    _require(
        review.reviewer.strip() != ""
        and len(review.findings.strip()) >= 20
        and review.observations_sha256 == _digest(observed)
        and review.review_items_sha256 == _digest(_encoded(evidence.review_items) + b"\n")
        and set(review.reviewed_items) == item_hashes
        and len(review.reviewed_items) == len(item_hashes)
        and len(set(review.reviewed_scenarios)) == len(review.reviewed_scenarios)
        and difficult <= set(review.reviewed_scenarios) <= set(scenarios)
        and max(r.observed_at for r in bundle.observations) <= review.reviewed_at <= clock.now(),
        "human findings omit failed evidence, interruptions or uncertain charges",
    )
    checks = {c.id: c for c in compatibility.checks}
    _require(
        len(checks) == len(compatibility.checks)
        and compatibility.target_inputs_sha256 == target.digest
        and all(c.inputs_sha256 == target.digest for c in compatibility.checks),
        "compatibility checks refer to different target inputs",
    )
    before, after = evidence.measured_inputs.files, target.files
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    _require(set(compatibility.changes) == changed, "unassessed target file changes")
    for name, disposition in compatibility.changes.items():
        _require(
            disposition.before == before.get(name)
            and disposition.after == after.get(name)
            and len(disposition.rationale.strip()) >= 20
            and set(disposition.checks) <= checks.keys(),
            "file disposition or verification mismatch",
        )
    old_deps, new_deps = evidence.measured_inputs.dependencies, target.dependencies
    changed_deps = {
        name
        for name in old_deps.keys() | new_deps.keys()
        if old_deps.get(name) != new_deps.get(name)
    }
    _require(
        set(compatibility.dependency_changes) == changed_deps,
        "unassessed target dependency changes",
    )
    for name, dependency in compatibility.dependency_changes.items():
        _require(
            dependency.before == old_deps.get(name)
            and dependency.after == new_deps.get(name)
            and len(dependency.rationale.strip()) >= 20
            and set(dependency.checks) <= checks.keys(),
            "dependency disposition or verification mismatch",
        )
    return report


def bind_bounded_recall(
    root: Path,
    manifest: Path,
    evidence: Path,
    release_id: str,
    dependencies: Mapping[str, str],
    *,
    principal: Principal,
    clock: Clock,
) -> BoundedAdmission:
    """Bind separately assessed target inputs and actual human findings."""
    raw_manifest, raw_evidence = _read_bounded(manifest), _read_bounded(evidence)
    binding = BoundedRecallManifest.model_validate(
        json.loads(raw_manifest, object_pairs_hook=_unique_object)
    )
    facts = BoundedRecallEvidence.model_validate(
        json.loads(raw_evidence, object_pairs_hook=_unique_object)
    )
    required = {
        "configuration_sha256",
        "embedding_configuration_sha256",
        "formation_configuration_sha256",
        "reader_configuration_sha256",
    }
    revision = release_id.rsplit("-", 1)[-1]
    _require(
        re.fullmatch(r"[a-f0-9]{7,40}", revision) is not None
        and binding.deployment_build_ref.startswith(revision)
        and binding.rollback_build_ref != binding.deployment_build_ref
        and (binding.tenant_id, binding.principal_id)
        == (principal.tenant_id, principal.principal_id)
        and set(dependencies) == required
        and all(binding.target_inputs.dependencies.get(k) == v for k, v in dependencies.items())
        and binding.evidence_sha256 == _digest(raw_evidence)
        and binding.activated_at <= clock.now() < binding.expires_at
        and binding.expires_at - binding.activated_at <= timedelta(days=7),
        "bounded recall release, owner, lifetime or active dependencies mismatch",
    )
    stamps = input_stamps(root)
    _require(
        capture_inputs(root, binding.target_inputs.dependencies) == binding.target_inputs,
        "bounded target input mismatch",
    )
    validate_bounded_evidence(facts, binding.target_inputs, clock)
    _require(facts.human_review.reviewed_at <= binding.activated_at, "activation predates review")
    revoked = False

    def admitted() -> bool:
        nonlocal revoked
        if not revoked:
            try:
                revoked = (
                    not binding.activated_at <= clock.now() < binding.expires_at
                    or _read_bounded(manifest) != raw_manifest
                    or _read_bounded(evidence) != raw_evidence
                    or input_stamps(root) != stamps
                )
            except (OSError, ValueError):
                revoked = True
        return not revoked

    _require(admitted(), "bounded evidence changed while binding")
    return BoundedAdmission(admitted)
