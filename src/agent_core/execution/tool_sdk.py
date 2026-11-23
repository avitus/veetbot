"""Standard-library-only client for the tool bridge (inside the sandbox).

A code-mode script reaches the run's tools through `Tools` (ADR-0342). The
library holds no tool list, token or credential: the relay adds the turn token
and the bridge decides whether a name may run.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

_MESSAGE_BYTES = 64 * 1024
# Refusals the bridge issues before it counts the call. The next request must
# repeat the ordinal, or every later call would be a protocol error.
_UNCOUNTED = frozenset(
    {
        "bridge.call_limit",
        "bridge.unauthorized",
        "bridge.protocol_error",
        "bridge.request_too_large",
    }
)


class ToolCallError(RuntimeError):
    """A bridged tool call did not succeed; the fields are the bridge's outcome."""

    def __init__(self, status: str, reason_code: str, *, retryable: bool = False) -> None:
        super().__init__(f"{status}: {reason_code}")
        self.status = status
        self.reason_code = reason_code
        self.retryable = retryable


class _Path:
    """An attribute path such as `tools.email.search`; calling it sends the name."""

    def __init__(self, tools: Tools, name: str) -> None:
        self._tools = tools
        self._name = name

    def __getattr__(self, part: str) -> _Path:
        if part.startswith("_"):
            raise AttributeError(part)
        return _Path(self._tools, f"{self._name}.{part}")

    def __call__(self, **arguments: Any) -> Any:
        return self._tools.call(self._name, arguments)

    def __repr__(self) -> str:
        return f"<tool {self._name}>"


class Tools:
    """Synchronous, sequential tool calls; constructing it performs no I/O."""

    def __init__(self) -> None:
        self._ordinal = 0

    def __getattr__(self, part: str) -> _Path:
        if part.startswith("_"):
            raise AttributeError(part)
        return _Path(self, part)

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call one tool by its full name and return its text, parsed when it is JSON."""
        if not isinstance(name, str) or not name:
            raise TypeError("A tool name must be a non-empty string.")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be a dict of named values.")
        # Encode before any I/O, so a request that cannot be sent takes no ordinal.
        encoded = json.dumps(
            {"call": name, "arguments": arguments, "ordinal": self._ordinal},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        # Leave room for the relay's token and the newline in the existing frame.
        if len(encoded) > _MESSAGE_BYTES - 1024:
            raise ValueError("Tool arguments exceed the bridge message limit.")
        path = os.environ.get("AGENT_TOOL_BRIDGE_SOCKET")
        if not path:
            raise ToolCallError("unavailable", "bridge.unavailable")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(path)
            connection.sendall(encoded + b"\n")
            # No read timeout: an approval hold is bounded by the bridge, and the
            # command's own deadline ends a script that waits past it.
            with connection.makefile("rb") as stream:
                response = stream.readline(_MESSAGE_BYTES + 1)
        outcome = _outcome(response)
        if outcome.get("reason_code") not in _UNCOUNTED:
            self._ordinal += 1
        status = outcome.get("status")
        if status != "succeeded":
            raise ToolCallError(
                str(status or "failed")[:64],
                str(outcome.get("reason_code") or "bridge.failed")[:200],
                retryable=outcome.get("retryable") is True,
            )
        return _value(outcome.get("result"))


def _outcome(response: bytes) -> dict[str, Any]:
    """Decode one bridge frame; an unreadable frame is a counted failure."""
    invalid = {"status": "failed", "reason_code": "bridge.invalid_response"}
    if not response.endswith(b"\n") or len(response) > _MESSAGE_BYTES:
        return invalid
    try:
        outcome = json.loads(response)
    except (UnicodeDecodeError, ValueError):
        return invalid
    return outcome if isinstance(outcome, dict) else invalid


def _value(result: object) -> Any:
    """Return what the model would have read: the text parts, parsed when they are JSON."""
    if not isinstance(result, list):
        return result
    text = "\n".join(
        part["text"]
        for part in result
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )
    if text[:1] in {"{", "["}:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text
