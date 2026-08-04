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
    assert first == chunker.chunk(
        text,
        "Test",
        document_row_id=UUID(int=1),
        document_id=UUID(int=2),
        version=1,
    )
    assert all(item.tokens <= CEILING_TOKENS for item in first)
