"""PDF text extraction for knowledge ingestion (ADR-0120)."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator

from pypdf import PdfReader

from agent_core.adapters.pdf_process import parse_pdf
from agent_core.domain.errors import ToolValidationError

PDF_MEDIA_TYPE = "application/pdf"


def _pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
    except Exception as exc:  # pypdf raises many unrelated types for damaged input
        raise ToolValidationError("knowledge source could not be read") from exc
    if reader.is_encrypted:
        raise ToolValidationError("knowledge source is encrypted")
    headings = _outline_headings(reader)
    pages: list[str] = []
    try:
        for number, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            lead = "\n".join(f"{'#' * depth} {title}" for depth, title in headings.get(number, ()))
            if lead:
                text = f"{lead}\n\n{text}" if text else lead
            pages.append(text)
    except Exception as exc:
        raise ToolValidationError("knowledge source could not be read") from exc
    return "\n\n".join(text for text in pages if text)


def _outline_headings(reader: PdfReader) -> dict[int, list[tuple[int, str]]]:
    """The document's bookmarks as Markdown headings on the pages they open.

    A manual's sections then become the chunks' heading paths, the way a
    Markdown document's headings do, so a passage about parking carries
    "Parking > Park Distance Control" into the index (ADR-0284). A PDF without
    an outline, or one whose outline cannot be read, keeps plain page text.
    """

    headings: dict[int, list[tuple[int, str]]] = {}

    def walk(items: object, depth: int) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            try:
                title = " ".join(str(item.title).split())
                page = reader.get_destination_page_number(item)
            except Exception:
                continue
            if title and isinstance(page, int) and page >= 0:
                headings.setdefault(page, []).append((min(depth, 6), title))

    try:
        walk(reader.outline, 1)
    except Exception:
        return {}
    return headings


class PdfTextExtractor:
    """Extract the text layer of an unencrypted PDF; a scan without one has no text."""

    def __init__(self, *, maximum_bytes: int, timeout_seconds: float = 60.0) -> None:
        if maximum_bytes <= 0:
            raise ValueError("knowledge source byte ceiling must be positive")
        self._maximum_bytes = maximum_bytes
        self._timeout_seconds = timeout_seconds

    def media_types(self) -> set[str]:
        return {PDF_MEDIA_TYPE}

    async def extract(self, source: AsyncIterator[bytes], media_type: str) -> str:
        if media_type != PDF_MEDIA_TYPE:
            raise ToolValidationError(f"unsupported knowledge media type {media_type!r}")
        content = bytearray()
        async for chunk in source:
            if len(content) + len(chunk) > self._maximum_bytes:
                raise ToolValidationError("knowledge source exceeds the byte ceiling")
            content.extend(chunk)
        try:
            result = await parse_pdf(
                bytes(content), operation="text", timeout=self._timeout_seconds
            )
            if not isinstance(result, str):
                raise ToolValidationError("knowledge source could not be read")
            return result
        except (TimeoutError, OSError) as exc:
            raise ToolValidationError("knowledge source could not be read") from exc
