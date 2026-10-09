"""Local equivalence rules; persistence must supply authenticated source snapshots."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from itertools import combinations
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import MemoryRecord
from agent_core.domain.reconsolidation import (
    ReconValue,
    SourceVersion,
    compatible,
    content_signature,
    eligible,
    utc_now,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
AttributionRole = Literal["speaker", "subject", "object", "mentioned"]


class MergeSource(ReconValue):
    """Current trusted store snapshot, never a provider-supplied assertion.

    The repository resolves the speaker and every admitted People identity,
    includes identity changes in content_revision, and supplies current
    erasure/rejection decisions. An unknown speaker cannot default to owner.
    """

    record: MemoryRecord
    version: SourceVersion
    attribution: tuple[tuple[AttributionRole, str], ...] = Field(min_length=1, max_length=64)
    original_evidence_at: AwareDatetime | None = None
    fenced: bool
    rejected: bool

    @model_validator(mode="after")
    def resolved_snapshot(self) -> MergeSource:
        if self.version.belief_id != self.record.id:
            raise ValueError("source version must identify its original")
        if len(set(self.attribution)) != len(self.attribution):
            raise ValueError("attribution identities must be distinct")
        if sum(role == "speaker" for role, _ in self.attribution) != 1:
            raise ValueError("attribution requires exactly one resolved speaker")
        for _, identity in self.attribution:
            if identity != "owner" and str(UUID(identity)) != identity:
                raise ValueError("attribution requires a canonical person UUID or owner")
        for instant in (
            self.record.valid_from,
            self.record.valid_to,
            self.record.expires_at,
            self.record.last_evidence_at,
            self.record.created_at,
            self.record.updated_at,
            self.record.last_reinforced_at,
            self.record.last_used_at,
        ):
            if instant is not None:
                utc_now(instant)
        return self


class MergeDependency(ReconValue):
    source: SourceVersion
    source_session_id: UUID
    source_event_ids: tuple[Annotated[int, Field(gt=0)], ...] = Field(min_length=1)
    evidence_at: AwareDatetime
    content_digest: Digest
    attribution_digest: Digest


class MergePlan(ReconValue):
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    canonical_id: UUID
    member_ids: tuple[UUID, ...] = Field(min_length=2, max_length=32)
    dependencies: tuple[MergeDependency, ...] = Field(min_length=2, max_length=32)
    blocked_pair_signatures: tuple[Digest, ...] = Field(min_length=1, max_length=992)

    @model_validator(mode="after")
    def complete_membership(self) -> MergePlan:
        if self.member_ids != tuple(sorted(set(self.member_ids))):
            raise ValueError("merge members must be distinct and ordered")
        if self.member_ids != tuple(item.source.belief_id for item in self.dependencies):
            raise ValueError("every member needs its ordered original dependency")
        oldest = min(
            self.dependencies,
            key=lambda item: (item.source.creation_sequence, item.source.belief_id),
        )
        if self.canonical_id != oldest.source.belief_id:
            raise ValueError("canonical member must have the oldest creation key")
        if self.blocked_pair_signatures != tuple(sorted(set(self.blocked_pair_signatures))):
            raise ValueError("block signatures must be distinct and ordered")
        return self


class MergeOperation(ReconValue):
    """Local transition value, not yet the persisted operation/history schema."""

    id: UUID
    plan: MergePlan
    state: Literal["committed", "invalidated", "undone"] = "committed"
    revision: int = Field(default=1, ge=1)
    committed_at: AwareDatetime
    invalidated_at: AwareDatetime | None = None
    undone_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def terminal_instant(self) -> MergeOperation:
        if (self.state == "invalidated") != (self.invalidated_at is not None):
            raise ValueError("invalidation state requires exactly its timestamp")
        if (self.state == "undone") != (self.undone_at is not None):
            raise ValueError("undo state requires exactly its timestamp")
        terminal = self.invalidated_at or self.undone_at
        if terminal is not None and (terminal < self.committed_at or self.revision < 2):
            raise ValueError("terminal merge requires a later revision and time")
        return self


class MergeUndoReceipt(ReconValue):
    operation_id: UUID
    tenant_id: str
    principal_id: str
    expected_revision: int = Field(ge=1)
    idempotency_digest: Digest
    undone_at: AwareDatetime


class MergeUndoResult(ReconValue):
    operation: MergeOperation
    receipt: MergeUndoReceipt
    restore_ids: tuple[UUID, ...]
    blocked_pairs: tuple[str, ...]


def revalidate_merge(
    operation: MergeOperation,
    principal: Principal,
    sources: tuple[MergeSource, ...],
    *,
    now: datetime,
) -> MergeOperation:
    _check_owner(operation, principal)
    now = utc_now(now)
    if now < operation.committed_at:
        raise ValueError("merge cannot be checked before its commit")
    if operation.state != "committed":
        return operation
    current = prepare_merge(
        principal,
        tuple(item.source for item in operation.plan.dependencies),
        sources,
        now=now,
        blocked_pairs=frozenset(),
        active_members=frozenset(),
    )
    if current == operation.plan:
        return operation
    return operation.model_copy(
        update={
            "state": "invalidated",
            "revision": operation.revision + 1,
            "invalidated_at": now,
        }
    )


def undo_merge(
    operation: MergeOperation,
    principal: Principal,
    sources: tuple[MergeSource, ...],
    *,
    expected_revision: int,
    idempotency_key: str,
    now: datetime,
    prior_receipt: MergeUndoReceipt | None,
) -> MergeUndoResult:
    _check_owner(operation, principal)
    now = utc_now(now)
    if not idempotency_key.strip() or len(idempotency_key) > 128 or expected_revision < 1:
        raise ValueError("undo requires a bounded idempotency key and positive revision")
    if now < operation.committed_at:
        raise ValueError("merge cannot be undone before its commit")
    key_digest = _digest([principal.tenant_id, principal.principal_id, idempotency_key])
    if prior_receipt is not None:
        if (
            prior_receipt.operation_id != operation.id
            or (prior_receipt.tenant_id, prior_receipt.principal_id)
            != (principal.tenant_id, principal.principal_id)
            or prior_receipt.expected_revision != expected_revision
            or prior_receipt.idempotency_digest != key_digest
            or operation.state != "undone"
            or operation.revision != expected_revision + 1
            or operation.undone_at != prior_receipt.undone_at
        ):
            raise ConflictError("undo key identifies a different request or receipt")
        receipt = prior_receipt
    else:
        if operation.state != "committed" or operation.revision != expected_revision:
            raise ConflictError("merge state or revision changed")
        operation = operation.model_copy(
            update={
                "state": "undone",
                "revision": operation.revision + 1,
                "undone_at": now,
            }
        )
        receipt = MergeUndoReceipt(
            operation_id=operation.id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            expected_revision=expected_revision,
            idempotency_digest=key_digest,
            undone_at=now,
        )
    dependencies = {item.source.belief_id: item for item in operation.plan.dependencies}
    if len({source.record.id for source in sources}) != len(sources):
        raise ValueError("undo source snapshots must have distinct identities")
    restore_ids = tuple(
        sorted(
            source.record.id
            for source in sources
            if source_admissible(source, principal, now)
            and dependencies.get(source.record.id) == source_dependency(source)
        )
    )
    return MergeUndoResult(
        operation=operation,
        receipt=receipt,
        restore_ids=restore_ids,
        blocked_pairs=operation.plan.blocked_pair_signatures,
    )


def _check_owner(operation: MergeOperation, principal: Principal) -> None:
    if (operation.plan.tenant_id, operation.plan.principal_id) != (
        principal.tenant_id,
        principal.principal_id,
    ):
        raise NotFoundError("merge not found")


def source_admissible(source: MergeSource, principal: Principal, now: datetime) -> bool:
    record = source.record
    return (
        (record.tenant_id, record.principal_id) == (principal.tenant_id, principal.principal_id)
        and record.id == source.version.belief_id
        and not source.fenced
        and not source.rejected
        and not record.flagged_for_review
        and not record.conflicts_with
        and eligible(record, now)
        and not contains_injection_pattern(record.subject + " " + record.statement)
        and not contains_secret_material(record.subject + " " + record.statement)
    )


def prepare_merge(
    principal: Principal,
    expected: tuple[SourceVersion, ...],
    sources: tuple[MergeSource, ...],
    *,
    now: datetime,
    blocked_pairs: frozenset[str],
    active_members: frozenset[UUID],
) -> MergePlan | None:
    now = utc_now(now)
    if not 2 <= len(sources) <= 32:
        return None
    versions = {item.belief_id: item for item in expected}
    if (
        len(versions) != len(expected)
        or len({source.record.id for source in sources}) != len(sources)
        or len(sources) != len(expected)
    ):
        return None
    for source in sources:
        record = source.record
        if (
            not source_admissible(source, principal, now)
            or versions.get(record.id) != source.version
            or record.id in active_members
            or not compatible(record, sources[0].record)
        ):
            return None
    if any(_equivalence_key(source) != _equivalence_key(sources[0]) for source in sources[1:]):
        return None
    signatures = _pair_signatures(principal, sources)
    if blocked_pairs.intersection(signatures):
        return None
    ordered = sorted(
        sources, key=lambda source: (source.version.creation_sequence, source.record.id)
    )
    return MergePlan(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        canonical_id=ordered[0].record.id,
        member_ids=tuple(sorted(source.record.id for source in ordered)),
        dependencies=tuple(
            source_dependency(source) for source in sorted(sources, key=lambda s: s.record.id)
        ),
        blocked_pair_signatures=signatures,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_claim(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def source_dependency(source: MergeSource) -> MergeDependency:
    return MergeDependency(
        source=source.version,
        source_session_id=source.record.source_session_id,
        source_event_ids=tuple(sorted(set(source.record.source_event_ids))),
        evidence_at=utc_now(source.record.last_evidence_at),
        content_digest=content_signature(source.record),
        attribution_digest=_digest(sorted(source.attribution)),
    )


def _pair_signatures(principal: Principal, sources: tuple[MergeSource, ...]) -> tuple[str, ...]:
    owner = [principal.tenant_id, principal.principal_id]
    signatures: set[str] = set()
    identities = {
        source.record.id: _digest(
            [
                normalize_claim(source.record.statement),
                source.record.belief_type,
                source.record.claim_kind,
                source.record.polarity,
                sorted(source.attribution),
                str(source.record.source_session_id),
                sorted(set(source.record.source_event_ids)),
            ]
        )
        for source in sources
    }
    for left, right in combinations(sources, 2):
        signatures.add(
            _digest(["members", owner, sorted([str(left.record.id), str(right.record.id)])])
        )
        signatures.add(
            _digest(
                [
                    "evidence",
                    owner,
                    sorted([identities[left.record.id], identities[right.record.id]]),
                ]
            )
        )
    return tuple(sorted(signatures))


def _equivalence_key(source: MergeSource) -> tuple[object, ...]:
    record = source.record
    return (
        normalize_claim(record.statement),
        record.subject,
        record.belief_type,
        record.claim_kind,
        record.polarity,
        record.derivation,
        tuple(sorted(source.attribution)),
        record.scope,
        record.portability,
        record.authority,
        record.sensitivity,
        record.valid_from,
        record.valid_to,
        record.expires_at,
    )
