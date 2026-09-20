"""Closed values for RFC 8058 one-click unsubscribe requests."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class UnsubscribeOutcomeCode(StrEnum):
    """Every observable outcome; the last three are decided before any network activity."""

    ACCEPTED = "unsubscribe.accepted"
    REDIRECT_REFUSED = "unsubscribe.redirect_refused"
    REJECTED = "unsubscribe.rejected"
    DESTINATION_REFUSED = "unsubscribe.destination_refused"
    TLS_FAILED = "unsubscribe.tls_failed"
    UNREACHABLE = "unsubscribe.unreachable"
    EVIDENCE_CHANGED = "unsubscribe.evidence_changed"
    NOT_ELIGIBLE = "unsubscribe.not_eligible"
    REQUIRES_SEND = "unsubscribe.requires_send"


ONE_CLICK_BODY: Final = b"List-Unsubscribe=One-Click"
ONE_CLICK_CONTENT_TYPE: Final = "application/x-www-form-urlencoded"
UNSUBSCRIBE_USER_AGENT: Final = "Veetbot-Unsubscribe/1"
UNSUBSCRIBE_REQUEST_DEADLINE_SECONDS: Final = 10.0
UNSUBSCRIBE_MAX_RESPONSE_BYTES: Final = 65536
