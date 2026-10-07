"""Bounded content-free browser phase evidence; never action authority (ADR-0158)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Literal, ParamSpec, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Phase = Literal[
    "binding",
    "acquisition",
    "launch",
    "navigation",
    "readiness",
    "observation",
    "dispatch",
    "projection",
    "cleanup",
    "operation",
    "postcondition",
]
Placement = Literal["orchestrator", "hosted", "runtime"]
Failure = Literal[
    "none",
    "stale_page",
    "target_missing",
    "login_needed",
    "profile_unavailable",
    "browser_unavailable",
    "access_refused",
    "unsupported",
    "outcome_unknown",
    "output_invalid",
    "grant_refused",
    "timeout",
    "internal",
    "cancelled",
]
Outcome = Literal["completed", "failed", "cancelled", "bound_expired"]


class BrowserPhaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    phase: Phase
    placement: Placement
    outcome: Outcome
    failure: Failure = "none"
    elapsed_ms: int = Field(ge=0, le=3_600_000)


class BrowserDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = 1
    phases: tuple[BrowserPhaseRecord, ...] = Field(default=(), max_length=64)
    truncated: bool = False
    elapsed_ms: int = Field(ge=0, le=3_600_000)


_FAILURES: dict[str, Failure] = {
    "tool.browser.page_changed": "stale_page",
    "tool.browser.element_not_found": "target_missing",
    "tool.browser.authentication_required": "login_needed",
    "tool.browser.needs_user": "login_needed",
    "tool.browser.profile_unavailable": "profile_unavailable",
    "tool.browser.provider_unavailable": "browser_unavailable",
    "tool.browser.url_disallowed": "access_refused",
    "tool.browser.action_not_allowed": "unsupported",
    "tool.browser.outcome_unknown": "outcome_unknown",
    "tool.browser.output_invalid": "output_invalid",
    "tool.browser.grant_not_applicable": "grant_refused",
}


def browser_failure_category(reason: str) -> Failure:
    return _FAILURES.get(reason, "internal")


def _failure(error: BaseException) -> Failure:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, TimeoutError):
        return "timeout"
    reason = getattr(error, "reason_code", None)
    return browser_failure_category(reason) if isinstance(reason, str) else "internal"


def _milliseconds(start: float, clock: Callable[[], float]) -> int:
    return max(0, min(3_600_000, int((clock() - start) * 1000)))


@dataclass
class BrowserDiagnosticCollector:
    clock: Callable[[], float]
    _started: float
    _phases: list[BrowserPhaseRecord] = field(default_factory=list)
    _truncated: bool = False

    def append(self, record: BrowserPhaseRecord) -> None:
        if len(self._phases) < 64:
            self._phases.append(record)
        else:
            self._truncated = True

    def snapshot(self) -> BrowserDiagnostics:
        return BrowserDiagnostics(
            phases=tuple(self._phases),
            truncated=self._truncated,
            elapsed_ms=_milliseconds(self._started, self.clock),
        )


_CURRENT: ContextVar[BrowserDiagnosticCollector | None] = ContextVar(
    "browser_diagnostics", default=None
)


@contextmanager
def collect_browser_diagnostics(
    *,
    clock: Callable[[], float],
    enabled: bool = True,
) -> Iterator[BrowserDiagnosticCollector]:
    collector = BrowserDiagnosticCollector(clock=clock, _started=clock())
    token = _CURRENT.set(collector if enabled else None)
    try:
        yield collector
    finally:
        _CURRENT.reset(token)


@dataclass
class PhaseOutcome:
    outcome: Outcome = "completed"
    failure: Failure = "none"


@contextmanager
def browser_phase(phase: Phase, placement: Placement = "runtime") -> Iterator[PhaseOutcome]:
    collector = _CURRENT.get()
    result = PhaseOutcome()
    if collector is None:
        yield result
        return
    started = collector.clock()
    try:
        yield result
    except BaseException as error:
        result.failure = _failure(error)
        result.outcome = "cancelled" if result.failure == "cancelled" else "failed"
        raise
    finally:
        if collector is not None:
            collector.append(
                BrowserPhaseRecord(
                    phase=phase,
                    placement=placement,
                    outcome=result.outcome,
                    failure=result.failure,
                    elapsed_ms=_milliseconds(started, collector.clock),
                )
            )


P = ParamSpec("P")
T = TypeVar("T")


def browser_phase_call(
    phase: Phase,
    placement: Placement = "runtime",
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Coroutine[Any, Any, T]]]:
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with browser_phase(phase, placement):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


def admit_browser_diagnostics(encoded: str) -> None:
    """Validate the entire hosted report, including bounds, before accepting any record."""
    collector = _CURRENT.get()
    if collector is None:
        return
    try:
        report = BrowserDiagnostics.model_validate_json(encoded)
    except (ValidationError, ValueError):
        return
    for record in report.phases:
        collector.append(record)
    collector._truncated |= report.truncated
