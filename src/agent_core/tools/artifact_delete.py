"""Delete files Chat generated in this chat at the owner's request (ADR-0277)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agent_core.domain.artifacts import ARTIFACT_DELETE_TOOL_NAME, REPLY_ATTACHMENT_ORIGINS
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory

MAX_ARTIFACT_IDS = 50
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ID_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_ids": {
            "type": "array",
            "items": {"type": "string", "pattern": f"^(artifact:)?{_UUID}$"},
            "minItems": 1,
            "maxItems": MAX_ARTIFACT_IDS,
            "uniqueItems": True,
        },
    },
    "required": ["artifact_ids"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deleted": _ID_LIST,
        "already_deleted": _ID_LIST,
        "not_deletable": _ID_LIST,
    },
    "required": ["deleted", "already_deleted", "not_deletable"],
    "additionalProperties": False,
}


class ArtifactDeleteTool:
    spec = ToolSpec(
        name=ARTIFACT_DELETE_TOOL_NAME,
        version="1.0.0",
        description=(
            "Delete files you generated in this chat when the user asks; they disappear from "
            "the chat and cannot be recovered. Pass the artifact ids shown on those files. "
            "The user's own uploads and other chats' files are never deleted."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes={"artifact.write"},
        timeout_seconds=10,
        maximum_output_bytes=8192,
        allow_parallel=False,
        target_kind="in_process",
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Expire each eligible file now; the artifact sweeper removes bytes and rows."""

        if context.origin_trust is TrustLevel.EXTERNAL_UNTRUSTED:
            return _failure(ToolFailureKind.PERMISSION, "artifact.untrusted_turn")
        requested = [
            UUID(str(value).removeprefix("artifact:")) for value in arguments["artifact_ids"]
        ]
        now = self._clock.now()
        deleted: list[ArtifactRef] = []
        already: list[ArtifactRef] = []
        refused: list[UUID] = []
        async with self._uow_factory() as uow:
            for artifact_id in requested:
                try:
                    artifact = await uow.artifacts.get(artifact_id, context.principal)
                except NotFoundError:
                    refused.append(artifact_id)
                    continue
                if (
                    artifact.session_id != context.session_id
                    or artifact.origin not in REPLY_ATTACHMENT_ORIGINS
                ):
                    refused.append(artifact_id)
                elif artifact.expires_at is not None and artifact.expires_at <= now:
                    already.append(artifact)
                else:
                    deleted.append(await uow.artifacts.expire(artifact_id, context.principal, now))
        if not deleted and not already:
            return _failure(ToolFailureKind.NOT_FOUND, "artifact.not_deletable")
        return ToolResult(
            ok=True,
            content=[TextPart(text=_summary(deleted, already, refused))],
            structured={
                "deleted": [str(artifact.id) for artifact in deleted],
                "already_deleted": [str(artifact.id) for artifact in already],
                "not_deletable": [str(artifact_id) for artifact_id in refused],
            },
        )


def _names(artifacts: list[ArtifactRef]) -> str:
    return ", ".join(f'"{artifact.name}" (artifact:{artifact.id})' for artifact in artifacts)


def _summary(deleted: list[ArtifactRef], already: list[ArtifactRef], refused: list[UUID]) -> str:
    lines = []
    if deleted:
        lines.append(f"Deleted {len(deleted)} file(s): {_names(deleted)}.")
    if already:
        lines.append(f"Already deleted: {_names(already)}.")
    if refused:
        ids = ", ".join(f"artifact:{artifact_id}" for artifact_id in refused)
        lines.append(f"Not deleted, because they are not files you generated in this chat: {ids}.")
    return "\n".join(lines)


def _failure(kind: ToolFailureKind, reason_code: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=kind, reason_code=reason_code, detail=reason_code, retryable=False
        ),
    )
