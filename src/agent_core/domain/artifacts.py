"""Artifact metadata carried across the streaming storage boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from agent_core.domain.policies import TrustLevel


class ArtifactOrigin(StrEnum):
    SANDBOX_EXPORT = "sandbox_export"
    TOOL_OUTPUT = "tool_output"
    MODEL_OUTPUT = "model_output"
    UPLOAD = "upload"
    TRAJECTORY_EXPORT = "trajectory_export"
    KNOWLEDGE_SOURCE = "knowledge_source"


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    artifact_id: UUID
    tenant_id: str
    principal_id: str
    session_id: UUID
    run_id: UUID
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
