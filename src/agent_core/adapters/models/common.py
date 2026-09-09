"""SDK-free request and raw-event helpers shared by provider adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from agent_core.domain.messages import (
    AssistantMessage,
    ContentPart,
    FileReferencePart,
    ImageReferencePart,
    ModelAttempt,
    ModelEvent,
    ModelFailedEvent,
    ModelPermanentError,
    ModelProtocolError,
    ModelTransientError,
    TextPart,
    sanitize_provider_code,
    sanitize_provider_parameter,
)
from agent_core.model.tool_definitions import tool_definition as tool_definition

type RawEventSource = Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]]

_TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 429})
_TRANSIENT_PROVIDER_CODES = frozenset(
    {
        "api_error",
        "internal_error",
        "overloaded_error",
        "rate_limit_error",
        "request_timeout",
        "server_error",
        "service_unavailable",
        "temporarily_unavailable",
        "timeout",
    }
)
_PERMANENT_PROVIDER_CODES = frozenset(
    {
        "authentication_error",
        "billing_hard_limit_reached",
        "insufficient_quota",
        "invalid_api_key",
        "invalid_request",
        "invalid_request_error",
        "not_found_error",
        "permission_error",
        "request_too_large",
        "unprocessable_entity",
        "usage_limit_reached",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """Safe, provider-neutral classification of an upstream failure."""

    category: Literal["transient", "permanent"]
    provider_code: str
    provider_parameter: str | None


def classify_provider_failure(
    body: object,
    *,
    http_status: int | None = None,
    fallback_code: str,
    default_transient: bool = False,
) -> ProviderFailure:
    """Classify a failure without retaining provider-controlled prose.

    Safe permanent codes override retryable HTTP statuses so quota exhaustion
    is never retried as an ordinary rate limit. Provider transient codes take
    precedence over other 4xx statuses because Anthropic reports overloads as
    an error type even when a compatible endpoint uses a generic status.
    """

    mapping = body if isinstance(body, dict) else {}
    raw_error = mapping.get("error", mapping)
    error = raw_error if isinstance(raw_error, dict) else {}
    raw_code = error.get("code")
    raw_type = error.get("type")
    code = sanitize_provider_code(raw_code if isinstance(raw_code, str) else None)
    error_type = sanitize_provider_code(raw_type if isinstance(raw_type, str) else None)
    parameter_value = error.get("param", error.get("parameter"))
    parameter = sanitize_provider_parameter(
        parameter_value if isinstance(parameter_value, str) else None
    )
    candidates = {
        value.lower()
        for value in (code, error_type)
        if value is not None and value != "provider_error"
    }
    if candidates & _PERMANENT_PROVIDER_CODES:
        transient = False
    elif candidates & _TRANSIENT_PROVIDER_CODES:
        transient = True
    elif http_status is not None:
        transient = http_status in _TRANSIENT_HTTP_STATUSES or http_status >= 500
    else:
        transient = default_transient
    provider_code = code or error_type or sanitize_provider_code(fallback_code) or "provider_error"
    return ProviderFailure(
        category="transient" if transient else "permanent",
        provider_code=provider_code,
        provider_parameter=parameter,
    )


def should_retry_failure_event(
    event: ModelEvent,
    *,
    internal_attempt: int,
    max_internal_attempts: int,
) -> bool:
    """Return whether an in-band failure can be retried before caller-visible output."""

    return (
        internal_attempt < max_internal_attempts
        and isinstance(event, ModelFailedEvent)
        and isinstance(event.error, ModelTransientError)
        and not event.error.stream_had_output
        and event.sequence == 0
    )


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
