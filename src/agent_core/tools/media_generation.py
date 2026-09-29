"""Generate one durable image or video through the approved media provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError

from agent_core.domain.errors import ArtifactIntegrityError
from agent_core.domain.media import (
    ImageGenerationArguments,
    ImageGenerationRequest,
    MediaGenerationError,
    MediaImage,
    MediaInputError,
    VideoGenerationArguments,
    VideoGenerationRequest,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.artifacts import ArtifactWriter
from agent_core.ports.dispatch import CancellationToken
from agent_core.ports.media import MediaGenerationProvider, MediaInputResolver

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string"},
        "sha256": {"type": "string"},
        "size_bytes": {"type": "integer"},
        "media_type": {"type": "string"},
    },
    "required": ["artifact_id", "sha256", "size_bytes", "media_type"],
    "additionalProperties": False,
}


def _spec(*, video: bool, references: bool = True) -> ToolSpec:
    request_type = (
        (VideoGenerationArguments if video else ImageGenerationArguments)
        if references
        else (VideoGenerationRequest if video else ImageGenerationRequest)
    )
    kind = "video" if video else "image"
    return ToolSpec(
        name=f"{kind}.generate",
        version="1.1.0" if references else "1.0.0",
        description=(
            f"Generate one {kind} from a text prompt and attach the file to your reply. "
            "Requires approval and incurs provider charges. "
            "Do not repeat a failed or timed-out generation without asking the user: "
            "the provider may already have charged. "
            + (
                "Optionally supply reference_image_ids from this chat's attachment labels "
                "or generated files. "
                + (
                    "Order them first frame, then optional last frame."
                    if video
                    else "Use up to eight ordered images for editing or visual references."
                )
                if references
                else "No reference images or editing."
            )
        ),
        input_schema=request_type.model_json_schema(),
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"media.generate", "artifact.write"},
        timeout_seconds=7200 if video else 1800,
        maximum_output_bytes=4096,
        allow_parallel=False,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )


class ImageGenerationTool:
    spec = _spec(video=False)
    request_type: type[ImageGenerationRequest] | type[VideoGenerationRequest] = (
        ImageGenerationArguments
    )
    media_type = "image/png"
    extension = "png"

    def __init__(
        self, provider: MediaGenerationProvider, *, image_resolver: MediaInputResolver | None = None
    ) -> None:
        self._provider = provider
        self._image_resolver = image_resolver

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            request = self.request_type.model_validate(arguments)
        except ValidationError:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "Invalid generation arguments.",
            )
        token = cast(CancellationToken, context.cancellation)
        token.raise_if_cancelled()
        generation = asyncio.create_task(self._generate(request, context))
        cancelled = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
                {generation, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancelled in done:
                await cancelled
                token.raise_if_cancelled()
            return await generation
        finally:
            generation.cancel()
            cancelled.cancel()
            await asyncio.gather(generation, cancelled, return_exceptions=True)

    async def _generate(
        self,
        request: ImageGenerationRequest | VideoGenerationRequest,
        context: ToolExecutionContext,
    ) -> ToolResult:
        writer = cast(ArtifactWriter, context.artifacts)
        try:
            images: tuple[MediaImage, ...] = ()
            if isinstance(request, (ImageGenerationArguments, VideoGenerationArguments)):
                if request.reference_image_ids:
                    if "artifact.read" not in context.principal.scopes:
                        return _failure(
                            ToolFailureKind.PERMISSION,
                            "policy.scope.missing",
                            "Reference images require artifact.read.",
                        )
                    if self._image_resolver is None:
                        raise MediaInputError("unavailable")
                    images = await self._image_resolver.resolve(
                        [UUID(value) for value in request.reference_image_ids],
                        run_id=context.run_id,
                        principal=context.principal,
                        video=isinstance(request, VideoGenerationRequest),
                    )
                core_type = (
                    VideoGenerationRequest
                    if isinstance(request, VideoGenerationRequest)
                    else ImageGenerationRequest
                )
                request = core_type.model_validate(
                    request.model_dump(exclude={"reference_image_ids"})
                )
            stream = self._provider.generate(
                request, timeout_seconds=context.timeout_seconds, images=images
            )
            # A stream is closed even if artifact persistence or cancellation fails.
            # The async iterator contract permits implementations without aclose.
            async with aclosing(_forward(stream)) as content:
                ref = await writer.create(
                    content,
                    f"generated-{context.invocation_id}.{self.extension}",
                    self.media_type,
                    TrustLevel.EXTERNAL_UNTRUSTED,
                )
        except MediaInputError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                f"tool.media.reference_{exc.code}",
                "Reference images could not be used. No generation request was sent.",
            )
        except MediaGenerationError as exc:
            return _media_failure(exc)
        except ArtifactIntegrityError:
            return _failure(
                ToolFailureKind.OUTPUT_TOO_LARGE,
                "tool.media.too_large",
                "Generated media exceeded the artifact limit.",
            )
        structured = {
            "artifact_id": str(ref.artifact_id),
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
            "media_type": ref.media_type,
        }
        return ToolResult(
            ok=True,
            content=[
                TextPart(text=json.dumps({**structured, "attached_to_reply": True}, sort_keys=True))
            ],
            structured=structured,
            artifacts=[structured],
        )


class VideoGenerationTool(ImageGenerationTool):
    spec = _spec(video=True)
    request_type: type[ImageGenerationRequest] | type[VideoGenerationRequest] = (
        VideoGenerationArguments
    )
    media_type = "video/mp4"
    extension = "mp4"


class LegacyImageGenerationTool(ImageGenerationTool):
    spec = _spec(video=False, references=False)
    request_type = ImageGenerationRequest


class LegacyVideoGenerationTool(VideoGenerationTool):
    spec = _spec(video=True, references=False)
    request_type = VideoGenerationRequest


def _failure(kind: ToolFailureKind, reason: str, detail: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(kind=kind, reason_code=reason, detail=detail, retryable=False),
    )


def _media_failure(error: MediaGenerationError) -> ToolResult:
    kinds = {
        "permission": ToolFailureKind.PERMISSION,
        "rejected": ToolFailureKind.INVALID_ARGUMENTS,
        "timeout": ToolFailureKind.TIMEOUT,
        "transport": ToolFailureKind.TRANSPORT,
        "upstream": ToolFailureKind.UPSTREAM_ERROR,
        "invalid": ToolFailureKind.OUTPUT_INVALID,
        "too_large": ToolFailureKind.OUTPUT_TOO_LARGE,
    }
    return _failure(
        kinds[error.code],
        f"tool.media.{error.code}",
        "Media generation failed; a charge may have occurred. Do not automatically retry.",
    )


# Keep the port at AsyncIterator while ensuring generator implementations close
# before the tool returns, including on a partial artifact write.
async def _forward(stream: AsyncIterator[bytes]) -> AsyncGenerator[bytes]:
    try:
        async for chunk in stream:
            yield chunk
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
