"""Authored fake-model fixture resolution and compatibility translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import yaml
from pydantic import ValidationError

from agent_core.domain.messages import (
    FakeModelScript,
    ModelError,
    ModelPermanentError,
    ModelTransientError,
    ModelUsage,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _legacy_turn(raw: object, usage: ModelUsage | None) -> ScriptedTurn:
    row = _mapping(raw, "model fixture turn")
    kind = row.get("kind")
    if kind == "tool_call":
        return ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name=str(row["tool_name"]),
                    arguments=row.get("arguments", {}),
                    call_id=str(row["call_id"]),
                )
            ],
            stop_reason=StopReason.TOOL_USE,
            usage=usage,
        )
    if kind == "final":
        return ScriptedTurn(
            text=str(row.get("text", "")),
            stop_reason=StopReason(str(row.get("stop_reason", StopReason.END_TURN.value))),
            usage=usage,
        )
    if kind == "error":
        error_class = str(row.get("error_class", "permanent"))
        message = str(row.get("message", f"scripted {error_class} model failure"))
        error: ModelError
        if error_class == "transient":
            error = ModelTransientError(
                provider="fake",
                model="scripted",
                attempt_id=UUID(int=0),
                message=message,
                stream_had_output=int(row.get("after_bytes", 0)) > 0,
            )
        elif error_class == "permanent":
            error = ModelPermanentError(
                provider="fake",
                model="scripted",
                attempt_id=UUID(int=0),
                message=message,
            )
        else:
            raise ValueError(f"unsupported model fixture error_class {error_class!r}")
        return ScriptedTurn(fail_with=error, usage=usage)
    raise ValueError(f"unsupported legacy model fixture turn kind {kind!r}")


def load_model_fixture(path: Path) -> FakeModelScript:
    """Parse the canonical shape, or the plan's authored shorthand shape."""

    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = _mapping(loaded, str(path))
    turns = root.get("turns")
    if not isinstance(turns, list):
        raise ValueError(f"{path} must declare a turns list")
    if not any(isinstance(turn, dict) and "kind" in turn for turn in turns):
        try:
            return FakeModelScript.model_validate(loaded)
        except ValidationError as exc:
            raise ValueError(f"{path} is not a valid FakeModelScript: {exc}") from exc
    else:
        usage_raw = root.get("usage")
        usage = ModelUsage.model_validate(usage_raw) if usage_raw is not None else None
        on_exhausted = cast(Literal["error", "repeat_last"], str(root.get("on_exhausted", "error")))
        return FakeModelScript(
            turns=[_legacy_turn(turn, usage) for turn in turns],
            on_exhausted=on_exhausted,
        )


def resolve_model_fixture(fixture_root: Path, name: str) -> FakeModelScript:
    path = fixture_root / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"model fixture {name!r} does not resolve to {path}")
    return load_model_fixture(path)
