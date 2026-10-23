"""Content-free proofs for complete, owner-bound legacy attribution recovery."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field

from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import ForwardDreamingBoundary
from agent_core.domain.errors import ConflictError
from agent_core.domain.people_sources import source_id
from agent_core.domain.reconsolidation import ReconValue
from agent_core.domain.reconsolidation_attribution import AttributionFootprint

EMPTY_KINDS = frozenset({"person", "identifier", "interaction", "relationship", "commitment"})


class LegacyAttributionProof(ReconValue):
    record_id: UUID
    revision: int = Field(ge=1)
    kind: Literal[
        "person",
        "identifier",
        "interaction",
        "relationship",
        "commitment",
        "memory_link",
        "mention",
        "source",
    ]
    belief_id: UUID | None = None
    person_id: UUID | None = None
    role: Literal["subject", "object", "speaker", "mentioned"] | None = None
    source_id: UUID | None = None
    session_id: UUID | None = None
    event_sequence: int | None = Field(default=None, ge=1)
    start: int | None = Field(default=None, ge=0, le=32768)
    end: int | None = Field(default=None, gt=0, le=32768)


class LegacyAttributionManifest(ReconValue):
    tenant_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(min_length=1, max_length=256)
    legacy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    proofs: tuple[LegacyAttributionProof, ...] = Field(max_length=5000)

    def digest(self) -> str:
        ordered = self.model_copy(
            update={"proofs": tuple(sorted(self.proofs, key=lambda p: p.record_id))}
        )
        return hashlib.sha256(ordered.model_dump_json().encode()).hexdigest()


class LegacyRecoveryResult(ReconValue):
    records: int = Field(ge=0)
    applied: bool
    already_applied: bool = False
    manifest_digest: str


@dataclass(frozen=True)
class RecoveryHead:
    kind: str
    revision: int
    erased: bool
    attribution: AttributionFootprint | None
    has_payload: bool = False


@dataclass(frozen=True)
class RecoveryEvidence:
    heads: dict[UUID, RecoveryHead]
    beliefs: frozenset[UUID]
    # Applied, owner-bound source-cleanup receipts: source ID -> creation times.
    source_cleanup: dict[UUID, frozenset[datetime]]
    deleted_sessions: dict[UUID, datetime]


def validate_recovery(
    principal: Principal,
    manifest: LegacyAttributionManifest,
    boundary: ForwardDreamingBoundary | None,
    evidence: RecoveryEvidence,
) -> dict[UUID, AttributionFootprint]:
    """Validate the entire manifest before any caller mutates attribution or cursors."""
    if (manifest.tenant_id, manifest.principal_id) != (principal.tenant_id, principal.principal_id):
        raise ConflictError("Recovery manifest belongs to another owner")
    if boundary is None or boundary.legacy_digest != manifest.legacy_digest:
        raise ConflictError("Recovery requires the matching original cutover")
    proofs = {proof.record_id: proof for proof in manifest.proofs}
    if len(proofs) != len(manifest.proofs):
        raise ConflictError("Duplicate recovery proof")
    unknown = {key: head for key, head in evidence.heads.items() if head.attribution is None}
    if boundary.recovered_at is None:
        digest = hashlib.sha256(
            "\n".join(
                f"{key}:{head.revision}:{head.kind}" for key, head in sorted(unknown.items())
            ).encode()
        ).hexdigest()
        if set(proofs) != set(unknown) or digest != manifest.legacy_digest:
            raise ConflictError("Recovery must cover exactly the unchanged legacy heads")
    elif boundary.recovery_digest != manifest.digest() or unknown:
        raise ConflictError("Recovery retry changed or attribution is no longer complete")
    recovered: dict[UUID, AttributionFootprint] = {}
    for key, proof in proofs.items():
        head = evidence.heads.get(key)
        if (
            head is None
            or not head.erased
            or head.has_payload
            or (head.kind, head.revision) != (proof.kind, proof.revision)
        ):
            raise ConflictError("Recovery requires the exact purged erased head")
        fields = proof.model_dump(exclude_none=True, exclude={"record_id", "revision", "kind"})
        if proof.kind in EMPTY_KINDS:
            if fields:
                raise ConflictError("Non-attribution records cannot carry recovered source keys")
            recovered[key] = AttributionFootprint()
        elif proof.revision != 1:
            raise ConflictError("Later revisions cannot be proved by their original identifier")
        elif proof.kind == "memory_link":
            person = evidence.heads.get(proof.person_id) if proof.person_id else None
            if (
                set(fields) != {"belief_id", "person_id", "role"}
                or proof.belief_id not in evidence.beliefs
                or person is None
                or person.kind != "person"
                or proof.belief_id is None
                or uuid5(proof.belief_id, f"{proof.person_id}:{proof.role}") != key
            ):
                raise ConflictError("Memory-link identity does not prove owner-bound attribution")
            recovered[key] = AttributionFootprint(belief_id=proof.belief_id)
        elif proof.kind == "source":
            if (
                set(fields) != {"session_id", "event_sequence"}
                or proof.session_id is None
                or proof.event_sequence is None
                or source_id(principal, proof.session_id, proof.event_sequence) != key
                or proof.session_id not in evidence.deleted_sessions
                or evidence.deleted_sessions[proof.session_id]
                not in evidence.source_cleanup.get(key, frozenset())
            ):
                raise ConflictError(
                    "Source identity lacks a matching deleted-session cleanup receipt"
                )
            recovered[key] = AttributionFootprint(
                source_id=key, session_id=proof.session_id, event_sequence=proof.event_sequence
            )
        elif proof.kind == "mention":
            source = proofs.get(proof.source_id) if proof.source_id else None
            if (
                set(fields) != {"source_id", "start", "end"}
                or proof.source_id is None
                or source is None
                or source.kind != "source"
                or proof.start is None
                or proof.end is None
                or proof.end <= proof.start
                or uuid5(proof.source_id, f"mention:{proof.start}:{proof.end}") != key
            ):
                raise ConflictError("Mention identity lacks an exact proved source and span")
            recovered[key] = AttributionFootprint(source_id=proof.source_id)
    if boundary.recovered_at is not None and any(
        evidence.heads[key].attribution != value for key, value in recovered.items()
    ):
        raise ConflictError("Installed recovery metadata no longer matches its receipt")
    return recovered
