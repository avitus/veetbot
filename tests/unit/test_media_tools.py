"""Media validation and cancellation before and during generation."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.agents import Principal
from agent_core.domain.errors import RunCancelledError, ToolValidationError
from agent_core.domain.media import ImageGenerationRequest, MediaImage, VideoGenerationRequest
from agent_core.domain.runs import CancelReason
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.media_generation import (
    ImageGenerationTool,
    LegacyImageGenerationTool,
    LegacyVideoGenerationTool,
    VideoGenerationTool,
)
from agent_core.tools.validation import validate_and_normalize
from tests.contract.support import NOW, tool_context
from tests.unit.test_artifact_export_content import _RecordingWriter


@pytest.mark.parametrize("tool_type", [ImageGenerationTool, VideoGenerationTool])
def test_reference_images_are_admitted_by_the_tool_schema(
    tool_type: type[ImageGenerationTool],
) -> None:
    arguments = {"prompt": "Animate or edit this image", "reference_image_ids": [str(UUID(int=1))]}
    normalized, _, _ = validate_and_normalize(arguments, tool_type.spec.input_schema)
    assert normalized["reference_image_ids"] == arguments["reference_image_ids"]
    assert tool_type.spec.version == "1.1.0"


@pytest.mark.parametrize("tool_type", [ImageGenerationTool, VideoGenerationTool])
@pytest.mark.parametrize(
    "references",
    [
        ["https://example.com/x.png"],
        ["data:image/png;base64,AAAA"],
        ["/tmp/x.png"],
        ["bad-id"],
        [None],
        "bad-list",
    ],
)
def test_reference_schema_rejects_inline_bytes_urls_paths_and_bad_ids(
    tool_type: type[ImageGenerationTool], references: object
) -> None:
    with pytest.raises(ToolValidationError):
        validate_and_normalize(
            {"prompt": "x", "reference_image_ids": references}, tool_type.spec.input_schema
        )


@pytest.mark.parametrize("tool_type,maximum", [(ImageGenerationTool, 8), (VideoGenerationTool, 2)])
def test_reference_count_is_bounded(tool_type: type[ImageGenerationTool], maximum: int) -> None:
    with pytest.raises(ToolValidationError):
        validate_and_normalize(
            {"prompt": "x", "reference_image_ids": [str(UUID(int=1))] * (maximum + 1)},
            tool_type.spec.input_schema,
        )


@pytest.mark.parametrize("tool_type", [LegacyImageGenerationTool, LegacyVideoGenerationTool])
def test_pinned_text_only_versions_keep_their_original_schema(
    tool_type: type[ImageGenerationTool],
) -> None:
    assert tool_type.spec.version == "1.0.0"
    validate_and_normalize({"prompt": "x"}, tool_type.spec.input_schema)
    with pytest.raises(ToolValidationError):
        validate_and_normalize(
            {"prompt": "x", "reference_image_ids": []}, tool_type.spec.input_schema
        )


class WaitingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def generate(
        self,
        request: ImageGenerationRequest | VideoGenerationRequest,
        *,
        timeout_seconds: float,
        images: tuple[MediaImage, ...] = (),
    ) -> AsyncIterator[bytes]:
        self.started.set()
        try:
            await asyncio.Event().wait()
            yield b"unreachable"
        finally:
            self.closed = True

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("tool_type", [ImageGenerationTool, VideoGenerationTool])
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"prompt": " "},
        {"prompt": "x" * 8001},
        {"prompt": "x", "seed": -1},
        {"prompt": "x", "seed": True},
        {"prompt": "x", "size": "1x1"},
        {"prompt": "x", "url": "https://example.org"},
        {"prompt": "x", "path": "/etc/passwd"},
    ],
)
def test_bad_arguments_never_enter_generation(
    tool_type: type[ImageGenerationTool], arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolValidationError):
        validate_and_normalize(arguments, tool_type.spec.input_schema)


@pytest.mark.parametrize("before_start", [False, True])
async def test_cooperative_cancellation_closes_stream_without_publishing(
    before_start: bool,
) -> None:
    provider = WaitingProvider()
    writer = _RecordingWriter()
    token = RunCancellationToken(FixedClock(NOW), None)
    context = replace(tool_context(), artifacts=writer, cancellation=token)
    if before_start:
        token.cancel(CancelReason.REQUESTED)
    task = asyncio.create_task(
        ImageGenerationTool(provider).execute({"prompt": "Lighthouse"}, context)
    )
    try:
        if not before_start:
            await asyncio.wait_for(provider.started.wait(), 1)
            token.cancel(CancelReason.REQUESTED)
        with pytest.raises(RunCancelledError):
            await asyncio.wait_for(task, 1)
        assert writer.calls == []
        assert provider.started.is_set() is not before_start
        assert provider.closed is not before_start
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_during_reference_read_never_starts_generation() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class WaitingImages:
        async def resolve(
            self, artifact_ids: Sequence[UUID], *, run_id: UUID, principal: Principal, video: bool
        ) -> tuple[MediaImage, ...]:
            started.set()
            try:
                await asyncio.Event().wait()
                return ()
            finally:
                closed.set()

    provider = WaitingProvider()
    writer = _RecordingWriter()
    token = RunCancellationToken(FixedClock(NOW), None)
    context = replace(tool_context(), artifacts=writer, cancellation=token)
    context.principal.scopes.add("artifact.read")
    task = asyncio.create_task(
        ImageGenerationTool(provider, image_resolver=WaitingImages()).execute(
            {"prompt": "Edit this image", "reference_image_ids": [str(UUID(int=1))]}, context
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        token.cancel(CancelReason.REQUESTED)
        with pytest.raises(RunCancelledError):
            await asyncio.wait_for(task, 1)
        assert closed.is_set()
        assert not provider.started.is_set()
        assert writer.calls == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
