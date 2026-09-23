"""Consent-gated, perishable trajectory export values."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from agent_core.domain.policies import TrustLevel


class ExportConsent(BaseModel):
    tenant_id: str
    principal_id: str
    granted_at: datetime
    withdrawn_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.withdrawn_at is None


class ArtifactRef(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    session_id: UUID
    # Null only before an ADR-0120 upload is claimed by the run of a sent
    # message; the artifacts table enforces the same rule with a constraint.
    run_id: UUID | None
    name: str
    media_type: str
    storage_uri: str
    sha256: str
    size_bytes: int
    origin: Literal[
        "trajectory_export",
        "sandbox_export",
        "tool_output",
        "model_output",
        "upload",
        "knowledge_source",
    ] = "trajectory_export"
    trust: TrustLevel
    expires_at: datetime | None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _run_required_outside_uploads(self) -> ArtifactRef:
        if self.run_id is None and self.origin not in {"upload", "knowledge_source"}:
            raise ValueError("only an upload or a knowledge source may have no run")
        return self


class TrajectoryExport(BaseModel):
    export_id: UUID
    tenant_id: str
    principal_id: str
    run_id: UUID
    artifact: ArtifactRef
    builder_version: str
    ruleset_version: str
    created_at: datetime


class RedactionSummary(BaseModel):
    ruleset_version: str
    replacements: dict[str, int] = Field(default_factory=dict)
