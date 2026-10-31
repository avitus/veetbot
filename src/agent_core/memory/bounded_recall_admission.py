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
from typing import Annotated, Any, Literal

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
from agent_core.memory.production_recall_restart import (
    ProductionRecallRestart,
    validate_restart_window,
)
from agent_core.memory.production_recall_trial import ProductionRecallTrial, validate_trial_window
from agent_core.memory.recall_handoff import (
    attempt_sha256,
    read_receipt,
    retired_origin,
    target_receipt,
)
from agent_core.memory.recall_release_binding import (
    InputFile,
    RecallInputManifest,
    capture_inputs,
    input_stamps,
)
from agent_core.ports.determinism import Clock

# ADR-0222 approves exactly this retained comparison and immutable scope, not a
# general caller-selected sampling policy. These hashes confer no human authorship.
APPROVED_SCOPE_SHA256 = "1202aab4a598d84ca9f049793fa14c83cdf088b713bd4efbcc0fe87aaed66bcb"
APPROVED_OBSERVATIONS_SHA256 = "0c52e63f4d130e6a8181fa02db54ad066ddcee89185e58b7c37837acd2fe750e"
APPROVED_ITEMS_SHA256 = "e825ed4f8fa584bc148d0001e560610b3a5db0441ae6af526a432d2533646df9"


class _HumanReviewIdentity(ReleaseValue):
    reviewer: str = Field(min_length=2, max_length=256)
    reviewer_kind: Literal["human"]
    independent_of_implementation: Literal[True]
    reviewed_at: AwareDatetime
    decision: Literal["accept_bounded_rollout"]
    findings: str = Field(min_length=20, max_length=100000)
    observations_sha256: Digest
    review_items_sha256: Digest
    reviewed_scenarios: tuple[Identifier, ...] = Field(min_length=1, max_length=400)


class BoundedHumanReview(_HumanReviewIdentity):
    version: Literal["recall-owner-review-submission@1"]
    reviewed_items: tuple[Digest, ...] = Field(max_length=20000)


class ScopedFinding(ReleaseValue):
    verdict: Literal["acceptable", "concern", "unacceptable"]
    notes: str = Field(max_length=10000)


class ScopedHumanReview(_HumanReviewIdentity):
    version: Literal["recall-owner-review-submission@2"]
    scope_proposal_sha256: Digest
    omitted_scenarios: tuple[Identifier, ...] = Field(max_length=400)
    scenario_findings: dict[Identifier, ScopedFinding] = Field(min_length=1, max_length=400)
    summary_findings: dict[str, ScopedFinding] = Field(min_length=1, max_length=5)
    reviewed_item_groups: dict[str, tuple[Digest, ...]] = Field(min_length=4, max_length=4)
    residual_risk_findings: str = Field(min_length=20, max_length=10000)


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
    version: Literal[
        "recall-bounded-evidence@1",
        "recall-bounded-evidence@2",
        "recall-bounded-evidence@3",
        "recall-bounded-evidence@4",
    ]
    bundle: CheckpointReleaseBundle
    operational_gate_report: str | None = Field(default=None, max_length=2 * 1024 * 1024)
    review_items: tuple[dict[str, Any], ...] = Field(max_length=20000)
    human_review: Annotated[
        BoundedHumanReview | ScopedHumanReview | None, Field(discriminator="version")
    ] = None
    production_trial: ProductionRecallTrial | None = None
    production_restart: ProductionRecallRestart | None = None
    scope_proposal: str | None = Field(default=None, max_length=2 * 1024 * 1024)
    owner_authorization_sha256: Digest
    provenance_verification_sha256: Digest
    retained_archives: tuple[Digest, ...] = Field(min_length=1, max_length=100)
    measured_inputs: RecallInputManifest
    compatibility: CompatibilityAssessment


class BoundedRecallManifest(ReleaseValue):
    version: Literal[
        "recall-bounded-admission@1",
        "recall-bounded-admission@2",
        "recall-bounded-admission@3",
        "recall-bounded-admission@4",
    ]
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
    receipt_sha256: str | None = None

    def __call__(self) -> bool:
        return self.check()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _validate_scoped_review(
    evidence: BoundedRecallEvidence,
    review: ScopedHumanReview,
    scenarios: set[str],
    item_hashes: set[str],
) -> None:
    raw = evidence.scope_proposal
    _require(
        evidence.version == "recall-bounded-evidence@2"
        and raw is not None
        and _digest(raw.encode()) == APPROVED_SCOPE_SHA256
        and review.scope_proposal_sha256 == APPROVED_SCOPE_SHA256
        and review.observations_sha256 == APPROVED_OBSERVATIONS_SHA256
        and review.review_items_sha256 == APPROVED_ITEMS_SHA256,
        "scoped review does not bind the owner-approved comparison and scope",
    )
    assert raw is not None
    scope = json.loads(raw, object_pairs_hook=_unique_object)
    selected = set(scope["proposed_individual_scenarios"])
    omitted = set(scope["omitted_required_scenarios"])
    groups = scope["interruption_summary_groups"]
    supplied_groups = {k: list(v) for k, v in review.reviewed_item_groups.items()}
    flattened = [digest for group in supplied_groups.values() for digest in group]
    findings = (*review.scenario_findings.values(), *review.summary_findings.values())
    _require(
        set(review.reviewed_scenarios) == selected
        and set(review.omitted_scenarios) == omitted
        and len(review.omitted_scenarios) == len(omitted)
        and selected.isdisjoint(omitted)
        and selected | omitted <= scenarios
        and set(review.scenario_findings) == selected
        and set(review.summary_findings) == {"cost", *groups}
        and supplied_groups == groups
        and set(flattened) == item_hashes
        and len(flattened) == len(item_hashes)
        and len(review.residual_risk_findings.strip()) >= 20
        and all(
            f.verdict != "unacceptable" and (f.verdict == "acceptable" or len(f.notes.strip()) >= 3)
            for f in findings
        ),
        "scoped human findings omit approved cases, summaries or residual risk",
    )


def validate_bounded_evidence(
    evidence: BoundedRecallEvidence,
    target: RecallInputManifest,
    clock: Clock,
) -> dict[str, Any]:
    """Recompute benefit and require explicit findings and complete dispositions."""
    evidence = BoundedRecallEvidence.model_validate(evidence.model_dump())
    bundle, compatibility = evidence.bundle, evidence.compatibility
    report = assess_bounded_comparison(
        bundle, operational_gate_report=evidence.operational_gate_report
    )
    _require(report["comparative_benefit_satisfied"], "bounded rollout has no comparative benefit")
    _require(
        evidence.measured_inputs.digest == bundle.lineage.evaluated_inputs_sha256,
        "original measured inputs changed",
    )
    observed = b"".join(r.model_dump_json().encode() + b"\n" for r in bundle.observations)
    _require(
        (evidence.production_restart is not None)
        == (evidence.version == "recall-bounded-evidence@4"),
        "only version-four evidence requires a separate restart decision",
    )
    if evidence.version in ("recall-bounded-evidence@3", "recall-bounded-evidence@4"):
        trial = evidence.production_trial
        _require(
            trial is not None and evidence.human_review is None and evidence.scope_proposal is None,
            "production trial cannot substitute or invent a synthetic human review",
        )
        assert trial is not None
        _require(
            trial.authorization_sha256 == evidence.owner_authorization_sha256
            and len(trial.instruction.strip()) >= 20
            and trial.observations_sha256 == _digest(observed) == APPROVED_OBSERVATIONS_SHA256
            and trial.review_items_sha256
            == _digest(_encoded(evidence.review_items) + b"\n")
            == APPROVED_ITEMS_SHA256
            and max(r.observed_at for r in bundle.observations)
            <= trial.authorized_at
            <= clock.now(),
            "production trial omits original failed evidence or actual owner authorization",
        )
    else:
        _require(evidence.production_trial is None, "legacy admission cannot accept a trial")
        _validate_human_findings(evidence, observed, report, clock)
    _validate_compatibility(evidence, target, compatibility)
    return report


def _validate_human_findings(
    evidence: BoundedRecallEvidence, observed: bytes, report: dict[str, Any], clock: Clock
) -> None:
    bundle, review = evidence.bundle, evidence.human_review
    _require(review is not None, "human findings are required for legacy admission")
    assert review is not None
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
    difficult.update(item["scenario_id"] for item in report["exceptions"] if "scenario_id" in item)
    item_hashes = {_digest(_encoded(item)) for item in evidence.review_items}
    _require(
        review.reviewer.strip() != ""
        and len(review.findings.strip()) >= 20
        and review.observations_sha256 == _digest(observed)
        and review.review_items_sha256 == _digest(_encoded(evidence.review_items) + b"\n")
        and len(set(review.reviewed_scenarios)) == len(review.reviewed_scenarios)
        and max(r.observed_at for r in bundle.observations) <= review.reviewed_at <= clock.now(),
        "human findings omit failed evidence, interruptions or uncertain charges",
    )
    if isinstance(review, ScopedHumanReview):
        _validate_scoped_review(evidence, review, set(scenarios), item_hashes)
    else:
        _require(
            evidence.version == "recall-bounded-evidence@1"
            and evidence.scope_proposal is None
            and set(review.reviewed_items) == item_hashes
            and len(review.reviewed_items) == len(item_hashes)
            and difficult <= set(review.reviewed_scenarios) <= set(scenarios),
            "human findings omit failed evidence, interruptions or uncertain charges",
        )


def _validate_compatibility(
    evidence: BoundedRecallEvidence,
    target: RecallInputManifest,
    compatibility: CompatibilityAssessment,
) -> None:
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
    _require(
        binding.version.rsplit("@", 1)[1] == facts.version.rsplit("@", 1)[1],
        "bounded admission and evidence versions differ",
    )
    required = {
        "configuration_sha256",
        "embedding_configuration_sha256",
        "formation_configuration_sha256",
        "reader_configuration_sha256",
    }
    revision = release_id.rsplit("-", 1)[-1]
    handoff = None
    if facts.production_trial and binding.deployment_build_ref.startswith(revision):
        _require(
            not retired_origin(manifest, raw_manifest), "release handoff retired original build"
        )
    if facts.production_trial is not None and not binding.deployment_build_ref.startswith(revision):
        handoff = target_receipt(
            root,
            manifest,
            raw_manifest,
            release_id,
            binding.evidence_sha256,
            binding.target_inputs.digest,
            binding.rollback_build_ref,
        )
    _require(
        re.fullmatch(r"[a-f0-9]{7,40}", revision) is not None
        and (binding.deployment_build_ref.startswith(revision) or handoff is not None)
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
    active_since = binding.activated_at
    if facts.production_restart is not None:
        assert facts.production_trial is not None
        validate_restart_window(
            facts.production_trial,
            facts.production_restart,
            binding.activated_at,
            binding.expires_at,
            binding.target_inputs.digest,
            binding.rollback_build_ref,
            principal,
            clock,
        )
        active_since = facts.production_restart.restarted_at
    elif facts.production_trial is not None:
        validate_trial_window(
            facts.production_trial, binding.activated_at, binding.expires_at, principal, clock
        )
    else:
        assert facts.human_review is not None
        _require(
            facts.human_review.reviewed_at <= binding.activated_at, "activation predates review"
        )
    attempt = attempt_sha256(root) if handoff is not None else None
    revoked = False

    def admitted() -> bool:
        nonlocal revoked
        if not revoked:
            try:
                revoked = (
                    not active_since <= clock.now() < binding.expires_at
                    or (
                        handoff is not None
                        and (
                            read_receipt(manifest, attempt_sha256(root)) != handoff
                            or attempt_sha256(root) != attempt
                        )
                    )
                    or _read_bounded(manifest) != raw_manifest
                    or _read_bounded(evidence) != raw_evidence
                    or input_stamps(root) != stamps
                )
            except (OSError, ValueError):
                revoked = True
        return not revoked

    _require(admitted(), "bounded evidence changed while binding")
    return BoundedAdmission(
        admitted, _digest(handoff or raw_manifest) if facts.production_trial else None
    )
