"""Closed, action-specific owner consent for following one X profile."""

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserElementFacts

_HANDLE = r"[A-Za-z0-9_]{1,15}"
_RESERVED = {"home", "explore", "notifications", "messages", "settings", "search", "i"}


def profile_handle(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "x.com" or parsed.query or parsed.fragment:
        return None
    match = re.fullmatch(rf"/({_HANDLE})/?", parsed.path)
    value = match.group(1) if match else ""
    if re.fullmatch(_HANDLE, value) and value.lower() not in _RESERVED:
        return value.lower()
    return None


def follow_request(text: str, previous_answer: str) -> str | None:
    """Only a complete affirmative command; never a substring in prose or a quote."""
    normalized = " ".join(text.lower().split()).rstrip(".! ")
    normalized = re.sub(r"^(?:this is perfect[.!]?|yes[,.!]?|approved[,.!]?)\s+", "", normalized)
    explicit = re.fullmatch(rf"(?:please )?follow @({_HANDLE})", normalized)
    if explicit:
        value = explicit.group(1)
        return None if value in _RESERVED else value
    if normalized not in {
        "follow him",
        "follow her",
        "follow them",
        "please follow him",
        "please follow her",
        "please follow them",
    }:
        return None
    # Only complete profile links. Post permalinks, arbitrary @ mentions, and
    # user information inside a URL are not candidate identities.
    handles = {
        handle
        for url in re.findall(r"https://[^\s<>\[\]()]+", previous_answer)
        if (handle := profile_handle(url)) is not None
    }
    return next(iter(handles)) if len(handles) == 1 else None


def exact_follow_control(
    handle: str,
    *,
    action: BrowserAction,
    page_url: str,
    role: str,
    labels: Sequence[str],
    facts: BrowserElementFacts | None,
    disabled: bool,
) -> bool:
    expected = f"follow @{handle}".casefold()
    names = [" ".join(label.casefold().split()) for label in labels if label.strip()]
    return (
        profile_handle(page_url) == handle
        and action.kind is BrowserActionKind.CLICK
        and role == "button"
        and not disabled
        and bool(names)
        and all(name in {expected, "follow"} for name in names)
        and expected in names
        and facts is not None
        and not facts.labels_truncated
        and not facts.download
        and facts.form_target is None
        and facts.link_target is None
        and not facts.context_name
    )
