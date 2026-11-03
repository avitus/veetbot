"""Knowledge-document ingestion and passage retrieval values."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from agent_core.domain.memory import Sensitivity
from agent_core.domain.retrieval_fusion import (
    RECIPROCAL_RANK_FUSION_K,
    blend_with_arm_score,
    reciprocal_rank_fusion,
)
from agent_core.domain.semantic_memory import EmbeddingSpace, checked_vector
from agent_core.domain.trajectory import ArtifactRef

# The text a chunk is embedded as: its heading path over its passage, cut to
# the embedding request's per-item bound at a character boundary. A new
# version here re-embeds every chunk; a vector carries the version it used.
KNOWLEDGE_EMBEDDING_TEXT_VERSION = "knowledge-text@1"
KNOWLEDGE_EMBEDDING_TEXT_BYTES = 4096
# The semantic arm's share of a passage's match, memory's `semantic_fusion_weight`.
KNOWLEDGE_SEMANTIC_FUSION_WEIGHT = 0.2
# A semantic hit below this cosine similarity is no candidate: the lexical arm
# returns only chunks holding a query term, and the semantic arm must likewise
# return only chunks near the question, or an unrelated question would be
# answered with the nearest unrelated passage. Calibrated on the recorded
# paraphrase fixture (ADR-0289): answering passages score 0.348 and above,
# passages of the other documents 0.333 and below.
KNOWLEDGE_SEMANTIC_MIN_SIMILARITY = 0.3


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
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("knowledge valid_to precedes valid_from")
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
    trace_id: UUID


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


class KnowledgeChunkEmbedding(BaseModel):
    """A chunk's derived vector in one embedding space; the chunk stays authoritative."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(pattern=r"^kc_[0-9a-f]{16}$")
    space: EmbeddingSpace
    text_version: str = KNOWLEDGE_EMBEDDING_TEXT_VERSION
    vector: tuple[float, ...] = Field(min_length=1, max_length=3072, repr=False)

    @model_validator(mode="after")
    def vector_fits_space(self) -> KnowledgeChunkEmbedding:
        checked_vector(self.vector, self.space)
        return self


# How the semantic arm meets the lexical arm, memory's `semantic_channel`:
# `hybrid` fuses both arms by reciprocal rank; `dense` orders candidates by
# the semantic arm alone, the configuration the paraphrase gate measured
# lift under (ADR-0289).
KnowledgeSemanticChannel = Literal["hybrid", "dense"]


class KnowledgeSemanticQuery(BaseModel):
    """The question embedded in the space the stored chunk vectors share."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    space: EmbeddingSpace
    vector: tuple[float, ...] = Field(min_length=1, max_length=3072, repr=False)
    channel: KnowledgeSemanticChannel = "hybrid"

    @model_validator(mode="after")
    def vector_fits_space(self) -> KnowledgeSemanticQuery:
        checked_vector(self.vector, self.space)
        return self


class KnowledgeIngestPrepared(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document: KnowledgeDocument
    chunks: list[KnowledgeChunk] = Field(min_length=1)
    embeddings: tuple[KnowledgeChunkEmbedding, ...] = ()

    @model_validator(mode="after")
    def embeddings_belong_to_chunks(self) -> KnowledgeIngestPrepared:
        chunk_ids = {chunk.chunk_id for chunk in self.chunks}
        seen: set[tuple[str, str]] = set()
        for embedding in self.embeddings:
            if embedding.chunk_id not in chunk_ids:
                raise ValueError("knowledge embedding names a chunk outside the ingest")
            key = (embedding.chunk_id, embedding.space.key)
            if key in seen:
                raise ValueError("knowledge embedding repeats a chunk in one space")
            seen.add(key)
        return self


def knowledge_embedding_text(heading_path: list[str], text: str) -> str:
    """The text a chunk is embedded as under `KNOWLEDGE_EMBEDDING_TEXT_VERSION`."""

    path = " > ".join(part.strip() for part in heading_path if part.strip())
    combined = f"{path}\n{text}" if path else text
    encoded = combined.encode("utf-8")
    if len(encoded) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES:
        return combined
    return encoded[:KNOWLEDGE_EMBEDDING_TEXT_BYTES].decode("utf-8", errors="ignore").rstrip()


def fuse_passage_arms(
    lexical: list[tuple[str, float]],
    semantic: list[tuple[str, float]],
    *,
    channel: KnowledgeSemanticChannel = "hybrid",
    k: int = RECIPROCAL_RANK_FUSION_K,
    weight: float = KNOWLEDGE_SEMANTIC_FUSION_WEIGHT,
) -> dict[str, tuple[float, list[str]]]:
    """Fuse the lexical and semantic arms the way belief recall fuses its arms.

    Each arm lists its chunks best first with the arm's own 0..1 score: the
    lexical arm's coverage share, the semantic arm's cosine similarity. A
    semantic hit below `KNOWLEDGE_SEMANTIC_MIN_SIMILARITY` is dropped first.
    Under the `hybrid` channel a chunk's match is its best arm score blended
    with its reciprocal-rank fusion across both arms, the blend
    `HybridMemoryRetriever` applies after `_rrf_fuse`. Under `dense` the
    semantic arm's similarity is the match, as memory's dense channel skips
    its sparse query, and the lexical arm covers only the chunks the space
    has not seen. The result maps a chunk id to its match and the arms that
    returned it; both stores order candidates by it.
    """

    semantic = [
        (chunk_id, similarity)
        for chunk_id, similarity in semantic
        if similarity >= KNOWLEDGE_SEMANTIC_MIN_SIMILARITY
    ]
    if channel == "dense":
        # The lexical arm reaches only chunks the space has not seen (both
        # stores restrict it so), ranked by coverage beside the semantic hits:
        # a document ingested before the arm was on stays reachable.
        matches = {
            chunk_id: (max(0.0, min(1.0, similarity)), ["semantic"])
            for chunk_id, similarity in semantic
        }
        matches.update({chunk_id: (coverage, ["lexical"]) for chunk_id, coverage in lexical})
        return matches
    fused = reciprocal_rank_fusion(
        {
            "lexical": [chunk_id for chunk_id, _ in lexical],
            "semantic": [chunk_id for chunk_id, _ in semantic],
        },
        k=k,
    )
    best: dict[str, float] = {}
    arms: dict[str, list[str]] = {}
    for arm, members in (("lexical", lexical), ("semantic", semantic)):
        for chunk_id, score in members:
            best[chunk_id] = max(best.get(chunk_id, 0.0), max(0.0, min(1.0, score)))
            arms.setdefault(chunk_id, []).append(arm)
    return {
        chunk_id: (
            blend_with_arm_score(best[chunk_id], fused[chunk_id], weight=weight),
            arms[chunk_id],
        )
        for chunk_id in best
    }
