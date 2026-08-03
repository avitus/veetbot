"""Stable application and domain failures."""

from __future__ import annotations


class AgentCoreError(Exception):
    """Base class for typed platform failures."""


class NotFoundError(AgentCoreError):
    """A tenant-scoped object does not exist or is not visible."""


class ConflictError(AgentCoreError):
    """The requested guarded state change is not valid."""


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


class EvalExpectationError(AgentCoreError):
    """A deterministic evaluation result did not match its authored expectation."""
