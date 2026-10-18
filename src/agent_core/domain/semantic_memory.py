"""Version-bound derived vectors. Originals retain all memory authority."""

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.memory import MemoryRecord, RecallQuery
from agent_core.domain.reconsolidation import SourceChange, SourceVersion


class EmbeddingSpace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=256)
    dimensions: int = Field(ge=1, le=3072)
    text_version: Literal["memory-text@1"] = "memory-text@1"

    @property
    def key(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class SemanticSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    record: MemoryRecord
    source: SourceVersion
    space: EmbeddingSpace
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Historical vectors bind their original revision and today's invalidation fence.
    current_fence: SourceVersion | None = None


def embedding_text(record: MemoryRecord) -> str:
    """Canonical local text for digesting; this grants no authority for provider egress."""
    return json.dumps([record.subject, record.statement], ensure_ascii=False, separators=(",", ":"))


def text_digest(record: MemoryRecord) -> str:
    return hashlib.sha256(embedding_text(record).encode()).hexdigest()


def checked_vector(vector: tuple[float, ...], space: EmbeddingSpace) -> tuple[float, ...]:
    if len(vector) != space.dimensions or any(not math.isfinite(v) for v in vector):
        raise ValueError("embedding dimensions and finite coordinates must match the space")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding must have a finite nonzero norm")
    return tuple(v / norm for v in vector)


class SemanticHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    record: MemoryRecord
    similarity: float = Field(ge=-1, le=1, allow_inf_nan=False)


class SemanticBounds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    creation: int = Field(ge=0)
    change: int = Field(ge=0)


class SemanticPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sources: tuple[SourceVersion, ...] = ()
    changes: tuple[SourceChange, ...] = ()
    cursor: int = Field(ge=0)


def check_page(after: int, through: int, limit: int) -> None:
    if not 0 <= after <= through or not 1 <= limit <= 256:
        raise ValueError("index pages require ordered cursors and 1..256 items")


def query_digest(query: RecallQuery) -> str:
    return hashlib.sha256(query.model_dump_json().encode()).hexdigest()


class SemanticQueryVector(BaseModel):
    """A local query-cache value, never evidence or authorization to export text."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    space: EmbeddingSpace
    vector: tuple[float, ...] = Field(min_length=1, max_length=3072)
