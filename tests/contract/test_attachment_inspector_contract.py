"""AttachmentInspector contract (ADR-0120): the bytes decide, never the claim."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from agent_core.adapters.artifacts.inspection import SignatureAttachmentInspector
from agent_core.domain.artifacts import AttachmentFacts


def _pdf(pages: int, *, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    if password is not None:
        writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("content", "filename", "declared", "expected"),
    [
        (b"\x89PNG\r\n\x1a\nrest", "a.jpg", "image/jpeg", AttachmentFacts("image/png", "image")),
        (b"\xff\xd8\xffrest", "a.png", "image/png", AttachmentFacts("image/jpeg", "image")),
        (b"GIF89arest", "a", "application/octet-stream", AttachmentFacts("image/gif", "image")),
        (
            b"RIFF\x00\x00\x00\x00WEBPrest",
            "a",
            "image/webp",
            AttachmentFacts("image/webp", "image"),
        ),
        (
            _pdf(3),
            "a.bin",
            "application/octet-stream",
            AttachmentFacts("application/pdf", "pdf", 3),
        ),
        (
            _pdf(1, password="secret"),
            "a.pdf",
            "application/pdf",
            AttachmentFacts("application/pdf", "other"),
        ),
        (
            b"%PDF-1.4 damaged",
            "a.pdf",
            "application/pdf",
            AttachmentFacts("application/pdf", "other"),
        ),
        (
            "über".encode(),
            "notes.md",
            "application/octet-stream",
            AttachmentFacts("text/markdown", "text"),
        ),
        (b"a,b\n1,2", "t.csv", "text/csv", AttachmentFacts("text/csv", "text")),
        (
            b"\xff\xfe\xfd",
            "t.txt",
            "text/plain",
            AttachmentFacts("application/octet-stream", "other"),
        ),
        (
            b"not an image",
            "x.png",
            "image/png",
            AttachmentFacts("application/octet-stream", "other"),
        ),
        (b"PK\x03\x04zip", "a.zip", "application/zip", AttachmentFacts("application/zip", "other")),
    ],
)
async def test_inspection_classifies_from_bytes(
    content: bytes, filename: str, declared: str, expected: AttachmentFacts
) -> None:
    inspector = SignatureAttachmentInspector()
    facts = await inspector.inspect(content, filename=filename, declared_media_type=declared)
    assert facts == expected
