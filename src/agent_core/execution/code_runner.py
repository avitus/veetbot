"""Sandbox-only entry point for a code-mode script received on stdin (ADR-0342)."""

from __future__ import annotations

import sys
import traceback

from agent_core.execution.tool_sdk import ToolCallError, Tools

_SOURCE_BYTES = 64 * 1024


def main() -> None:
    """Execute the supplied script with `tools` bound; its prints are its answer."""
    raw = sys.stdin.buffer.read(_SOURCE_BYTES + 1)
    try:
        if len(raw) > _SOURCE_BYTES:
            raise ValueError("Script exceeds 64 KiB.")
        source = raw.decode("utf-8")
        if not source.strip():
            raise ValueError("Script is empty.")
        program = compile(source, "<code>", "exec")
    except (SyntaxError, ValueError) as exc:
        sys.stderr.write("".join(traceback.format_exception_only(type(exc), exc)))
        raise SystemExit(1) from None
    namespace = {"__name__": "__main__", "tools": Tools(), "ToolCallError": ToolCallError}
    try:
        # This module is installed and invoked only inside the execution image.
        exec(program, namespace)  # noqa: S102
    except SystemExit:
        raise
    except BaseException as exc:
        # Show the script's own frames, not this runner's.
        sys.stdout.flush()
        traceback.print_exception(
            type(exc), exc, exc.__traceback__.tb_next if exc.__traceback__ else None
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
