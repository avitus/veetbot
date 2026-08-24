"""The closed failure vocabulary that crosses the stdio pipe."""

from __future__ import annotations

CREDENTIAL_REJECTED = "gmail.credential_rejected"
RATE_LIMITED = "gmail.rate_limited"
UNAVAILABLE = "gmail.unavailable"
REJECTED = "gmail.rejected"
INVALID_OUTPUT = "gmail.invalid_output"

STABLE_CODES = frozenset({CREDENTIAL_REJECTED, RATE_LIMITED, UNAVAILABLE, REJECTED, INVALID_OUTPUT})


class GmailServerError(Exception):
    """A failure whose string form is exactly one stable, content-free code.

    Upstream Google text, headers, and token material must never reach this
    type; the MCP layer stringifies the exception into the tool error, so
    the constructor accepts a code and nothing else.
    """

    def __init__(self, code: str) -> None:
        if code not in STABLE_CODES:
            raise ValueError(f"not a stable gmail_mcp failure code: {code!r}")
        super().__init__(code)
        self.code = code
