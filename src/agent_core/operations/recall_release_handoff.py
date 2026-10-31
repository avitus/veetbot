"""Publish a trial handoff using independently collected, operator-owned release evidence.

Install this module and its interpreter outside the deployment account's write boundary.
No executable or Python module from either release is loaded by the privileged writer.
"""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from pydantic import AwareDatetime

from agent_core.domain.agents import Principal
from agent_core.domain.general_recall_release import Digest, ReleaseValue
from agent_core.memory.bounded_recall_admission import (
    BoundedRecallManifest,
    bind_bounded_recall,
)
from agent_core.memory.general_recall_evidence import _read_bounded
from agent_core.memory.recall_handoff import (
    TRIALS,
    Build,
    HandoffError,
    ReleaseHandoff,
    ReleaseID,
    attempt_sha256,
    protected,
    read_receipt,
)
from agent_core.memory.recall_release_binding import capture_inputs
from agent_core.ports.determinism import Clock

APP = Path("/opt/veetbot")
PROCESSES = Path("/proc")


class Environment(ReleaseValue):
    settings_sha256: Digest
    permissions_sha256: Digest
    schema_sha256: Digest
    overlays_sha256: Digest
    assets_sha256: Digest
    roles_sha256: Digest


class Verification(ReleaseValue):
    """The independent release operator attests reviewed target and rollback evidence."""

    source_release: ReleaseID
    target_release: ReleaseID
    target_commit: Build
    prior_manifest_sha256: Digest
    evidence_sha256: Digest
    rollback_build_ref: Build
    review_sha256: Digest
    ci_sha256: Digest
    rollback_verification_sha256: Digest
    source_environment: Environment
    target_environment: Environment


class Prepared(ReleaseValue):
    captured_at: AwareDatetime
    facts: Verification


def issue(source: Path, target: Path, manifest: Path, proof: Verification, clock: Clock) -> None:
    """Bind exact bytes and use a pinned directory so renewal/withdrawal wins races."""
    if (
        source.name != proof.source_release
        or target.name != proof.target_release
        or target.name[:15] <= source.name[:15]
        or not proof.target_commit.startswith(target.name.rsplit("-", 1)[1])
        or proof.source_environment != proof.target_environment
    ):
        raise HandoffError("Release or environment differs from the attested handoff")
    protected(manifest.parent, directory=True)
    protected(manifest)
    evidence = manifest.with_name("evidence.json")
    protected(evidence)
    raw = _read_bounded(manifest)
    binding = BoundedRecallManifest.model_validate_json(raw)
    if (
        binding.version not in {"recall-bounded-admission@3", "recall-bounded-admission@4"}
        or proof.prior_manifest_sha256 != hashlib.sha256(raw).hexdigest()
        or proof.rollback_build_ref != binding.rollback_build_ref
        or proof.target_commit in {binding.rollback_build_ref, binding.deployment_build_ref}
        or proof.evidence_sha256 != binding.evidence_sha256
    ):
        raise HandoffError("Trial, target or rollback identity mismatch")
    dependencies = binding.target_inputs.dependencies
    active = bind_bounded_recall(
        source,
        manifest,
        evidence,
        source.name,
        {
            k: dependencies[k]
            for k in (
                "configuration_sha256",
                "embedding_configuration_sha256",
                "formation_configuration_sha256",
                "reader_configuration_sha256",
            )
        },
        principal=Principal(
            tenant_id=binding.tenant_id, principal_id=binding.principal_id, scopes=set()
        ),
        clock=clock,
    )
    if capture_inputs(target, dependencies) != binding.target_inputs:
        raise HandoffError("Staged recall input census differs")
    receipt = ReleaseHandoff(
        version="recall-release-handoff@1",
        source_release=source.name,
        target_release=target.name,
        deployment_build_ref=proof.target_commit,
        manifest_sha256=proof.prior_manifest_sha256,
        evidence_sha256=binding.evidence_sha256,
        rollback_build_ref=binding.rollback_build_ref,
        attempt_sha256=attempt_sha256(target),
        inputs_sha256=binding.target_inputs.digest,
        environment_sha256=hashlib.sha256(
            proof.target_environment.model_dump_json().encode()
        ).hexdigest(),
        review_sha256=proof.review_sha256,
        ci_sha256=proof.ci_sha256,
        rollback_verification_sha256=proof.rollback_verification_sha256,
    )
    publish(
        manifest,
        f"handoff-{receipt.attempt_sha256}.json",
        receipt.model_dump_json().encode(),
        active,
    )


def publish(manifest: Path, filename: str, raw: bytes, valid: Callable[[], bool]) -> None:
    """Atomically link new immutable evidence into the pinned active directory."""
    fd = os.open(manifest.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with tempfile.NamedTemporaryFile(dir=manifest.parent, delete=False) as stream:
            name = Path(stream.name).name
            try:
                stream.write(raw)
                stream.flush()
                os.fchown(stream.fileno(), -1, manifest.stat().st_gid)
                os.fchmod(stream.fileno(), 0o640)
                os.fsync(stream.fileno())
                if not valid() or os.fstat(fd).st_ino != manifest.parent.stat().st_ino:
                    raise HandoffError("Active trial changed during handoff")
                # Link once. An older role retains its own immutable receipt until
                # cutover finishes. A retry must use a different deployment nonce.
                os.link(
                    name,
                    filename,
                    src_dir_fd=fd,
                    dst_dir_fd=fd,
                    follow_symlinks=False,
                )
                os.fsync(fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def verify_roles(target: Path, manifest: Path, clock: Clock) -> None:
    """Confirm the binding composed by each running process, without executing candidate code."""
    if (APP / "current").resolve() != target:
        raise HandoffError("Cutover moved to a different release")
    raw = read_receipt(manifest, attempt_sha256(target))
    receipt = ReleaseHandoff.model_validate_json(raw)
    binding = BoundedRecallManifest.model_validate_json(_read_bounded(manifest))
    if (
        not binding.activated_at <= clock.now() < binding.expires_at
        or hashlib.sha256(_read_bounded(manifest.with_name("evidence.json"))).hexdigest()
        != receipt.evidence_sha256
        or receipt.target_release != target.name
        or hashlib.sha256(_read_bounded(manifest)).hexdigest() != receipt.manifest_sha256
    ):
        raise HandoffError("Superseded handoff")
    from agent_core.operations.recall_handoff_controller import probe

    environment = Environment.model_validate_json(probe("environment", target.name))
    if (
        hashlib.sha256(environment.model_dump_json().encode()).hexdigest()
        != receipt.environment_sha256
    ):
        raise HandoffError("Role environment changed during promotion")
    expected = hashlib.sha256(raw).hexdigest()
    eligible = {"api", "worker", "async-worker", "maintenance"}
    identities = {}
    for role in (*sorted(eligible), "schedule", "call", "surface"):
        unit = f"veetbot-{role}.service"

        def pid(unit_name: str) -> str:
            return subprocess.check_output(
                ["/usr/bin/systemctl", "show", "-p", "MainPID", "--value", unit_name],
                text=True,
                timeout=5,
            ).strip()

        process = pid(unit)
        if not process.isdecimal() or process == "0":
            raise HandoffError("Inactive role")
        proc = PROCESSES / process
        if (proc / "cwd").resolve() != target:
            raise HandoffError("Mixed release identities")
        env = dict(
            v.split(b"=", 1) for v in (proc / "environ").read_bytes().split(b"\0") if b"=" in v
        )
        if (env.get(b"AGENT_JOBS_ENABLED") == b"1") != (role in eligible | {"schedule"}) or (
            env.get(b"AGENT_MEMORY_SEMANTIC_INDEX_ENABLED") == b"1"
        ) != (role in eligible):
            raise HandoffError("Unexpected job or recall permission")
        if role in eligible:
            logs = subprocess.check_output(
                [
                    "/usr/bin/journalctl",
                    "-u",
                    unit,
                    "_PID=" + process,
                    "--grep",
                    "semantic_trial_composed",
                    "-n",
                    "1",
                    "--no-pager",
                    "-o",
                    "cat",
                ],
                timeout=5,
            )
            if f"semantic_trial_composed binding={expected} jobs=1".encode() not in logs:
                raise HandoffError("Running role did not compose the expected trial")
        if env.get(b"AGENT_MEMORY_RECONSOLIDATION_ENABLED", b"0") != b"0":
            raise HandoffError("Unexpected unattended reconsolidation permission")
        identities[role] = process
        if pid(unit) != process:
            raise HandoffError("Role changed during readback")
    # Retire only the bound manifest generation. Existing old PIDs keep their
    # own guard during rolling cutover. A fresh composition on rollback refuses.
    name = f"origin-{receipt.manifest_sha256}.retired"
    if not manifest.with_name(name).exists():
        publish(
            manifest,
            name,
            raw,
            lambda: (
                (APP / "current").resolve() == target
                and hashlib.sha256(_read_bounded(manifest)).hexdigest() == receipt.manifest_sha256
            ),
        )
    readback = f"handoff-{receipt.attempt_sha256}-verified.json"
    if not manifest.with_name(readback).exists():
        publish(
            manifest,
            readback,
            json.dumps(
                {
                    "verified_at": clock.now().isoformat(),
                    "release": target.name,
                    "binding_sha256": expected,
                    "role_pids": identities,
                }
            ).encode(),
            lambda: (
                (APP / "current").resolve() == target
                and hashlib.sha256(_read_bounded(manifest)).hexdigest() == receipt.manifest_sha256
                and read_receipt(manifest, attempt_sha256(target)) == raw
            ),
        )


class ExpiredPreflightError(HandoffError):
    """Allow deployment to collect fresh evidence once after a long worker drain."""


def main(target_id: str, mode: str = "--prepare", *, clock: Clock) -> None:
    """Only the release ID is caller-supplied. Trust material lives outside the release."""
    import re

    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{7,40}", target_id):
        raise HandoffError("Invalid release ID")
    protected(TRIALS, directory=True)
    manifests = list(TRIALS.glob("*/manifest.json"))
    if not manifests:
        raise HandoffError("No current trial")
    if len(manifests) != 1:
        raise HandoffError("Ambiguous trial")
    protected(manifests[0].parent, directory=True)
    protected(manifests[0])
    protected(manifests[0].with_name("evidence.json"))
    source = (APP / "current").resolve(strict=True)
    target = APP / "releases" / target_id
    if source.parent != APP / "releases" or target.resolve(strict=True) != target:
        raise HandoffError("Invalid release directories")
    if mode == "--verify":
        verify_roles(target, manifests[0], clock)
        return
    from agent_core.operations.recall_handoff_controller import collect

    manifest = manifests[0]
    key = f"handoff-preflight-{attempt_sha256(target)}.json"
    if mode == "--prepare":
        proof = collect(source, target, manifest)
        prepared = Prepared(captured_at=clock.now(), facts=proof)
        publish(
            manifest,
            key,
            prepared.model_dump_json().encode(),
            lambda: (
                hashlib.sha256(_read_bounded(manifest)).hexdigest() == proof.prior_manifest_sha256
            ),
        )
        return
    path = manifest.with_name(key)
    protected(path)
    prepared = Prepared.model_validate_json(_read_bounded(path))
    age = clock.now() - prepared.captured_at
    if mode != "--commit" or age < timedelta(0):
        raise HandoffError("Invalid handoff preflight")
    if age > timedelta(minutes=10):
        raise ExpiredPreflightError("Handoff preflight expired")
    from agent_core.operations.recall_handoff_controller import probe

    observed = Environment.model_validate_json(probe("environment", target.name))
    if observed != prepared.facts.target_environment:
        raise HandoffError("Effective environment changed before cutover")
    issue(source, target, manifest, prepared.facts, clock)


def command(clock: Clock) -> None:
    """Run the root-installed CLI with its injected clock."""
    try:
        if len(sys.argv) != 3 or sys.argv[2] not in ("--prepare", "--commit", "--verify"):
            raise HandoffError("Expected a release ID and deployment phase")
        main(sys.argv[1], sys.argv[2], clock=clock)
    except (OSError, ValueError) as exc:
        print(
            "Recall handoff refused:",
            str(exc) if isinstance(exc, HandoffError) else type(exc).__name__,
            file=sys.stderr,
        )
        sys.exit(os.EX_TEMPFAIL if isinstance(exc, ExpiredPreflightError) else 1)
