"""Knowledge extractor contract."""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from agent_core.domain.errors import ToolValidationError
from agent_core.knowledge.chunking import PlainTextExtractor


async def _source(value: bytes) -> AsyncIterator[bytes]:
    yield value


async def test_extractor_accepts_only_declared_utf8_text() -> None:
    extractor = PlainTextExtractor()
    assert await extractor.extract(_source(b"hello"), "text/plain") == "hello"
    with pytest.raises(ToolValidationError):
        await extractor.extract(_source(b"hello"), "application/pdf")
    with pytest.raises(ToolValidationError, match="UTF-8"):
        await extractor.extract(_source(b"\xff"), "text/plain")


async def test_pdf_extractor_reads_the_text_layer_and_refuses_what_it_cannot() -> None:
    """ADR-0120: owner-sent PDFs reach knowledge through their text layer."""

    import io

    from pypdf import PdfWriter

    from agent_core.adapters.knowledge.pdf import PdfTextExtractor
    from tests.gates.test_attachment_auto_ingest_adr0120 import _text_pdf

    extractor = PdfTextExtractor(maximum_bytes=1024 * 1024)
    assert extractor.media_types() == {"application/pdf"}
    text = await extractor.extract(_source(_text_pdf("Revenue grew")), "application/pdf")
    assert "Revenue grew" in text

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(ToolValidationError, match="encrypted"):
        await extractor.extract(_source(encrypted.getvalue()), "application/pdf")
    with pytest.raises(ToolValidationError, match="could not be read"):
        await extractor.extract(_source(b"%PDF-1.4 broken"), "application/pdf")
    with pytest.raises(ToolValidationError, match="byte ceiling"):
        await PdfTextExtractor(maximum_bytes=4).extract(_source(b"%PDF-1.4"), "application/pdf")
    with pytest.raises(ToolValidationError):
        await extractor.extract(_source(b"hello"), "text/plain")


async def test_routing_extractor_dispatches_by_declared_type() -> None:
    from agent_core.adapters.knowledge.pdf import PdfTextExtractor
    from agent_core.knowledge.chunking import RoutingExtractor

    routing = RoutingExtractor([PlainTextExtractor(), PdfTextExtractor(maximum_bytes=1024)])
    assert routing.media_types() == {"text/plain", "text/markdown", "application/pdf"}
    assert await routing.extract(_source(b"hi"), "text/markdown") == "hi"
    with pytest.raises(ToolValidationError, match="unsupported"):
        await routing.extract(_source(b"hi"), "application/zip")
    with pytest.raises(ValueError, match="two extractors"):
        RoutingExtractor([PlainTextExtractor(), PlainTextExtractor()])


async def test_pdf_extractor_turns_bookmarks_into_headings() -> None:
    """ADR-0284: a PDF outline becomes Markdown headings, so chunks carry heading paths."""

    import io

    from pypdf import PdfWriter

    from agent_core.adapters.knowledge.pdf import PdfTextExtractor
    from agent_core.knowledge.chunking import DeterministicChunker
    from tests.gates.test_attachment_auto_ingest_adr0120 import _text_pdf

    writer = PdfWriter(clone_from=io.BytesIO(_text_pdf("The valve clicks after parking.")))
    writer.add_blank_page(width=200, height=200)
    parent = writer.add_outline_item("Parking", 0)
    writer.add_outline_item("Fuel system check", 0, parent=parent)
    writer.add_outline_item("Appendix", 1)
    buffer = io.BytesIO()
    writer.write(buffer)

    text = await PdfTextExtractor(maximum_bytes=1024 * 1024).extract(
        _source(buffer.getvalue()), "application/pdf"
    )

    assert text.startswith("# Parking\n## Fuel system check\n\nThe valve clicks after parking.")
    assert text.endswith("# Appendix")
    chunks = DeterministicChunker().chunk(
        text, "Manual", document_row_id=UUID(int=1), document_id=UUID(int=2), version=1
    )
    assert chunks[0].heading_path == ["Parking", "Fuel system check"]
