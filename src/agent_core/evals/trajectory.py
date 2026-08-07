"""Lossy conversion from a redacted export artifact to reviewable eval inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
)
from agent_core.domain.runs import FailureReason, RunStatus
from agent_core.evals.cases import EvalCase


@dataclass(frozen=True, slots=True)
class ConvertedTrajectory:
    case: EvalCase
    model_script: FakeModelScript
    tool_results: dict[str, dict[str, Any]]


def _text(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        TextPart.model_validate(part).text
        for part in parts
        if isinstance(part, dict) and part.get("kind") == "text"
    )


def convert_trajectory(
    content: bytes,
    *,
    case_name: str,
    model_fixture: str,
) -> ConvertedTrajectory:
    """Convert only already-redacted artifact bytes; this module has no log port."""

    raw: object = json.loads(content)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("trajectory artifact must use schema version 1")
    messages = raw.get("messages")
    if not isinstance(messages, list):
        raise ValueError("trajectory artifact has no normalized messages")
    user_inputs: list[str] = []
    turns: list[ScriptedTurn] = []
    tool_results: dict[str, dict[str, Any]] = {}
    pending_calls: list[ScriptedToolCall] = []
    pending_text: str | None = None

    def flush_pending() -> None:
        nonlocal pending_calls, pending_text
        if pending_calls:
            turns.append(
                ScriptedTurn(
                    text=pending_text or "",
                    tool_calls=pending_calls,
                    stop_reason=StopReason.TOOL_USE,
                )
            )
        elif pending_text:
            turns.append(ScriptedTurn(text=pending_text, stop_reason=StopReason.END_TURN))
        pending_calls = []
        pending_text = None

    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("trajectory message is not a mapping")
        kind = message.get("kind")
        if kind == "user":
            flush_pending()
            value = _text(message.get("content"))
            if value:
                user_inputs.append(value)
        elif kind == "assistant":
            if pending_calls:
                flush_pending()
            value = _text(message.get("content"))
            if value:
                pending_text = value if pending_text is None else f"{pending_text}\n{value}"
        elif kind == "tool_call":
            arguments = message.get("arguments")
            pending_calls.append(
                ScriptedToolCall(
                    name=str(message.get("name", "")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                    call_id=str(message.get("call_id", "")),
                )
            )
        elif kind == "tool_result":
            flush_pending()
            call_id = str(message.get("call_id", ""))
            tool_results[call_id] = {
                "content": _text(message.get("content")),
                "is_error": bool(message.get("is_error", False)),
            }
        elif kind == "provider_reasoning":
            raise ValueError("redacted trajectory unexpectedly contains provider reasoning")
    flush_pending()
    if not user_inputs or not turns:
        raise ValueError("trajectory cannot produce a deterministic input and model script")
    raw_outcome = raw.get("outcome")
    if not isinstance(raw_outcome, str) or not raw_outcome:
        raise ValueError("trajectory artifact has no outcome")
    try:
        outcome = RunStatus(raw_outcome)
    except ValueError as exc:
        raise ValueError("trajectory artifact has an invalid outcome") from exc
    export_id = raw.get("export_id")
    if not isinstance(export_id, str) or not export_id:
        raise ValueError("trajectory artifact has no export id")
    failure = raw.get("failure")
    failure_reason = None
    if isinstance(failure, dict) and failure.get("kind") is not None:
        failure_reason = FailureReason(str(failure["kind"]))
    final_text = next((turn.text for turn in reversed(turns) if turn.text), None)
    case = EvalCase.model_validate(
        {
            "name": case_name,
            "milestone": 3,
            "source": "trajectory",
            "source_export_id": export_id,
            "tags": ["trajectory", "regression"],
            "input": {"text": user_inputs[0]},
            "model_fixture": model_fixture,
            "expected": {
                "terminal_status": outcome.value,
                "final_text": final_text if outcome is RunStatus.COMPLETED else None,
                "failure_reason": failure_reason,
                "model_calls": len(turns),
            },
        }
    )
    return ConvertedTrajectory(
        case=case,
        model_script=FakeModelScript(turns=turns),
        tool_results=tool_results,
    )
