"""Artifact metadata carried across the streaming storage boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from agent_core.domain.messages import FileReferencePart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef


class ArtifactOrigin(StrEnum):
    SANDBOX_EXPORT = "sandbox_export"
    TOOL_OUTPUT = "tool_output"
    MODEL_OUTPUT = "model_output"
    UPLOAD = "upload"
    TRAJECTORY_EXPORT = "trajectory_export"
    KNOWLEDGE_SOURCE = "knowledge_source"


# ADR-0122: every file a run exports rides on its final reply. Tool-output
# captures and the owner's own uploads are never re-attached.
REPLY_ATTACHMENT_ORIGINS = frozenset(
    {ArtifactOrigin.SANDBOX_EXPORT.value, ArtifactOrigin.MODEL_OUTPUT.value}
)


def reply_attachments(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
    """Choose a run's exported files in creation order, once per name and content."""

    chosen: list[ArtifactRef] = []
    seen: set[tuple[str, str]] = set()
    for artifact in sorted(artifacts, key=lambda item: (item.created_at, item.id)):
        key = (artifact.name, artifact.sha256)
        if artifact.origin not in REPLY_ATTACHMENT_ORIGINS or key in seen:
            continue
        seen.add(key)
        chosen.append(artifact)
    return chosen


def reply_file_reference(artifact: ArtifactRef) -> FileReferencePart:
    return FileReferencePart(
        artifact_id=artifact.id,
        media_type=artifact.media_type,
        filename=artifact.name,
        size_bytes=artifact.size_bytes,
    )


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    artifact_id: UUID
    tenant_id: str
    principal_id: str
    session_id: UUID
    run_id: UUID | None
    origin: ArtifactOrigin
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    trust: TrustLevel
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredArtifactRef:
    artifact_id: UUID
    sha256: str
    size_bytes: int
    media_type: str


# ADR-0120 chat attachments. The metadata keys below are the only upload facts
# kept on the artifact row, and every one of them is visible in ArtifactView.
ATTACHMENT_METADATA_KEY = "attachment"
AUTO_INGEST_KEY = "auto_ingest"
AUTO_INGEST_REASON_KEY = "auto_ingest_reason"
AUTO_INGEST_ATTEMPTS_KEY = "auto_ingest_attempts"
AUTO_INGEST_PENDING = "pending"
AUTO_INGEST_INGESTED = "ingested"
AUTO_INGEST_REFUSED = "refused"
AUTO_INGEST_FAILED = "failed"
UPLOAD_UNCLAIMED_TTL = timedelta(hours=24)


def claimed_upload(artifact: ArtifactRef, *, run_id: UUID, auto_ingest: bool) -> ArtifactRef:
    """Apply the claim rule: first claim binds the run and ends the expiry."""

    update: dict[str, Any] = {}
    if artifact.run_id is None:
        update["run_id"] = run_id
        if artifact.origin == ArtifactOrigin.UPLOAD.value:
            update["expires_at"] = None
    if auto_ingest and AUTO_INGEST_KEY not in artifact.metadata:
        update["metadata"] = {**artifact.metadata, AUTO_INGEST_KEY: AUTO_INGEST_PENDING}
    return artifact.model_copy(update=update, deep=True)


def auto_ingest_metadata(
    metadata: dict[str, Any], *, state: str, reason: str | None, attempts: int
) -> dict[str, Any]:
    recorded = {
        key: value
        for key, value in metadata.items()
        if key not in {AUTO_INGEST_REASON_KEY, AUTO_INGEST_ATTEMPTS_KEY}
    }
    recorded[AUTO_INGEST_KEY] = state
    if reason is not None:
        recorded[AUTO_INGEST_REASON_KEY] = reason
    if attempts:
        recorded[AUTO_INGEST_ATTEMPTS_KEY] = attempts
    return recorded


@dataclass(frozen=True, slots=True)
class AttachmentFacts:
    """What inspection learned from an upload's bytes; never from its claims."""

    media_type: str
    kind: str
    page_count: int | None = None

    def metadata(self) -> dict[str, Any]:
        facts: dict[str, Any] = {"kind": self.kind}
        if self.page_count is not None:
            facts["page_count"] = self.page_count
        return facts


@dataclass(frozen=True, slots=True)
class AttachmentRead:
    """One bounded read a model adapter asks the attachment resolver for."""

    artifact_id: UUID
    max_bytes: int


@dataclass(frozen=True, slots=True)
class AttachmentContent:
    """Bytes the resolver released for one attachment; never persisted."""

    artifact_id: UUID
    data: bytes
    truncated: bool = False
