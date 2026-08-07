"""Knowledge extractor contract."""

from collections.abc import AsyncIterator

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
