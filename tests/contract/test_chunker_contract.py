"""Deterministic structure-first chunking contract."""

from uuid import UUID

from agent_core.knowledge.chunking import CEILING_TOKENS, DeterministicChunker


def test_chunker_is_stable_and_never_exceeds_the_ceiling() -> None:
    chunker = DeterministicChunker()
    text = "# Heading\n\n" + "word " * 2500
    first = chunker.chunk(
        text,
        "Test",
        document_row_id=UUID(int=1),
        document_id=UUID(int=2),
        version=1,
    )
    assert first
    assert first == chunker.chunk(
        text,
        "Test",
        document_row_id=UUID(int=1),
        document_id=UUID(int=2),
        version=1,
    )
    assert all(item.tokens <= CEILING_TOKENS for item in first)
    next_version = chunker.chunk(
        text,
        "Test",
        document_row_id=UUID(int=3),
        document_id=UUID(int=2),
        version=2,
    )
    other_document = chunker.chunk(
        text,
        "Test",
        document_row_id=UUID(int=4),
        document_id=UUID(int=5),
        version=1,
    )
    assert {item.chunk_id for item in first}.isdisjoint(item.chunk_id for item in next_version)
    assert {item.chunk_id for item in first}.isdisjoint(item.chunk_id for item in other_document)


def test_chunker_does_not_merge_small_sections_across_headings() -> None:
    chunks = DeterministicChunker().chunk(
        "# First\n\nshort first\n\n# Second\n\nshort second",
        "Test",
        document_row_id=UUID(int=10),
        document_id=UUID(int=11),
        version=1,
    )
    assert [item.heading_path for item in chunks] == [["First"], ["Second"]]
