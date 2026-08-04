"""Closed normalization, text extraction, and deterministic structure-first chunks."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import AsyncIterator
from uuid import UUID

from agent_core.domain.errors import ToolValidationError
from agent_core.domain.knowledge import KnowledgeChunk

CHUNKER_VERSION = "knowledge-chunker@1"
TARGET_TOKENS = 600
CEILING_TOKENS = 1_000
FLOOR_TOKENS = 100
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_INSTRUCTION = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|"
    r"you\s+must|do\s+not\s+follow|override\s+(?:policy|instructions))",
    re.I,
)


class PlainTextExtractor:
    def media_types(self) -> set[str]:
        return {"text/plain", "text/markdown"}

    async def extract(self, source: AsyncIterator[bytes], media_type: str) -> str:
        if media_type not in self.media_types():
            raise ToolValidationError(f"unsupported knowledge media type {media_type!r}")
        content = bytearray()
        async for chunk in source:
            content.extend(chunk)
        try:
            return bytes(content).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolValidationError("knowledge source is not valid UTF-8") from exc


def normalize_text(text: str, *, tab_width: int = 4) -> str:
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.expandtabs(tab_width).rstrip() for line in normalized.split("\n")]
    result: list[str] = []
    blank_count = 0
    for line in lines:
        if line:
            blank_count = 0
            result.append(line)
            continue
        blank_count += 1
        if blank_count <= 2:
            result.append("")
    return "\n".join(result).strip()


class DeterministicChunker:
    version = CHUNKER_VERSION

    def chunk(
        self, text: str, title: str, *, document_row_id: UUID, document_id: UUID, version: int
    ) -> list[KnowledgeChunk]:
        sections = _sections(text)
        drafts: list[tuple[list[str], str]] = []
        for heading_path, body in sections:
            paragraphs = [part.strip() for part in re.split(r"\n{2,}", body) if part.strip()]
            current: list[str] = []
            current_tokens = 0
            for paragraph in paragraphs:
                for piece in _split_ceiling(paragraph):
                    piece_tokens = token_estimate(piece)
                    if current and current_tokens + piece_tokens > TARGET_TOKENS:
                        drafts.append((heading_path, "\n\n".join(current)))
                        current = []
                        current_tokens = 0
                    current.append(piece)
                    current_tokens += piece_tokens
            if current:
                drafts.append((heading_path, "\n\n".join(current)))
        if not drafts and text:
            drafts = [([], piece) for piece in _split_ceiling(text)]
        merged: list[tuple[list[str], str]] = []
        for heading_path, body in drafts:
            if merged and token_estimate(body) < FLOOR_TOKENS:
                previous_path, previous = merged[-1]
                combined = f"{previous}\n\n{body}"
                if token_estimate(combined) <= CEILING_TOKENS:
                    merged[-1] = (previous_path, combined)
                    continue
            merged.append((heading_path, body))
        result: list[KnowledgeChunk] = []
        for ordinal, (heading_path, body) in enumerate(merged):
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            identity = "\0".join((self.version, title, *heading_path, str(ordinal), digest)).encode(
                "utf-8"
            )
            chunk_id = f"kc_{hashlib.sha256(identity).hexdigest()[:16]}"
            result.append(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    document_row_id=document_row_id,
                    document_id=document_id,
                    version=version,
                    ordinal=ordinal,
                    heading_path=heading_path,
                    text=body,
                    tokens=token_estimate(body),
                    contains_instruction_like_text=_INSTRUCTION.search(body) is not None,
                    content_sha256=digest,
                )
            )
        return result


def token_estimate(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def _sections(text: str) -> list[tuple[list[str], str]]:
    path: list[str] = []
    body: list[str] = []
    sections: list[tuple[list[str], str]] = []

    def flush() -> None:
        if any(line.strip() for line in body):
            sections.append((list(path), "\n".join(body).strip()))
        body.clear()

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match is None:
            body.append(line)
            continue
        flush()
        depth = len(match.group(1))
        path[:] = [*path[: depth - 1], match.group(2).strip()]
    flush()
    return sections


def _split_ceiling(paragraph: str) -> list[str]:
    if token_estimate(paragraph) <= CEILING_TOKENS:
        return [paragraph]
    words = paragraph.split()
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and token_estimate(candidate) > CEILING_TOKENS:
            pieces.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces
