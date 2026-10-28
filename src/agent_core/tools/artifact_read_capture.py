"""Read a chat's own untrusted tool-output capture in sections (ADR-0261)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agent_core.domain.artifacts import StoredArtifactRef
from agent_core.domain.captures import (
    CAPTURE_METADATA_KEY,
    CAPTURE_READER_TOOL_NAME,
    CAPTURE_SECTION_BYTES,
    CaptureSections,
    capture_text,
)
from agent_core.domain.errors import ArtifactIntegrityError, NotFoundError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolInvocationStatus,
    ToolResult,
    ToolSpec,
)
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.artifacts import ArtifactStore
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory

_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact": {"type": "string", "pattern": f"^(artifact:)?{_UUID}$"},
        "section": {"type": "integer", "minimum": 1, "maximum": 65_536, "default": 1},
        "find": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "required": ["artifact"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact": {"type": "string"},
        "tool": {"type": "string"},
        "sections": {"type": "integer", "minimum": 1},
        "section": {"type": "integer", "minimum": 1},
        "text": {"type": "string"},
        "find": {"type": "string"},
        "total": {"type": "integer", "minimum": 0},
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "integer", "minimum": 1},
                    "snippet": {"type": "string"},
                },
                "required": ["section", "snippet"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["artifact", "tool", "sections"],
    "additionalProperties": False,
}


class ArtifactReadCaptureTool:
    spec = ToolSpec(
        name=CAPTURE_READER_TOOL_NAME,
        version="1.0.0",
        description=(
            "Read one section of a captured tool result, or search it with find. Pass the "
            "artifact id a capture marker or truncation pointer shows."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(),
        timeout_seconds=10,
        maximum_output_bytes=CAPTURE_SECTION_BYTES,
        allow_parallel=False,
        target_kind="in_process",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        store: ArtifactStore,
        clock: Clock,
        *,
        step_maximum_reads: int = 2,
        run_maximum_reads: int = 16,
    ) -> None:
        self._uow_factory = uow_factory
        self._store = store
        self._clock = clock
        self._step_maximum_reads = step_maximum_reads
        self._run_maximum_reads = run_maximum_reads

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Return one section or a search, after the step and run limits allow it."""

        section = int(arguments.get("section", 1))
        needle = arguments.get("find")
        artifact_id = UUID(str(arguments["artifact"]).removeprefix("artifact:"))
        async with self._uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(context.run_id, context.principal)
            try:
                artifact: ArtifactRef | None = await uow.artifacts.get(
                    artifact_id, context.principal
                )
            except NotFoundError:
                artifact = None
        # Only completed reads count, so a refused or failed read costs nothing
        # and a resumed run counts the reads its invocations already recorded.
        reads = [
            invocation
            for invocation in invocations
            if invocation.tool_name == CAPTURE_READER_TOOL_NAME
            and invocation.status is ToolInvocationStatus.SUCCEEDED
            and invocation.id != context.invocation_id
        ]
        if sum(read.step_number == context.step_number for read in reads) >= (
            self._step_maximum_reads
        ):
            return _failure(ToolFailureKind.PERMISSION, "tool.capture.step_limit")
        if len(reads) >= self._run_maximum_reads:
            return _failure(ToolFailureKind.PERMISSION, "tool.capture.run_limit")
        tool = None if artifact is None else self._producing_tool(artifact, context)
        if artifact is None or tool is None:
            return _failure(ToolFailureKind.NOT_FOUND, "tool.capture.not_found")
        stored = StoredArtifactRef(
            artifact_id=artifact.id,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
        )
        try:
            chunks = await self._store.open_verified(stored, tenant_id=context.tenant_id)
            raw = b"".join([chunk async for chunk in chunks])
        except (OSError, ArtifactIntegrityError):
            return _failure(ToolFailureKind.NOT_FOUND, "tool.capture.not_found")
        sections = CaptureSections(capture_text(raw), artifact_id=artifact.id, tool=tool)
        if section > sections.count:
            return _failure(ToolFailureKind.INVALID_ARGUMENTS, "tool.capture.section_out_of_range")
        structured: dict[str, Any] = {
            "artifact": str(artifact.id),
            "tool": tool,
            "sections": sections.count,
        }
        if isinstance(needle, str):
            found = sections.find(needle, from_section=section)
            text = sections.find_text(needle, from_section=section)
            shown = text.count("\n§")
            structured |= {
                "find": needle,
                "total": found.total,
                "matches": [
                    {"section": match.section, "snippet": match.snippet}
                    for match in found.matches[:shown]
                ],
            }
        else:
            text = sections.section_text(section)
            structured |= {"section": section, "text": sections.body(section)}
        return ToolResult(
            ok=True,
            content=[TextPart(text=text)],
            structured=structured,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )

    def _producing_tool(self, artifact: ArtifactRef, context: ToolExecutionContext) -> str | None:
        """The capture's producing tool when this run may open it, otherwise None."""

        mark = artifact.metadata.get(CAPTURE_METADATA_KEY)
        if not isinstance(mark, dict):
            return None
        tool = mark.get("tool")
        scopes = mark.get("required_scopes")
        if (
            artifact.origin != "tool_output"
            or artifact.tenant_id != context.tenant_id
            or artifact.session_id != context.session_id
            or artifact.trust is not TrustLevel.EXTERNAL_UNTRUSTED
            or (artifact.expires_at is not None and artifact.expires_at <= self._clock.now())
            or not isinstance(tool, str)
            or not isinstance(scopes, list)
            or not all(isinstance(scope, str) for scope in scopes)
            or not set(scopes) <= context.principal.scopes
        ):
            return None
        return tool


def _failure(kind: ToolFailureKind, reason_code: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=kind, reason_code=reason_code, detail=reason_code, retryable=False
        ),
    )
