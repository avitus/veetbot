"""Store the file a Gmail read returns and hand back a receipt (ADR-0294)."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

from agent_core.domain.artifacts import (
    ATTACHMENT_METADATA_KEY,
    AUTO_INGEST_KEY,
    AUTO_INGEST_PENDING,
)
from agent_core.domain.attachments import stored_email_attachment_name
from agent_core.domain.mcp import MCPCallResult
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolExecutionContext
from agent_core.model.attachments import KNOWLEDGE_MEDIA_TYPES, PDF_MEDIA_TYPE
from agent_core.ports.artifacts import ArtifactWriter, AttachmentInspector

EMAIL_SOURCE_METADATA_KEY = "email_source"
_FALLBACK_MEDIA_TYPE = "application/octet-stream"
_CHUNK_BYTES = 64 * 1024


async def _chunks(content: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(content), _CHUNK_BYTES):
        yield content[start : start + _CHUNK_BYTES]


def _invalid() -> MCPCallResult:
    return MCPCallResult(
        content=("gmail.provider_output_invalid",),
        is_error=True,
        structured={"effect_status": "not_applied"},
    )


class EmailAttachmentSaver:
    """Keep a file's bytes out of the tool result, and so out of every event."""

    def __init__(self, inspector: AttachmentInspector) -> None:
        self._inspector = inspector

    async def __call__(
        self,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
        call: Callable[[dict[str, Any]], Awaitable[MCPCallResult]],
    ) -> MCPCallResult:
        result = await call(arguments)
        if result.is_error:
            return result
        value = result.structured or {}
        filename, encoded = value.get("filename"), value.get("data_base64")
        if not isinstance(filename, str) or not isinstance(encoded, str):
            return _invalid()
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return _invalid()
        if not content:
            return _invalid()
        name = stored_email_attachment_name(filename)
        declared = value.get("mime_type")
        facts = await self._inspector.inspect(
            content,
            filename=name,
            declared_media_type=declared
            if isinstance(declared, str) and declared
            # A blank type is no claim at all.
            else _FALLBACK_MEDIA_TYPE,
        )
        source = {
            "thread_id": str(value.get("thread_id", "")),
            "message_id": str(value.get("message_id", "")),
            "attachment_index": arguments.get("attachment_index"),
        }
        metadata: dict[str, Any] = {
            ATTACHMENT_METADATA_KEY: facts.metadata(),
            EMAIL_SOURCE_METADATA_KEY: source,
        }
        # The press or the approval is the human admission knowledge requires.
        offered = (
            "knowledge.write" in context.principal.scopes
            and facts.media_type in KNOWLEDGE_MEDIA_TYPES
            and (facts.media_type != PDF_MEDIA_TYPE or facts.page_count is not None)
        )
        if offered:
            metadata[AUTO_INGEST_KEY] = AUTO_INGEST_PENDING
        writer = cast(ArtifactWriter, context.artifacts)
        stored = await writer.create(
            _chunks(content),
            name,
            facts.media_type,
            TrustLevel.EXTERNAL_UNTRUSTED,
            metadata=metadata,
        )
        receipt = {
            "thread_id": source["thread_id"],
            "message_id": source["message_id"],
            "filename": name,
            "artifact_id": str(stored.artifact_id),
            "media_type": stored.media_type,
            "size_bytes": stored.size_bytes,
            "saved_to": "This chat. The final reply carries the file.",
            "searchable": (
                "Shortly, through knowledge search."
                if offered
                else "No. Only the owner can open this file."
            ),
        }
        return MCPCallResult(
            content=(json.dumps(receipt, ensure_ascii=False, sort_keys=True),),
            structured=receipt,
        )
