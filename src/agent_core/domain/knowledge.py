"""Knowledge-document ingestion and passage retrieval values."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from agent_core.domain.memory import Sensitivity
from agent_core.domain.trajectory import ArtifactRef


class KnowledgeVisibility(StrEnum):
    PRINCIPAL = "principal"
    PROJECT = "project"
    TENANT = "tenant"


class DocumentAuthority(StrEnum):
    PRINCIPAL_AUTHORED = "principal_authored"
    PRINCIPAL_SUPPLIED = "principal_supplied"
    FETCHED = "fetched"


class KnowledgeDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: UUID
    document_id: UUID
    tenant_id: str
    ingested_by_principal_id: str
    visibility: KnowledgeVisibility
    project_scope: str | None = None
    title: str = Field(min_length=1, max_length=1024)
    source_ref: ArtifactRef
    media_type: str
    doc_date: date | None = None
    authority: DocumentAuthority
    version: PositiveInt
    chunker_version: str
    superseded_by: UUID | None = None
    valid_from: datetime
    valid_to: datetime | None = None
    ingested_at: datetime
    sensitivity: Sensitivity

    @model_validator(mode="after")
    def project_visibility_has_scope(self) -> KnowledgeDocument:
        if self.visibility is KnowledgeVisibility.PROJECT and not self.project_scope:
            raise ValueError("project-visible knowledge requires project_scope")
        if self.visibility is not KnowledgeVisibility.PROJECT and self.project_scope is not None:
            raise ValueError("only project-visible knowledge may carry project_scope")
        return self


class KnowledgeChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(pattern=r"^kc_[0-9a-f]{16}$")
    document_row_id: UUID
    document_id: UUID
    version: PositiveInt
    ordinal: int = Field(ge=0)
    heading_path: list[str]
    text: str = Field(min_length=1)
    tokens: PositiveInt
    contains_instruction_like_text: bool
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class KnowledgeQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    principal_id: str
    current_scope: str | None
    text: str = Field(min_length=1)
    as_of: datetime | None = None
    budget_tokens: PositiveInt
    max_passages: PositiveInt
    max_per_document: PositiveInt = 2
    min_score: float = Field(ge=0, le=1)
    sensitivity_ceiling: Sensitivity = Sensitivity.RESTRICTED


class RetrievedPassage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    document_id: UUID
    title: str
    heading_path: list[str]
    text: str
    doc_date: date | None
    authority: DocumentAuthority
    sensitivity: Sensitivity
    score: float
    arms: list[str]
    instruction_like: bool


class KnowledgeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passages: list[RetrievedPassage]
    rendered: str
    tokens: int = Field(ge=0)
    truncated: bool
    trace_id: UUID | None = None


class KnowledgeIngestRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: ArtifactRef
    title: str = Field(min_length=1, max_length=1024)
    visibility: KnowledgeVisibility
    project_scope: str | None = None
    document_id: UUID | None = None
    doc_date: date | None = None
    authority: DocumentAuthority = DocumentAuthority.PRINCIPAL_SUPPLIED
    sensitivity: Sensitivity = Sensitivity.INTERNAL


class KnowledgeIngestPrepared(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document: KnowledgeDocument
    chunks: list[KnowledgeChunk] = Field(min_length=1)
