"""The bounded program and result of one agent-written job (ADR-0268)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JOB_CALLS = frozenset({"http.request", "model.complete", "feed.publish", "log"})
JOB_SOURCE_BYTES = 64 * 1024
JOB_INPUT_BYTES = 64 * 1024
JOB_RESULT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class JobProgram:
    source: str
    calls: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.source.strip() or len(self.source.encode("utf-8")) > JOB_SOURCE_BYTES:
            raise ValueError("Job source must contain between 1 byte and 64 KiB.")
        if not self.calls <= JOB_CALLS:
            raise ValueError("Job declares an unsupported broker operation.")


@dataclass(frozen=True, slots=True)
class JobResult:
    value: Any
    diagnostics: str
    broker_calls: int


class JobExecutionError(ValueError):
    """A job failed; the message is untrusted repair feedback for its author."""
