"""Content-free failures that may cross the MCP boundary."""

from __future__ import annotations

from svp_mcp.constants import STABLE_FAILURE_CODES


class SvpError(RuntimeError):
    """A normalized failure whose string form is only its stable code."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_FAILURE_CODES:
            raise ValueError("unknown Scale VP failure code")
        self.code = code
        super().__init__(code)


class SvpRejectionError(SvpError):
    """The service's own client-error answer; its message stays out of the string form."""

    def __init__(self, status: int, problem: str) -> None:
        super().__init__("svp.provider_rejected")
        self.status = status
        self.problem = problem
