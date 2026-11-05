"""A root-issued target receipt supplements, but never replaces, live trial evidence."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from agent_core.domain.general_recall_release import Digest, ReleaseValue
from agent_core.memory.general_recall_evidence import _read_bounded, _unique_object

ReleaseID = Annotated[str, Field(pattern=r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{7,40}$")]
Build = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
TRIALS = Path("/etc/veetbot/recall/trials")
OPERATOR_UID = 0


class HandoffError(ValueError):
    """Operator-safe fixed reason describing a failed release check."""


class ReleaseHandoff(ReleaseValue):
    version: Literal["recall-release-handoff@1"]
    source_release: ReleaseID
    target_release: ReleaseID
    deployment_build_ref: Build
    manifest_sha256: Digest
    evidence_sha256: Digest
    rollback_build_ref: Build
    attempt_sha256: Digest
    inputs_sha256: Digest
    environment_sha256: Digest
    review_sha256: Digest
    ci_sha256: Digest
    rollback_verification_sha256: Digest


def protected(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    is_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not is_type(info.st_mode) or info.st_uid != OPERATOR_UID or info.st_mode & 0o022:
        raise HandoffError("Unsafe recall handoff ownership or permissions")


def read_receipt(manifest: Path, attempt: str) -> bytes:
    if manifest.name != "manifest.json" or manifest.parent.parent != TRIALS:
        raise HandoffError("Handoff requires a dedicated operator admission directory")
    for directory in (TRIALS, manifest.parent):
        protected(directory, directory=True)
    path = manifest.with_name(f"handoff-{attempt}.json")
    protected(path)
    return _read_bounded(path)


def retired_origin(manifest: Path, raw: bytes) -> bool:
    """A new process on the old build cannot revive a successfully transferred admission."""
    if manifest.parent.parent != TRIALS:
        return False
    marker = manifest.with_name(f"origin-{hashlib.sha256(raw).hexdigest()}.retired")
    try:
        protected(marker)
    except FileNotFoundError:
        return False
    return True


def attempt_sha256(root: Path) -> str:
    """Each deployment, including a retry of the same revision, gets a fresh nonce."""
    return hashlib.sha256(_read_bounded(root / ".venv/recall-handoff-attempt")).hexdigest()


def target_receipt(
    root: Path,
    manifest: Path,
    raw_manifest: bytes,
    release_id: str,
    evidence_sha256: str,
    inputs_sha256: str,
    rollback_build_ref: str,
) -> bytes:
    try:
        raw = read_receipt(manifest, attempt_sha256(root))
    except OSError as exc:
        raise HandoffError("No current release handoff") from exc
    receipt = ReleaseHandoff.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
    if not (
        root.resolve().name == release_id == receipt.target_release
        and receipt.target_release > receipt.source_release
        and receipt.deployment_build_ref.startswith(release_id.rsplit("-", 1)[1])
        and receipt.deployment_build_ref != rollback_build_ref == receipt.rollback_build_ref
        and receipt.attempt_sha256 == attempt_sha256(root)
        and receipt.manifest_sha256 == hashlib.sha256(raw_manifest).hexdigest()
        and receipt.evidence_sha256 == evidence_sha256
        and receipt.inputs_sha256 == inputs_sha256
    ):
        raise HandoffError("Release handoff no longer matches this trial or target")
    return raw
