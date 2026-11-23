"""One approval for the scripts of a request (ADR-0342 decision 11).

A script written after the turn read outside content asks before it runs. The
approval the owner resolves with ``approve_for_request`` is the grant: it
belongs to the run that answers the owner's message, lasts at most two hours,
and is found again through the ``script.request_grant.created`` event that
resolution appends. It covers starting a later script of the same run. It
never covers a call a script makes; each of those is decided by itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.execution import SANDBOX_RUN_CODE_TOOL_NAME

SCRIPT_REQUEST_GRANT_DURATION: Final = timedelta(hours=2)
SCRIPT_REQUEST_GRANT_AUTHORIZATION_KIND: Final = "script_request_grant"
SCRIPT_REQUEST_GRANT_AUTHORIZED_REASON: Final = "script.request_grant.authorized"
SCRIPT_REQUEST_GRANT_CREATED_EVENT: Final = "script.request_grant.created"
# The denial when the run has no request approval for scripts; it names no grant.
NO_SCRIPT_REQUEST_GRANT: Final = "script.request_grant.none"
# The server-authored offer text.
SCRIPT_REQUEST_GRANT_SUMMARY: Final = (
    "Allow for this request: run the scripts Veetbot writes for this request without "
    "asking again. Each tool a script uses is still checked on its own, so Veetbot "
    "still asks before a script sends, deletes or changes anything."
)


class ScriptRequestGrantOffer(BaseModel):
    """The server-authored offer stored on a script approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: Literal["sandbox.run_code"] = SANDBOX_RUN_CODE_TOOL_NAME
    duration_seconds: Literal[7200] = 7200
    summary: str = Field(min_length=1, max_length=1024)
