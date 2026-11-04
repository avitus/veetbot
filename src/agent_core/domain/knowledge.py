"""Knowledge-document ingestion and passage retrieval values."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Protocol
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
# the embedding request's per-item bound at a character boundary, and, for an
# encoder with a token capacity, windowed into runs of the passage that each
# fit it (`knowledge_embedding_windows`). A new version here re-embeds every
# chunk; a vector carries the version it used.
KNOWLEDGE_EMBEDDING_TEXT_VERSION = "knowledge-text@2"
KNOWLEDGE_EMBEDDING_TEXT_BYTES = 4096
# The space the arm embeds in with its own OpenAI provider (ADR-0290): the
# model gate 13 measured lift with, at its native width. A new revision here
# is a new space, and the backfill re-embeds every chunk into it.
KNOWLEDGE_OPENAI_SPACE = EmbeddingSpace(
    provider="openai",
    model="text-embedding-3-small",
    revision="knowledge-openai@2",
    dimensions=1536,
)
# The OpenAI route's window budget in the chunker's estimated tokens (ADR-0290):
# measured on the owner's manual, where whole 600-token chunks ranked the
# answering section 78th to 136th and 96-token windows first to seventh.
KNOWLEDGE_OPENAI_WINDOW_TOKENS = 128
# A chunk under this many estimated tokens stays one window: cutting a short
# topical section only sharpens its distracting sentences (ADR-0290).
KNOWLEDGE_OPENAI_WHOLE_TOKENS = 256
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


class KnowledgeReader(Protocol):
    """What the stores need to decide which documents a reader may see.

    Both the passage query and the catalog query carry these four fields;
    visibility, tenancy and the sensitivity ceiling are the same predicate
    for a search and for the catalog (ADR-0294).
    """

    @property
    def tenant_id(self) -> str: ...

    @property
    def principal_id(self) -> str: ...

    @property
    def current_scope(self) -> str | None: ...

    @property
    def sensitivity_ceiling(self) -> Sensitivity: ...


class KnowledgeCatalogQuery(BaseModel):
    """The documents on file for a reader at an instant, capped by count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    principal_id: str
    current_scope: str | None
    as_of: datetime
    sensitivity_ceiling: Sensitivity = Sensitivity.RESTRICTED
    max_documents: int = Field(ge=0)


class KnowledgeCatalogLine(BaseModel):
    """One current document: its title, its dates and how many passages it holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=1024)
    doc_date: date | None
    ingested_at: datetime
    passages: int = Field(ge=0)


class KnowledgeCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[KnowledgeCatalogLine, ...]
    omitted: int = Field(ge=0)


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
    # Which window of the chunk this vector embeds; a chunk the encoder admits
    # whole has one window, zero.
    window: int = Field(default=0, ge=0)
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
        seen: set[tuple[str, str, int]] = set()
        for embedding in self.embeddings:
            if embedding.chunk_id not in chunk_ids:
                raise ValueError("knowledge embedding names a chunk outside the ingest")
            key = (embedding.chunk_id, embedding.space.key, embedding.window)
            if key in seen:
                raise ValueError("knowledge embedding repeats a chunk window in one space")
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


def _heading_prefix(heading_path: list[str]) -> str:
    return " > ".join(part.strip() for part in heading_path if part.strip())


def _bounded_text(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES:
        return text
    return encoded[:KNOWLEDGE_EMBEDDING_TEXT_BYTES].decode("utf-8", errors="ignore").rstrip()


def _token_estimate(text: str) -> int:
    """The chunker's estimate, four bytes a token, for an encoder without a tokenizer."""

    return max(1, (len(text.encode("utf-8")) + 3) // 4)


# How a run of text splits when it does not fit the budget, finest last: at a
# paragraph break, after a sentence, between words, and, for a single word no
# encoder could admit, between characters. Each split keeps every character,
# so joining the pieces gives the run back.
_SPLITS = (
    re.compile(r"(?<=\n\n)"),
    re.compile(r"(?<=[.!?:;])(?=\s)"),
    re.compile(r"(?<=\s)(?=\S)"),
)


def _pieces(run: str, level: int) -> list[str]:
    if level < len(_SPLITS):
        pieces = [piece for piece in _SPLITS[level].split(run) if piece]
    else:
        half = max(1, len(run) // 2)
        pieces = [run[:half], run[half:]] if len(run) > 1 else [run]
    return pieces


# A question's words, in any script, with a possessive "'s" (straight or
# curly, either case, at the end of the word) kept with its word; a title's
# file extension, which the model may quote with the name; and the quotes
# and sentence punctuation that may sit against a name without being part
# of it.
_QUESTION_WORD = re.compile(r"[^\W_]+(?:['\u2019][sS](?!\w))?")
_FILE_EXTENSION = re.compile(r"\.[0-9A-Za-z]+$")
_POSSESSIVE = re.compile(r"'s(?!\w)")
_NAME_QUOTES = "\"'\u201c\u201d\u2018\u2019"
_NAME_EDGE = _NAME_QUOTES + "()[]{}<>.,;:!?"


def _name_key(text: str) -> str:
    """A name, or a run of question text, as it is compared.

    Case folds (Unicode case folding, so "Straße" is "STRASSE"), a possessive
    folds off, spacing collapses, and the quotes and sentence punctuation at
    either end are not part of the name; punctuation inside it is.
    """

    folded = text.casefold().replace("\u2019", "'")
    return " ".join(_POSSESSIVE.sub("", folded).split()).strip(_NAME_EDGE)


def _name_extent(text: str, first: int, last: int) -> tuple[int, int]:
    """The run of question text a run of words stands in: out to whitespace, less its edges.

    One rule serves comparison and removal: a word run grows over the
    characters joined to it ("C" in "C#" becomes "C#", "C++" is whole), then
    gives back the quotes and sentence punctuation at its ends, which the
    name does not own.
    """

    while first > 0 and not text[first - 1].isspace():
        first -= 1
    while last < len(text) and not text[last].isspace():
        last += 1
    while first < last and text[first] in _NAME_EDGE:
        first += 1
    while last > first and text[last - 1] in _NAME_EDGE:
        last -= 1
    return first, last


def knowledge_question_without_document_names(text: str, titles: Iterable[str]) -> str:
    """The question with the name of any visible document taken out (ADR-0293).

    A search asks the corpus, not the catalogue: a question that names a
    document by its title or file name, as a model does to aim at it, matches
    the document's cover page ahead of the passage it wants. Each name is
    matched as written, as a contiguous run of the question in any case and
    script, with or without its file extension and a possessive, and the
    quotes around it go with it; punctuation inside a name counts, so "C#"
    is not "C++", while the sentence punctuation after one stays. The words
    of a name that appear apart stay, as does a question that is nothing but
    a name.
    """

    asked = text
    # Composed and decomposed spellings of one letter are one word.
    text = unicodedata.normalize("NFC", text)
    names: dict[tuple[str, ...], set[str]] = {}
    for title in titles:
        title = unicodedata.normalize("NFC", title)
        for phrase in (title, _FILE_EXTENSION.sub("", title)):
            words = tuple(_name_key(match.group(0)) for match in _QUESTION_WORD.finditer(phrase))
            if words and all(words):
                names.setdefault(words, set()).add(_name_key(phrase))
    matches = list(_QUESTION_WORD.finditer(text))
    keys = [_name_key(match.group(0)) for match in matches]
    dropped = [False] * len(matches)
    for words, written in sorted(names.items(), key=lambda item: -len(item[0])):
        size = len(words)
        for start in range(len(keys) - size + 1):
            if tuple(keys[start : start + size]) != words:
                continue
            first, last = _name_extent(
                text, matches[start].start(), matches[start + size - 1].end()
            )
            if _name_key(text[first:last]) in written:
                dropped[start : start + size] = [True] * size
    if not any(dropped) or all(dropped):
        return asked
    kept: list[str] = []
    cursor = 0
    index = 0
    while index < len(matches):
        if not dropped[index]:
            index += 1
            continue
        end = index
        while end + 1 < len(matches) and dropped[end + 1]:
            end += 1
        first, last = _name_extent(text, matches[index].start(), matches[end].end())
        while first > cursor and text[first - 1] in _NAME_QUOTES:
            first -= 1
        while last < len(text) and text[last] in _NAME_QUOTES:
            last += 1
        kept.append(text[cursor:first])
        cursor = last
        index = end + 1
    kept.append(text[cursor:])
    return " ".join("".join(kept).split())


def knowledge_embedding_windows(
    heading_path: list[str],
    text: str,
    *,
    budget_tokens: int | None = None,
    count_tokens: Callable[[str], int] | None = None,
    whole_tokens: int | None = None,
) -> list[str]:
    """The texts a chunk is embedded as under `KNOWLEDGE_EMBEDDING_TEXT_VERSION`.

    `whole_tokens`, when given, is the length of text under which a chunk
    stays one window although it is over the budget: windows rescue a long
    section that mixes topics, and cutting a short topical one only sharpens
    its distracting sentences. It measures the chunk's text alone, as the
    chunker sizes a chunk, while the budget counts the heading too. The local
    encoder gives none, since its budget is a capacity the provider refuses
    to exceed.

    Without a budget the chunk is one window, the text `knowledge_embedding_text`
    gives. With one, the encoder's capacity, the chunk's text is cut into runs
    at paragraph, then sentence, then word boundaries, and each window is the
    heading path over a run that counts under the budget by `count_tokens`,
    the encoder's own tokenizer, or the chunker's estimate when none is
    given. Windows are ordered, non-empty, and together hold every character
    of the text once, so a passage is reachable from any of its sentences.
    """

    whole = knowledge_embedding_text(heading_path, text)
    if budget_tokens is None:
        return [whole]
    count = count_tokens or _token_estimate
    unbounded = _heading_prefix(heading_path)
    unbounded = f"{unbounded}\n{text}" if unbounded else text
    # One window only when the byte bound cut nothing from it; otherwise the
    # windows below each fit the bound, so no character is lost to it.
    if len(unbounded.encode("utf-8")) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES and (
        count(whole) <= budget_tokens or (whole_tokens is not None and count(text) < whole_tokens)
    ):
        return [whole]
    # A heading has no length limit. The prefix keeps its deepest levels and
    # drops the outer ones until it leaves half the budget, in tokens and in
    # bytes, for text; a prefix no level of which fits is left out.
    path = list(heading_path)
    while path and (
        count(_heading_prefix(path)) > budget_tokens // 2
        or len(_heading_prefix(path).encode("utf-8")) > KNOWLEDGE_EMBEDDING_TEXT_BYTES // 2
    ):
        path = path[1:]
    prefix = _heading_prefix(path)

    def raw(body: str) -> str:
        body = body.strip()
        return f"{prefix}\n{body}" if prefix else body

    def window(body: str) -> str:
        return _bounded_text(raw(body))

    def fits(body: str) -> bool:
        text = raw(body)
        return (
            len(text.encode("utf-8")) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES
            and count(text) <= budget_tokens
        )

    def pack(run: str, level: int) -> list[str]:
        """Fill windows greedily from the run's pieces at this level; refine any that overflow."""

        bodies: list[str] = []
        current = ""
        for piece in _pieces(run, level):
            if fits(current + piece):
                current += piece
                continue
            if current.strip():
                bodies.append(current)
            current = ""
            if fits(piece):
                current = piece
            elif len(piece.strip()) > 1 or level < len(_SPLITS):
                bodies.extend(pack(piece, level + 1))
            else:
                # One character the encoder cannot admit on its own; it still
                # has to land somewhere, and the request bound still holds.
                bodies.append(piece)
        if current.strip():
            bodies.append(current)
        return bodies

    return [window(body) for body in pack(text, 0) if body.strip()]


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
