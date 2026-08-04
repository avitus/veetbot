"""Stable application and domain failures."""

from __future__ import annotations


class AgentCoreError(Exception):
    """Base class for typed platform failures."""


class NotFoundError(AgentCoreError):
    """A tenant-scoped object does not exist or is not visible."""


class ConflictError(AgentCoreError):
    """The requested guarded state change is not valid."""


class WorkerFencedError(ConflictError):
    """A durable worker no longer owns the lease epoch used for a write."""


class BudgetExceededError(AgentCoreError):
    """A configured run limit was reached before more work began."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ToolValidationError(AgentCoreError):
    """A tool schema or invocation argument failed validation."""


class ModelScriptExhaustedError(AgentCoreError):
    """A deterministic fake provider was called beyond its authored script."""


class RunCancelledError(AgentCoreError):
    """The run cancellation token was observed at a safe boundary."""


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
