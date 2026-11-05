"""Sandbox-only entry point for a job module received on stdin."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from types import ModuleType

from agent_core.execution.job_sdk import JobContext

_VALUE_BYTES = 64 * 1024


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def main() -> None:
    """Execute the supplied program and emit a bounded structured result with repair feedback."""
    try:
        raw = sys.stdin.buffer.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("Job request exceeds 256 KiB.")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("Job request must be an object.")
        source, inputs = request.get("source"), request.get("inputs")
        if not isinstance(source, str) or not source.strip() or len(source.encode()) > _VALUE_BYTES:
            raise ValueError("Job source must contain between 1 byte and 64 KiB.")
        if not isinstance(inputs, dict) or len(_encode(inputs)) > _VALUE_BYTES:
            raise ValueError("Job inputs must be a JSON object of at most 64 KiB.")
        module = ModuleType("__veetbot_job__")
        # This module is installed and invoked only inside the execution image.
        # Redirect ordinary prints so they cannot corrupt the structured report.
        with redirect_stdout(sys.stderr):
            exec(compile(source, "<job>", "exec"), module.__dict__)  # noqa: S102
            entry = getattr(module, "main", None)
            if not callable(entry):
                raise ValueError("Job must define main(inputs, job).")
            value = entry(inputs, JobContext())
        if len(_encode(value)) > _VALUE_BYTES:
            raise ValueError("Job result exceeds 64 KiB.")
        report = _encode({"ok": True, "value": value})
    except (Exception, SystemExit) as exc:
        report = _encode({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]})
        sys.stdout.buffer.write(report + b"\n")
        raise SystemExit(1) from None
    sys.stdout.buffer.write(report + b"\n")


if __name__ == "__main__":
    main()
