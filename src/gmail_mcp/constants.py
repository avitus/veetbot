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
    "read": (
        "search_threads",
        "get_thread",
        "list_labels",
        "get_profile",
        "sync_changes",
        "get_thread_page",
        "get_message_body",
        "get_unsubscribe",
        "fetch_attachment",
        "stage_attachment",
        "save_attachment",
    ),
    "write": ("create_draft", "modify_labels", "trash_thread", "untrash_thread"),
    "send": ("send_message", "send_attached_message"),
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
        "gmail.attachment_not_found",
        "gmail.attachment_too_large",
    }
)

UPSTREAM_MAXIMUM_BYTES: Final = 8 * 1024 * 1024
OUTPUT_MAXIMUM_BYTES: Final = 1024 * 1024
# ADR-0296: the chat upload limit. Only the attachment request gets the larger
# upstream bound, which holds this many bytes in base64 plus its JSON wrapper.
ATTACHMENT_MAXIMUM_BYTES: Final = 32 * 1024 * 1024
ATTACHMENT_UPSTREAM_MAXIMUM_BYTES: Final = (ATTACHMENT_MAXIMUM_BYTES // 3 + 1) * 4 + 64 * 1024
