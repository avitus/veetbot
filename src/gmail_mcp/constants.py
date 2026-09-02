"""Closed Gmail endpoints, scopes, modes, rosters, and failure codes."""

from __future__ import annotations

from typing import Final

GMAIL_API_ROOT: Final = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"
GOOGLE_AUTHORIZATION_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
LOOPBACK_REDIRECT_HOST: Final = "127.0.0.1"

GOOGLE_SCOPES: Final = {
    "read": "https://www.googleapis.com/auth/gmail.readonly",
    "write": "https://www.googleapis.com/auth/gmail.modify",
    "send": "https://www.googleapis.com/auth/gmail.send",
}

ROSTERS: Final = {
    "read": ("search_threads", "get_thread", "list_labels"),
    "write": ("create_draft", "modify_labels", "trash_thread", "untrash_thread"),
    "send": ("send_message",),
}

STABLE_FAILURE_CODES: Final = frozenset(
    {
        "gmail.credential_rejected",
        "gmail.rate_limited",
        "gmail.provider_unavailable",
        "gmail.provider_rejected",
        "gmail.provider_output_invalid",
        "gmail.outcome_unknown",
        "gmail.arguments_invalid",
    }
)

UPSTREAM_MAXIMUM_BYTES: Final = 8 * 1024 * 1024
OUTPUT_MAXIMUM_BYTES: Final = 1024 * 1024
