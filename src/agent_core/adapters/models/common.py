"""SDK-free request and raw-event helpers shared by provider adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol, cast

from agent_core.domain.messages import (
    AssistantMessage,
    ContentPart,
    FileReferencePart,
    ImageReferencePart,
    ModelAttempt,
    ModelFailedEvent,
    ModelPermanentError,
    ModelProtocolError,
    ModelTransientError,
    TextPart,
    sanitize_provider_code,
    sanitize_provider_parameter,
)
from agent_core.domain.tools import ToolSpec

type RawEventSource = Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]]


class Dumpable(Protocol):
    def model_dump(self, *, mode: str = "python") -> dict[str, Any]: ...


def as_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): nested for key, nested in value.items()}
    if hasattr(value, "model_dump"):
        dumped = cast(Dumpable, value).model_dump(mode="python")
        if isinstance(dumped, dict):
            return {str(key): nested for key, nested in dumped.items()}
    raise TypeError("provider event was not mapping-shaped")


def nested(value: object, *path: str, default: Any = None) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def text_content(parts: list[ContentPart]) -> str:
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            rendered.append(part.text)
        elif isinstance(part, (ImageReferencePart, FileReferencePart)):
            raise ValueError("artifact references require an artifact resolver")
    return "\n".join(rendered)


def assistant_text(message: AssistantMessage) -> str:
    return text_content(message.content)


def tool_definition(spec: object, *, anthropic: bool = False) -> dict[str, Any]:
    tool = ToolSpec.model_validate(spec)
    if anthropic:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        "strict": True,
    }


def failed_event(
    *,
    attempt: ModelAttempt,
    provider: str,
    model: str,
    sequence: int,
    category: str,
    provider_code: str | None = None,
    http_status: int | None = None,
    provider_parameter: str | None = None,
    stream_had_output: bool = False,
    detail: str | None = None,
) -> ModelFailedEvent:
    """Build a fixed-template failure that can never echo a raw response body."""

    safe_code = sanitize_provider_code(provider_code)
    safe_parameter = sanitize_provider_parameter(provider_parameter)
    error: ModelTransientError | ModelProtocolError | ModelPermanentError
    if category == "transient":
        error = ModelTransientError(
            provider=provider,
            model=model,
            attempt_id=attempt.attempt_id,
            message="the model provider reported a transient failure",
            provider_code=safe_code,
            http_status=http_status,
            provider_parameter=safe_parameter,
            stream_had_output=stream_had_output,
        )
    elif category == "protocol":
        error = ModelProtocolError(
            provider=provider,
            model=model,
            attempt_id=attempt.attempt_id,
            message="the model provider stream violated the normalized protocol",
            provider_code=safe_code,
            http_status=http_status,
            provider_parameter=safe_parameter,
            detail=detail or "provider protocol violation",
        )
    else:
        error = ModelPermanentError(
            provider=provider,
            model=model,
            attempt_id=attempt.attempt_id,
            message="the model provider rejected the request",
            provider_code=safe_code,
            http_status=http_status,
            provider_parameter=safe_parameter,
        )
    return ModelFailedEvent(
        attempt_id=attempt.attempt_id,
        run_id=attempt.run_id,
        step_number=attempt.step_number,
        sequence=sequence,
        error=error,
    )
