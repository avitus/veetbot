"""Inspect a bounded rendition of one public photo."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

import httpx
from PIL import Image

from agent_core.domain.messages import ImageReferencePart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult
from agent_core.ports.artifacts import ArtifactWriter
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_search import _failure


class WebInspectImageTool:
    spec = WebFetchTool.spec.model_copy(
        update={
            "name": "web.inspect_image",
            "description": (
                "Inspect a public photo from an image URL or selected SmugMug photo permalink. "
                "Ask which photo for a gallery without a selection."
            ),
            "output_schema": {
                "type": "object",
                "properties": {"artifact_id": {"type": "string"}},
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
        }
    )

    def __init__(self, read: Callable[[str], Awaitable[bytes]]) -> None:
        self.read = read

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if "artifact.read" not in context.principal.scopes:
            return _failure(ToolFailureKind.PERMISSION, "policy.scope.missing", retryable=False)
        try:
            data = await self.read(arguments["url"])
        except (
            ValueError,
            OSError,
            Image.DecompressionBombError,
            httpx.InvalidURL,
            httpx.HTTPError,
        ):
            return _failure(
                ToolFailureKind.UPSTREAM_ERROR, "tool.web.image_unavailable", retryable=False
            )

        async def stream() -> AsyncIterator[bytes]:
            yield data

        ref = await cast(ArtifactWriter, context.artifacts).create(
            stream(),
            f"inspection-{context.invocation_id}.jpg",
            "image/jpeg",
            TrustLevel.EXTERNAL_UNTRUSTED,
        )
        return ToolResult(
            ok=True,
            content=[
                ImageReferencePart(
                    artifact_id=ref.artifact_id,
                    media_type=ref.media_type,
                    size_bytes=ref.size_bytes,
                    filename="Inspected photo.jpg",
                )
            ],
            structured={"artifact_id": str(ref.artifact_id)},
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
