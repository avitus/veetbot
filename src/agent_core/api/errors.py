"""Single class-to-code-to-status map for the HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.domain import errors as domain_errors


@dataclass(frozen=True, slots=True)
class ErrorMapping:
    code: str
    status: int


ERROR_STATUS_MAP: dict[type[BaseException], ErrorMapping] = {
    domain_errors.AuthenticationError: ErrorMapping("authentication_error", 401),
    domain_errors.AuthorizationError: ErrorMapping("authorization_error", 403),
    domain_errors.NotFoundError: ErrorMapping("not_found", 404),
    domain_errors.ConflictError: ErrorMapping("conflict", 409),
    domain_errors.ScheduleValidationError: ErrorMapping("schedule_validation_error", 422),
    domain_errors.DeviceValidationError: ErrorMapping("device_validation_error", 422),
    domain_errors.DeviceIngestError: ErrorMapping("device_ingest_error", 422),
    domain_errors.DeviceChannelUnavailable: ErrorMapping("device_channel_unavailable", 503),
    domain_errors.InvalidStateTransition: ErrorMapping("invalid_state_transition", 409),
    domain_errors.ToolNotFoundError: ErrorMapping("tool_not_found", 404),
    domain_errors.ToolValidationError: ErrorMapping("tool_validation_error", 422),
    domain_errors.ToolPolicyDenied: ErrorMapping("tool_policy_denied", 403),
    domain_errors.ApprovalRequired: ErrorMapping("approval_required", 409),
    domain_errors.ApprovalDenied: ErrorMapping("approval_denied", 409),
    domain_errors.ApprovalExpired: ErrorMapping("approval_expired", 409),
    domain_errors.BudgetExceeded: ErrorMapping("budget_exceeded", 402),
    domain_errors.DeadlineExceeded: ErrorMapping("deadline_exceeded", 504),
    domain_errors.RunDeadlineExceeded: ErrorMapping("run_deadline_exceeded", 504),
    domain_errors.RunCancelled: ErrorMapping("run_cancelled", 409),
    domain_errors.ContextOverflow: ErrorMapping("context_overflow", 422),
    domain_errors.ToolLoopDetected: ErrorMapping("tool_loop_detected", 409),
    domain_errors.ModelTransientError: ErrorMapping("model_transient_error", 503),
    domain_errors.ModelPermanentError: ErrorMapping("model_permanent_error", 502),
    domain_errors.ModelProtocolError: ErrorMapping("model_protocol_error", 502),
    domain_errors.ToolTimeoutError: ErrorMapping("tool_timeout", 504),
    domain_errors.ToolExecutionError: ErrorMapping("tool_execution_error", 502),
    domain_errors.ToolResultValidationError: ErrorMapping("tool_result_invalid", 502),
    domain_errors.SandboxProvisionError: ErrorMapping("sandbox_provision_error", 503),
    domain_errors.SandboxExecutionError: ErrorMapping("sandbox_execution_error", 502),
    domain_errors.ArtifactStorageError: ErrorMapping("artifact_storage_error", 503),
    domain_errors.ConcurrencyConflict: ErrorMapping("concurrency_conflict", 409),
    # Runtime compatibility names represent the same public conditions.
    domain_errors.ApprovalRequiredError: ErrorMapping("approval_required", 409),
    domain_errors.BudgetExceededError: ErrorMapping("budget_exceeded", 402),
    domain_errors.RunCancelledError: ErrorMapping("run_cancelled", 409),
}

API_ERROR_STATUS: dict[str, int] = {
    "malformed_request": 400,
    "unsupported_media_type": 415,
    "payload_too_large": 413,
    "rate_limited": 429,
    "internal_error": 500,
}

# One class, several conditions: the reason decides the status the way the
# closed reason vocabulary already decides the `details` body.
DEVICE_INGEST_STATUS: dict[str, int] = {
    "ingest_daily_cap": 429,
    "channel_disabled": 409,
    "channel_unsupported": 422,
    "sender_invalid": 422,
    "body_invalid": 422,
}

ERROR_CODE_VOCABULARY = frozenset(
    {mapping.code for mapping in ERROR_STATUS_MAP.values()} | set(API_ERROR_STATUS)
)
INTERNAL_ONLY_ERROR_TYPES = frozenset({domain_errors.WorkerFenced, domain_errors.EmptyModelTurn})


def mapping_for(exc: BaseException) -> ErrorMapping:
    """Resolve through the MRO so a subclass has exactly one effective map."""

    if isinstance(exc, domain_errors.DeviceIngestError):
        return ErrorMapping(
            "device_ingest_error",
            DEVICE_INGEST_STATUS.get(exc.reason, 422),
        )
    for error_type in type(exc).__mro__:
        mapped = ERROR_STATUS_MAP.get(error_type)
        if mapped is not None:
            return mapped
    return ErrorMapping("internal_error", 500)


def details_for(exc: BaseException, code: str) -> dict[str, object]:
    if code == "conflict" and isinstance(exc, domain_errors.ConflictError):
        details = dict(exc.details)
        if exc.reason is not None:
            details = {"reason": exc.reason, **details}
        return details
    if code == "tool_validation_error" and isinstance(exc, domain_errors.ToolValidationError):
        return {"tool_name": exc.tool_name, "errors": exc.errors}
    if code == "schedule_validation_error" and isinstance(
        exc, domain_errors.ScheduleValidationError
    ):
        return {"reason": exc.reason}
    if code == "device_validation_error" and isinstance(exc, domain_errors.DeviceValidationError):
        return {"reason": exc.reason}
    if code == "device_ingest_error" and isinstance(exc, domain_errors.DeviceIngestError):
        return {"reason": exc.reason}
    if code == "device_channel_unavailable" and isinstance(
        exc, domain_errors.DeviceChannelUnavailable
    ):
        return {"reason": exc.reason}
    return {}
