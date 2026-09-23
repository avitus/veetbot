"""PDF text extraction for knowledge ingestion (ADR-0120)."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator

from pypdf import PdfReader

from agent_core.domain.errors import ToolValidationError

PDF_MEDIA_TYPE = "application/pdf"


def _pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
    except Exception as exc:  # pypdf raises many unrelated types for damaged input
        raise ToolValidationError("knowledge source could not be read") from exc
    if reader.is_encrypted:
        raise ToolValidationError("knowledge source is encrypted")
    pages: list[str] = []
    try:
        for page in reader.pages:
            pages.append(page.extract_text() or "")
    except Exception as exc:
        raise ToolValidationError("knowledge source could not be read") from exc
    return "\n\n".join(text.strip() for text in pages if text.strip())


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
            return await asyncio.wait_for(
                asyncio.to_thread(_pdf_text, bytes(content)), timeout=self._timeout_seconds
            )
        except TimeoutError as exc:
            raise ToolValidationError("knowledge source could not be read") from exc
