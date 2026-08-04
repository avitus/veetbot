"""Artifact writer surface contract."""

from collections.abc import AsyncIterator
from uuid import UUID

from agent_core.domain.artifacts import StoredArtifactRef
from agent_core.domain.policies import TrustLevel
from agent_core.ports.artifacts import ArtifactWriter


class _RecordingWriter:
    def __init__(self) -> None:
        self.seen = b""
        self.filename = ""
        self.media_type = ""
        self.trust: TrustLevel | None = None

    async def create(
        self,
        stream: AsyncIterator[bytes],
        filename: str,
        media_type: str,
        trust: TrustLevel,
    ) -> StoredArtifactRef:
        self.filename = filename
        self.media_type = media_type
        self.trust = trust
        self.seen = b"".join([chunk async for chunk in stream])
        return StoredArtifactRef(UUID(int=1), "0" * 64, len(self.seen), media_type)


async def test_artifact_writer_receives_only_bytes_and_display_metadata() -> None:
    writer = _RecordingWriter()

    async def stream() -> AsyncIterator[bytes]:
        yield b"a"
        yield b"b"

    artifact_writer: ArtifactWriter = writer
    ref = await artifact_writer.create(
        stream(), "name.txt", "text/plain", TrustLevel.EXTERNAL_UNTRUSTED
    )
    assert writer.seen == b"ab"
    assert writer.filename == "name.txt"
    assert writer.media_type == "text/plain"
    assert writer.trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert ref.size_bytes == 2
