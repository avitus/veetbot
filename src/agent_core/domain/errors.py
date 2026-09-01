"""Stable application and domain failures."""

from __future__ import annotations

from uuid import UUID


class AgentCoreError(Exception):
    """Base class for typed platform failures."""


class AuthenticationError(AgentCoreError):
    """Authentication did not produce a principal."""


class NotFoundError(AgentCoreError):
    """A tenant-scoped object does not exist or is not visible."""


class ConflictError(AgentCoreError):
    """The requested guarded state change is not valid."""

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


class PersonaContentError(AgentCoreError):
    """Persona text was refused before persistence: secrets, injection
    patterns, oversize entries, or provenance the caller may not mint."""


class ScheduleValidationError(AgentCoreError):
    """A schedule definition failed one stable boundary rule."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class DeviceValidationError(AgentCoreError):
    """A device registration failed one stable boundary rule."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class DelegationValidationError(AgentCoreError):
    """A delegation request failed one stable boundary rule."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class InvalidStateTransition(ConflictError):  # noqa: N818 - normative taxonomy name
    """A resource exists but cannot accept the requested state change."""


class ConcurrencyConflict(ConflictError):  # noqa: N818 - normative taxonomy name
    """A concurrent guarded operation won the race."""


class WorkerFencedError(ConflictError):
    """A durable worker no longer owns the lease epoch used for a write."""


class BudgetExceededError(AgentCoreError):
    """A configured run limit was reached before more work began."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class DeadlineExceeded(AgentCoreError):  # noqa: N818 - normative taxonomy name
    """An operation exceeded its deadline."""


class RunDeadlineExceeded(DeadlineExceeded):
    """A run exceeded its configured deadline."""


class ToolValidationError(AgentCoreError):
    """A tool schema or invocation argument failed validation."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        errors: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.errors = errors or []


class ToolTrustRejectedError(ToolValidationError):
    """A tool rejected content because its provenance trust was insufficient."""


class ToolNotFoundError(AgentCoreError):
    """A requested tool is not registered."""


class AuthorizationError(AgentCoreError):
    """The authenticated principal lacks authority for an action."""


class ToolPolicyDenied(AuthorizationError):  # noqa: N818 - normative taxonomy name
    """Policy denied a requested tool action."""


class ApprovalRequiredError(AgentCoreError):
    """Normal control flow indicating a durable approval has parked the run."""

    def __init__(self, approval_id: UUID) -> None:
        super().__init__(f"approval {approval_id} is pending")
        self.approval_id = approval_id


class UserInputRequiredError(AgentCoreError):
    """Normal control flow indicating a run is waiting for a user's answer."""

    def __init__(self, question_id: UUID, invocation_id: UUID) -> None:
        super().__init__(f"question {question_id} is waiting for input")
        self.question_id = question_id
        self.invocation_id = invocation_id


class ChildRunRequiredError(AgentCoreError):
    """Normal control flow indicating child runs have parked the parent."""

    def __init__(
        self,
        delegation_id: UUID,
        invocation_id: UUID,
        child_run_ids: list[UUID],
    ) -> None:
        super().__init__(f"delegation {delegation_id} is running")
        self.delegation_id = delegation_id
        self.invocation_id = invocation_id
        self.child_run_ids = child_run_ids


class ApprovalDenied(ConflictError):  # noqa: N818 - normative taxonomy name
    """A required approval was denied."""


class ApprovalExpired(ConflictError):  # noqa: N818 - normative taxonomy name
    """A required approval expired before resolution."""


class WorkspaceEscape(ToolValidationError):  # noqa: N818 - normative domain name
    """A path failed the workspace containment boundary."""


class WorkspaceReadLimitExceededError(ToolValidationError):
    """A bounded workspace read exceeded its declared byte limit."""


class ModelScriptExhaustedError(AgentCoreError):
    """A deterministic fake provider was called beyond its authored script."""


class RunCancelledError(AgentCoreError):
    """The run cancellation token was observed at a safe boundary."""


class ContextOverflow(AgentCoreError):  # noqa: N818 - normative taxonomy name
    """The request cannot fit the configured context window."""


class SkillValidationError(AgentCoreError):
    """A skill package failed one named, total validation rule."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


class SkillRevisionConflict(ConflictError):  # noqa: N818 - normative taxonomy name
    """Optimistic skill revision assignment observed a newer winner."""

    def __init__(self, current_revision: int) -> None:
        super().__init__(
            "skill revision changed during installation",
            reason="skill_revision_conflict",
            details={"current_revision": current_revision},
        )
        self.current_revision = current_revision


class MCPUnavailableError(AgentCoreError):
    """An MCP server is unavailable without escaping the tool outcome vocabulary."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class MCPTransportError(MCPUnavailableError):
    """An established MCP transport disconnected or could not complete a request."""

    def __init__(self) -> None:
        super().__init__("tool.server_unreachable")


class MCPUnauthorizedError(MCPUnavailableError):
    """An MCP connection or call was rejected as unauthorized."""

    def __init__(self) -> None:
        super().__init__("tool.server_unauthorized")


class ToolLoopDetected(ConflictError):  # noqa: N818 - normative taxonomy name
    """The runtime detected a repeated tool-call loop."""


class ModelTransientError(AgentCoreError):
    """A model provider reported a retryable failure."""


class ModelPermanentError(AgentCoreError):
    """A model provider reported a permanent failure."""


class ModelProtocolError(AgentCoreError):
    """A model provider response violated its protocol."""


class ToolTimeoutError(AgentCoreError):
    """A tool call exceeded its deadline."""


class ToolExecutionError(AgentCoreError):
    """A tool failed while executing."""


class ToolResultValidationError(AgentCoreError):
    """A tool returned a result outside its declared schema."""


class SandboxProvisionError(AgentCoreError):
    """An isolated execution environment could not be created."""


class SandboxExecutionError(AgentCoreError):
    """An isolated execution environment failed during execution."""


class ExecutionUnavailable(SandboxExecutionError):  # noqa: N818
    """The execution service cannot provision or reach a sandbox."""


class ExecutionRejected(SandboxExecutionError):  # noqa: N818
    """A sandbox command or handle was rejected before execution."""


class ArtifactStorageError(AgentCoreError):
    """Artifact storage could not satisfy an operation."""


class ArtifactIntegrityError(ArtifactStorageError):
    """Artifact bytes did not match their declared checksum or size."""


class ExportConsentError(AgentCoreError):
    """Trajectory export was not authorized for this run."""


class ExportStateError(AgentCoreError):
    """Trajectory export state is not ready or is no longer exportable."""


class ExportRedactionError(AgentCoreError):
    """A finished export still contained sensitive-shaped content."""

    def __init__(self, rule: str, message_index: int | None) -> None:
        location = "document" if message_index is None else f"message {message_index}"
        super().__init__(f"trajectory export refused: {rule} remained in {location}")
        self.rule = rule
        self.message_index = message_index


class ExportRedactionPatternError(AgentCoreError):
    """A tenant redaction pattern is invalid or outside the safe subset."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"tenant redaction pattern {name!r} is invalid: {reason}")
        self.name = name
        self.reason = reason


class ArtifactSweepError(AgentCoreError):
    """An expiry sweep attempted every object but could not delete them all."""

    def __init__(self, *, deleted: int, failed: int) -> None:
        super().__init__(f"artifact sweep deleted {deleted} object(s); {failed} deletion(s) failed")
        self.deleted = deleted
        self.failed = failed


class EvalExpectationError(AgentCoreError):
    """A deterministic evaluation result did not match its authored expectation."""


# Canonical API taxonomy names. The earlier ``*Error`` spellings remain for
# compatibility with the runtime while the wire vocabulary stays stable.
class WorkerFenced(WorkerFencedError):  # noqa: N818 - normative taxonomy name
    """Internal lease-fencing signal; never returned by the API."""


class BudgetExceeded(BudgetExceededError):  # noqa: N818 - normative taxonomy name
    """Canonical boundary name for a budget failure."""


class ApprovalRequired(ApprovalRequiredError):  # noqa: N818 - normative taxonomy name
    """Canonical boundary name for a parked approval."""


class RunCancelled(RunCancelledError):  # noqa: N818 - normative taxonomy name
    """Canonical boundary name for cooperative cancellation."""


class EmptyModelTurn(AgentCoreError):  # noqa: N818 - normative taxonomy name
    """Internal retry signal; never returned by the API."""
