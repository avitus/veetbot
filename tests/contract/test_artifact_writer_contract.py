"""Artifact writer surface contract."""

from collections.abc import AsyncIterator
from typing import cast

from agent_core.domain.policies import TrustLevel
from agent_core.ports.artifacts import ArtifactWriter


class _RecordingWriter:
    def __init__(self) -> None:
        self.seen = b""

    async def create(
        self,
        stream: AsyncIterator[bytes],
        filename: str,
        media_type: str,
        trust: TrustLevel,
    ) -> object:
        del filename, media_type, trust
        self.seen = b"".join([chunk async for chunk in stream])
        return object()


async def test_artifact_writer_receives_only_bytes_and_display_metadata() -> None:
    writer = _RecordingWriter()

    async def stream() -> AsyncIterator[bytes]:
        yield b"a"
        yield b"b"

    await cast(ArtifactWriter, writer).create(
        stream(), "name.txt", "text/plain", TrustLevel.EXTERNAL_UNTRUSTED
    )
    assert writer.seen == b"ab"
