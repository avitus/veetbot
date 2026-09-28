"""Streaming generation contract; no provider SDK types or credentials escape."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.media import ImageGenerationRequest, MediaImage, VideoGenerationRequest


class MediaInputResolver(Protocol):
    async def resolve(
        self, artifact_ids: Sequence[UUID], *, run_id: UUID, principal: Principal, video: bool
    ) -> tuple[MediaImage, ...]:
        """Read only live, caller-owned, claimed or exported images in this run's chat.

        Enforce artifact.read, count/byte bounds, checksums and PNG/JPEG/WebP
        signatures. Preserve order. Raise MediaInputError before provider access.
        """
        ...


class MediaGenerationProvider(Protocol):
    def generate(
        self,
        request: ImageGenerationRequest | VideoGenerationRequest,
        *,
        timeout_seconds: float,
        images: tuple[MediaImage, ...] = (),
    ) -> AsyncIterator[bytes]:
        """Yield one validated PNG or MP4, bounded by the request's media ceiling.

        Never retry a paid request. Raise MediaGenerationError with a safe code
        on failure; cancellation propagates and closes the response stream.
        """
        ...

    async def close(self) -> None: ...
