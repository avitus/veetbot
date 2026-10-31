"""Collect equivalence evidence from fixed, operator-owned read-only deployment probes.

The release controller owns the probes and the enrollment. The candidate cannot
supply executable paths, environment variables, observations, or approval files.
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Literal

from agent_core.domain.general_recall_release import Digest, ReleaseValue
from agent_core.memory.general_recall_evidence import _read_bounded
from agent_core.memory.recall_handoff import Build, HandoffError, ReleaseID, protected
from agent_core.operations.recall_release_handoff import Environment, Verification

CONTROLLER = Path("/etc/veetbot/recall/handoff-controller")
PROBES = Path("/usr/local/libexec/veetbot-recall")


class Enrollment(ReleaseValue):
    manifest_sha256: Digest
    evidence_sha256: Digest
    environment: Environment
    rollback_build_ref: Build


class Clearance(ReleaseValue):
    """Attestation from the existing authenticated release review and CI gate."""

    target_release: ReleaseID
    target_commit: Build
    reviewed_head: Build
    reviewed_tree: Build
    merge_tree: Build
    final_head_review_clear: Literal[True]
    all_review_forms_disposed: Literal[True]
    final_head_hosted_ci_passed: Literal[True]
    review_receipt_sha256: Digest
    ci_receipt_sha256: Digest


class Recovery(ReleaseValue):
    build_ref: Build
    schema_sha256: Digest
    assets_and_surfaces_verified: Literal[True]
    schema_guard_passed: Literal[True]
    verification_sha256: Digest


def probe(name: str, *arguments: str) -> bytes:
    """Invoke installed control-plane tools with no caller environment or shell."""
    protected(PROBES, directory=True)
    path = PROBES / name
    protected(path)
    result = subprocess.run(
        [str(path), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd="/",
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        timeout=60,
        check=False,
    )
    if result.returncode or not 0 < len(result.stdout) <= 65536:
        raise HandoffError(f"Independent {name} probe failed")
    return result.stdout


def collect(source: Path, target: Path, manifest: Path) -> Verification:
    """Run every observation on this deployment attempt, anchored to the original assessment."""
    protected(CONTROLLER, directory=True)
    path = CONTROLLER / "enrollment.json"
    protected(path)
    baseline = Enrollment.model_validate_json(_read_bounded(path))
    if baseline.manifest_sha256 != hashlib.sha256(_read_bounded(manifest)).hexdigest():
        raise HandoffError("Controller enrollment is for a different trial or renewal")
    clearance = Clearance.model_validate_json(probe("release-clearance", target.name))
    if clearance.target_release != target.name or clearance.reviewed_tree != clearance.merge_tree:
        raise HandoffError("Reviewed tree differs from deployment")
    old = Environment.model_validate_json(probe("environment", source.name))
    new = Environment.model_validate_json(probe("environment", target.name))
    if old != baseline.environment or new != old:
        raise HandoffError("Release environment differs from the enrolled production settings")
    recovery = Recovery.model_validate_json(probe("recovery", baseline.rollback_build_ref))
    if (
        recovery.build_ref != baseline.rollback_build_ref
        or recovery.schema_sha256 != new.schema_sha256
    ):
        raise HandoffError("Recovery target no longer verified on the current schema")
    return Verification(
        source_release=source.name,
        target_release=target.name,
        target_commit=clearance.target_commit,
        prior_manifest_sha256=baseline.manifest_sha256,
        evidence_sha256=baseline.evidence_sha256,
        rollback_build_ref=baseline.rollback_build_ref,
        review_sha256=clearance.review_receipt_sha256,
        ci_sha256=clearance.ci_receipt_sha256,
        rollback_verification_sha256=recovery.verification_sha256,
        source_environment=old,
        target_environment=new,
    )
