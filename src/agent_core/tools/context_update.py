"""Typed control tool for deterministic structured working-state transitions."""

from __future__ import annotations

from typing import Any

from agent_core.context.working_state import WorkingStateLimitError, WorkingStateManager
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolKind,
    ToolResult,
    ToolSpec,
)


class UpdateWorkingStateTool:
    spec = ToolSpec(
        name="context.update_working_state",
        version="1.0.0",
        description=(
            "Update the run's typed objective, constraints, tasks, facts, questions, and next "
            "action. Constraints are append-only and facts retain event provenance."
        ),
        input_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "objective": {"type": ["string", "null"], "maxLength": 4096},
                "add_constraints": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "maxItems": 20,
                },
                "upsert_tasks": {
                    "type": "array",
                    "maxItems": 30,
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                            "description": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 4096,
                            },
                            "status": {
                                "type": "string",
                                "enum": ["open", "in_progress", "blocked", "completed"],
                            },
                            "source_event_ids": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 1},
                            },
                        },
                        "required": ["task_id", "description"],
                        "additionalProperties": False,
                    },
                },
                "add_facts": {
                    "type": "array",
                    "maxItems": 40,
                    "items": {
                        "type": "object",
                        "properties": {
                            "statement": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 4096,
                            },
                            "source_event_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "integer", "minimum": 1},
                            },
                        },
                        "required": ["statement", "source_event_ids"],
                        "additionalProperties": False,
                    },
                },
                "resolve_questions": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "maxItems": 20,
                },
                "next_action": {"type": ["string", "null"], "maxLength": 4096},
            },
            "additionalProperties": False,
        },
        output_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "updated": {"type": "boolean"},
                "working_state": {"type": "object"},
            },
            "required": ["updated", "working_state"],
            "additionalProperties": False,
        },
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes=set(),
        timeout_seconds=1,
        maximum_output_bytes=64 * 1024,
        allow_parallel=False,
        kind=ToolKind.CONTROL,
        output_trust=TrustLevel.PLATFORM,
    )

    def __init__(self, manager: WorkingStateManager) -> None:
        self._manager = manager

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        current = self._manager.load(context.working_state)
        try:
            updated = self._manager.transition(current, arguments)
        except WorkingStateLimitError as exc:
            return ToolResult(
                ok=False,
                content=[TextPart(text=str(exc))],
                failure=ToolFailure(
                    kind=ToolFailureKind.INVALID_ARGUMENTS,
                    reason_code="tool.arguments_invalid",
                    detail=str(exc),
                    retryable=False,
                ),
            )
        changed = updated != current
        return ToolResult(
            ok=True,
            content=[
                TextPart(
                    text=(
                        "Structured working state updated."
                        if changed
                        else "Structured working state already matched."
                    )
                )
            ],
            structured={"updated": changed, "working_state": updated.model_dump(mode="json")},
            output_trust=TrustLevel.PLATFORM,
        )
