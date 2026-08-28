"""Content-free failures that may cross the MCP boundary."""

from __future__ import annotations

from gmail_mcp.constants import STABLE_FAILURE_CODES


class GmailError(RuntimeError):
    """A normalized failure whose string form is only its stable code."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_FAILURE_CODES:
            raise ValueError("unknown Gmail failure code")
        self.code = code
        super().__init__(code)
