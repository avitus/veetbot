"""Versioned procedural-memory packages and session catalog values."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from agent_core.domain.policies import TrustLevel

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SKILL_REF_PATTERN = re.compile(
    r"^(?P<name>[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)(?:@(?P<revision>[1-9][0-9]*))?$"
)


class SkillSource(StrEnum):
    BUILTIN = "builtin"
    OPERATOR = "operator"
    AGENT = "agent"
    MCP = "mcp"


class SkillStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SkillManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    description: str
    required_tools: tuple[str, ...] = ()


class SkillPackageMember(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    data: bytes = b""
    kind: Literal["file", "symlink"] = "file"


class SkillPackage(BaseModel):
    """Untrusted package input before validation and canonical archiving."""

    model_config = ConfigDict(frozen=True)

    directory_name: str
    members: tuple[SkillPackageMember, ...]


class SkillPackagePut(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    created: bool


class ValidatedSkillPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: SkillManifest
    body: str
    body_tokens: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive: bytes
    package_bytes: int = Field(gt=0)
    file_count: int = Field(gt=0)


class AuthoringContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    principal_id: str
    invocation_id: UUID
    idempotency_key: str = Field(min_length=1)


class SkillRevision(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: UUID
    tenant_id: str
    revision: PositiveInt
    manifest: SkillManifest
    body: str
    body_tokens: PositiveInt
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_key: str
    package_bytes: PositiveInt
    file_count: PositiveInt
    source: SkillSource
    trust: TrustLevel
    status: SkillStatus
    authored_by_run_id: UUID | None = None
    authored_by_principal_id: str | None = None
    authored_by_invocation_id: UUID | None = None
    authoring_idempotency_key: str | None = None
    archived_by_invocation_id: UUID | None = None
    archive_idempotency_key: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def provenance_matches_source(self) -> SkillRevision:
        durable_provenance = (
            self.authored_by_principal_id,
            self.authored_by_invocation_id,
            self.authoring_idempotency_key,
        )
        has_durable_provenance = all(value is not None for value in durable_provenance)
        if any(value is not None for value in durable_provenance) != has_durable_provenance:
            raise ValueError("agent skill provenance must be complete")
        if self.source is not SkillSource.AGENT and self.authored_by_run_id is not None:
            raise ValueError("non-agent skills cannot carry authoring run provenance")
        if (self.source is SkillSource.AGENT) != has_durable_provenance:
            raise ValueError("agent skill provenance must match its source")
        if (self.archived_by_invocation_id is None) != (self.archive_idempotency_key is None):
            raise ValueError("archive idempotency provenance must be complete")
        return self


class SkillRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    revision: PositiveInt | None = None

    @classmethod
    def parse(cls, text: str) -> SkillRef:
        matched = SKILL_REF_PATTERN.fullmatch(text)
        if matched is None:
            raise ValueError(f"invalid skill reference {text!r}")
        revision = matched.group("revision")
        return cls(name=matched.group("name"), revision=None if revision is None else int(revision))

    def __str__(self) -> str:
        return self.name if self.revision is None else f"{self.name}@{self.revision}"


class SkillPin(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    revision: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: SkillManifest
    revision: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust: TrustLevel
    source: SkillSource
    package_key: str | None = None
    ephemeral_body: str | None = None

    @property
    def pin(self) -> SkillPin:
        return SkillPin(
            name=self.manifest.name,
            revision=self.revision,
            content_sha256=self.content_sha256,
        )

    @property
    def metadata(self) -> CatalogMetadata:
        return CatalogMetadata(
            manifest=self.manifest,
            revision=self.revision,
            content_sha256=self.content_sha256,
            trust=self.trust,
            source=self.source,
        )


class CatalogMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: SkillManifest
    revision: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust: TrustLevel
    source: SkillSource


class SessionSkillCatalog(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[CatalogEntry, ...] = ()
    dropped_names: tuple[str, ...] = ()

    @property
    def pins(self) -> tuple[SkillPin, ...]:
        return tuple(entry.pin for entry in self.entries)


class LoadedSkillBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    revision: int = Field(ge=0)
    path: str | None = None
    content: str
    tokens: PositiveInt
    trust: TrustLevel
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
