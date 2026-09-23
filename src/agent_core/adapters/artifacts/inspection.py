"""Detect what an uploaded chat attachment is from its bytes (ADR-0118)."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import PurePosixPath

from pypdf import PdfReader

from agent_core.domain.artifacts import AttachmentFacts
from agent_core.model.attachments import (
    IMAGE_MEDIA_TYPES,
    PDF_MEDIA_TYPE,
    AttachmentKind,
    is_text_media_type,
)

logger = logging.getLogger(__name__)

OCTET_STREAM = "application/octet-stream"
_PDF_SIGNATURE_WINDOW = 1024
# A fixed table rather than the platform's MIME database, so detection does
# not depend on the host's /etc/mime.types.
_TEXT_EXTENSIONS: dict[str, str] = {
    ".txt": "text/plain",
    ".text": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".py": "text/x-python",
    ".swift": "text/x-swift",
    ".sql": "application/sql",
    ".sh": "application/x-sh",
}


def _image_signature(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _pdf_pages(content: bytes) -> int | None:
    """Count pages, or None for an encrypted or unreadable document."""

    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            return None
        return len(reader.pages)
    except Exception:  # pypdf raises many unrelated types for damaged input
        logger.info("attachment_pdf_unreadable")
        return None


def _is_utf8(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


class SignatureAttachmentInspector:
    """Trust the bytes first, the declared type second, the name last."""

    def __init__(self, *, pdf_timeout_seconds: float = 10.0) -> None:
        self._pdf_timeout_seconds = pdf_timeout_seconds

    async def inspect(
        self, content: bytes, *, filename: str, declared_media_type: str
    ) -> AttachmentFacts:
        image = _image_signature(content)
        if image is not None:
            return AttachmentFacts(media_type=image, kind=AttachmentKind.IMAGE.value)
        if b"%PDF-" in content[:_PDF_SIGNATURE_WINDOW]:
            try:
                pages = await asyncio.wait_for(
                    asyncio.to_thread(_pdf_pages, content), timeout=self._pdf_timeout_seconds
                )
            except TimeoutError:
                logger.info("attachment_pdf_inspection_timeout")
                pages = None
            if pages is None:
                return AttachmentFacts(media_type=PDF_MEDIA_TYPE, kind=AttachmentKind.OTHER.value)
            return AttachmentFacts(
                media_type=PDF_MEDIA_TYPE, kind=AttachmentKind.PDF.value, page_count=pages
            )
        declared = declared_media_type
        if declared == OCTET_STREAM:
            declared = _TEXT_EXTENSIONS.get(PurePosixPath(filename).suffix.lower(), OCTET_STREAM)
        if is_text_media_type(declared):
            if _is_utf8(content):
                return AttachmentFacts(media_type=declared, kind=AttachmentKind.TEXT.value)
            return AttachmentFacts(media_type=OCTET_STREAM, kind=AttachmentKind.OTHER.value)
        if declared in IMAGE_MEDIA_TYPES or declared == PDF_MEDIA_TYPE:
            # A type this platform reads, contradicted by the bytes.
            return AttachmentFacts(media_type=OCTET_STREAM, kind=AttachmentKind.OTHER.value)
        return AttachmentFacts(media_type=declared, kind=AttachmentKind.OTHER.value)
