"""A standing owner admission for semantic recall: no expiry, no release census.

The bounded routes (ADR-0220 through ADR-0246) bind quality evidence to an
exact release tree and to a lifetime of at most seven days, so every release
and every missed check-in returned production to sparse recall. This route
(ADR-0290) carries the owner's standing decision instead of quality evidence.
It binds the owner, the embedding tuple the stored vectors were made in, and
the bytes of the two files, and nothing else: a policy, formation or reader
change does not withdraw recall, because the admission makes no quality claim
that such a change could invalidate. Only the operator's flag, or a change to
either file, turns it off.

As with every other admission, the files bind bytes, not authorship. The
operator verifies the genuine owner statement before installing them.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import AwareDatetime, Field

from agent_core.domain.agents import Principal
from agent_core.domain.general_recall_release import Digest, Identifier, ReleaseValue
from agent_core.memory.bounded_recall_admission import BoundedAdmission
from agent_core.memory.general_recall_evidence import _read_bounded, _require, _unique_object
from agent_core.ports.determinism import Clock

STANDING_EVIDENCE_VERSION: Final = "recall-standing-evidence@1"
STANDING_ADMISSION_VERSION: Final = "recall-standing-admission@1"
EVIDENCE_FILE = "standing-evidence.json"
MANIFEST_FILE = "standing-admission.json"
# The composition's live dependency digests; only the embedding tuple is bound.
REQUIRED_DEPENDENCIES = frozenset(
    {
        "configuration_sha256",
        "embedding_configuration_sha256",
        "formation_configuration_sha256",
        "reader_configuration_sha256",
    }
)


class StandingRecallEvidence(ReleaseValue):
    """The owner's standing decision in their own words, hashed and referenced."""

    version: Literal["recall-standing-evidence@1"]
    tenant_id: Identifier
    principal_id: Identifier
    owner_kind: Literal["human"]
    decided_at: AwareDatetime
    instruction: str = Field(min_length=20, max_length=10000)
    owner_response_ref: Identifier
    owner_statement_sha256: Digest
    supersedes: Identifier | None = None
    # The live digests at preparation, kept for the record; the binder reads
    # only the embedding tuple from the manifest.
    observed_dependencies: dict[str, Digest] = Field(min_length=1, max_length=16)


class StandingRecallManifest(ReleaseValue):
    """What composition binds: the owner, the embedding tuple and the evidence bytes."""

    version: Literal["recall-standing-admission@1"]
    evidence_sha256: Digest
    tenant_id: Identifier
    principal_id: Identifier
    activated_at: AwareDatetime
    embedding_configuration_sha256: Digest
    # The retained release an operator would roll back to; informational.
    rollback_build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode(value: ReleaseValue) -> bytes:
    return (
        json.dumps(value.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def bind_standing_recall(
    manifest: Path,
    evidence: Path,
    dependencies: Mapping[str, str],
    *,
    principal: Principal,
    clock: Clock,
) -> BoundedAdmission:
    """Bind the owner's standing decision; the handle withdraws only on file mutation.

    The three digests other than the embedding tuple are read but not enforced:
    this admission carries a decision, not quality evidence measured under a
    configuration, so a policy, formation or reader change leaves it standing.
    The returned marker keeps composition's local-encoder, interactive-only and
    no-unattended-reconsolidation rules.
    """
    raw_manifest, raw_evidence = _read_bounded(manifest), _read_bounded(evidence)
    binding = StandingRecallManifest.model_validate(
        json.loads(raw_manifest, object_pairs_hook=_unique_object)
    )
    facts = StandingRecallEvidence.model_validate(
        json.loads(raw_evidence, object_pairs_hook=_unique_object)
    )
    _require(
        binding.version.rsplit("@", 1)[1] == facts.version.rsplit("@", 1)[1],
        "standing admission and evidence versions differ",
    )
    _require(
        binding.evidence_sha256 == _digest(raw_evidence),
        "standing evidence changed since the manifest was written",
    )
    owner = (principal.tenant_id, principal.principal_id)
    _require(
        (binding.tenant_id, binding.principal_id) == owner
        and (facts.tenant_id, facts.principal_id) == owner,
        "standing admission names another owner",
    )
    _require(
        len(facts.instruction.strip()) >= 20
        and facts.owner_statement_sha256 == _digest(facts.instruction.encode("utf-8"))
        and facts.owner_response_ref.strip() != "",
        "standing admission omits the owner's actual statement",
    )
    _require(
        facts.decided_at <= binding.activated_at <= clock.now(),
        "standing admission activation precedes the decision or lies in the future",
    )
    _require(
        set(dependencies) == REQUIRED_DEPENDENCIES
        and dependencies["embedding_configuration_sha256"]
        == binding.embedding_configuration_sha256,
        "standing admission embedding tuple mismatch",
    )
    revoked = False

    def admitted() -> bool:
        nonlocal revoked
        if not revoked:
            try:
                revoked = (
                    _read_bounded(manifest) != raw_manifest
                    or _read_bounded(evidence) != raw_evidence
                )
            except (OSError, ValueError):
                revoked = True
        return not revoked

    _require(admitted(), "standing admission changed while binding")
    return BoundedAdmission(admitted, _digest(raw_manifest))


def write_standing_admission(
    directory: Path,
    *,
    tenant_id: str,
    principal_id: str,
    instruction: str,
    reference: str,
    rollback_build_ref: str,
    dependencies: Mapping[str, str],
    activated_at: datetime,
    decided_at: datetime | None = None,
    supersedes: str | None = None,
) -> tuple[Path, Path]:
    """Write the evidence then the manifest that cites it; never over existing files."""
    _require(
        set(dependencies) == REQUIRED_DEPENDENCIES,
        "standing admission needs the four live dependency digests",
    )
    evidence_path, manifest_path = directory / EVIDENCE_FILE, directory / MANIFEST_FILE
    _require(
        not evidence_path.exists() and not manifest_path.exists(),
        "standing admission files already exist; choose an empty directory",
    )
    _require(
        decided_at is None or decided_at <= activated_at,
        "standing admission decided after activation; the binder would refuse it",
    )
    facts = StandingRecallEvidence(
        version=STANDING_EVIDENCE_VERSION,
        tenant_id=tenant_id,
        principal_id=principal_id,
        owner_kind="human",
        decided_at=decided_at or activated_at,
        instruction=instruction,
        owner_response_ref=reference,
        owner_statement_sha256=_digest(instruction.encode("utf-8")),
        supersedes=supersedes,
        observed_dependencies=dict(dependencies),
    )
    raw_evidence = _encode(facts)
    binding = StandingRecallManifest(
        version=STANDING_ADMISSION_VERSION,
        evidence_sha256=_digest(raw_evidence),
        tenant_id=tenant_id,
        principal_id=principal_id,
        activated_at=activated_at,
        embedding_configuration_sha256=dependencies["embedding_configuration_sha256"],
        rollback_build_ref=rollback_build_ref,
    )
    directory.mkdir(parents=True, exist_ok=True)
    with open(evidence_path, "xb") as handle:
        handle.write(raw_evidence)
    with open(manifest_path, "xb") as handle:
        handle.write(_encode(binding))
    return evidence_path, manifest_path
