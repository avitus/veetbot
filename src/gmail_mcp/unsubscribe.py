"""Syntactic List-Unsubscribe evidence, and the signatures that vouch for it.

Nothing here resolves, dials, or authorizes an address. It reads the headers one
message already carries and normalizes them, so the caller decides on a closed
value rather than on free text.
"""

from __future__ import annotations

import re
from typing import Any, Final, NamedTuple
from urllib.parse import unquote

LIST_UNSUBSCRIBE: Final = "list-unsubscribe"
LIST_UNSUBSCRIBE_POST: Final = "list-unsubscribe-post"
MECHANISM_HEADERS: Final = frozenset({LIST_UNSUBSCRIBE, LIST_UNSUBSCRIBE_POST})
ONE_CLICK_MARKER: Final = "List-Unsubscribe=One-Click"
GMAIL_AUTHSERV_ID: Final = "mx.google.com"
NO_OFFER: Final = "none"

BULK_HEADERS: Final = ("List-Unsubscribe", "List-Unsubscribe-Post", "List-Id")
EVIDENCE_HEADERS: Final = (*BULK_HEADERS, "From", "Authentication-Results", "DKIM-Signature")

_HEADER_MAXIMUM: Final = 8192
_HTTPS_MAXIMUM: Final = 2048
_LIST_ID_MAXIMUM: Final = 255
_RECIPIENT_MAXIMUM: Final = 254
_SUBJECT_MAXIMUM: Final = 256
_BODY_MAXIMUM: Final = 1024

_LINE_BREAK: Final = re.compile(r"\r\n|[\r\n]")
_WHITESPACE: Final = re.compile(r"\s+")
_BRACKETED: Final = re.compile(r"<([^<>]*)>")
_DKIM_PASS: Final = re.compile(r"\s*dkim(?:/\d+)?\s*=\s*pass(?![-\w])", re.IGNORECASE)
_UNSAFE_RECIPIENT: Final = re.compile(r"[\s\x00-\x1f\x7f]")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_BUT_NEWLINE: Final = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


class Offer(NamedTuple):
    """What `List-Unsubscribe` claims before any signature is consulted."""

    offered: str
    https_uri: str
    mailto: str


def unfold(value: str) -> str:
    """Undo RFC 5322 folding so one logical header value parses as one line."""
    return _LINE_BREAK.sub(" ", value)


def header_values(payload: object, name: str) -> list[str]:
    """Every value for one header name in document order, top of the message first."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("headers")
    if not isinstance(rows, list):
        return []
    wanted = name.casefold()
    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_name = row.get("name")
        value = row.get("value")
        if isinstance(row_name, str) and isinstance(value, str) and row_name.casefold() == wanted:
            values.append(value[:_HEADER_MAXIMUM])
    return values


def last_header(payload: object, name: str) -> str:
    """The last value wins: DKIM signs upward, so a header prepended in transit is not it."""
    values = header_values(payload, name)
    return values[-1] if values else ""


def normalize_list_id(value: str) -> str:
    """The bracketed identifier when the header carries one, else its trimmed text."""
    unfolded = unfold(value)
    bracketed = _BRACKETED.findall(unfolded)
    identifier = bracketed[-1].strip() if bracketed else unfolded.strip()
    return identifier.casefold()[:_LIST_ID_MAXIMUM]


def _addresses(value: str) -> list[str]:
    """Each bracketed address in order; whitespace inside the brackets is not significant."""
    return ["".join(match.group(1).split()) for match in _BRACKETED.finditer(unfold(value))]


def offer(payload: object) -> Offer:
    """Classify the unauthenticated offer, and keep the addresses it rests on."""
    addresses = _addresses(last_header(payload, "List-Unsubscribe"))
    https = next(
        (
            item
            for item in addresses
            if item.casefold().startswith("https:") and len(item) <= _HTTPS_MAXIMUM
        ),
        "",
    )
    mailto = next((item for item in addresses if item.casefold().startswith("mailto:")), "")
    marked = unfold(last_header(payload, "List-Unsubscribe-Post")).strip() == ONE_CLICK_MARKER
    if https and marked:
        return Offer("one_click", https, mailto)
    if mailto:
        return Offer("mailto", https, mailto)
    if https:
        return Offer("link", https, mailto)
    return Offer(NO_OFFER, https, mailto)


def mailto_fields(uri: str) -> dict[str, str] | None:
    """The RFC 6068 message, or nothing at all when the address is not exactly closed.

    Anything wider than one recipient, one subject, and one body — a second
    address, `cc`, `bcc`, any other field, or a value carrying control text —
    normalizes to no `mailto` rather than to a trimmed one.
    """
    if not uri:
        return None
    path, _separator, query = uri.partition(":")[2].partition("?")
    try:
        recipient = unquote(path, errors="strict")
        fields: dict[str, str] = {}
        for item in query.split("&") if query else []:
            name, separator, value = item.partition("=")
            if not separator:
                return None
            field = unquote(name, errors="strict").casefold()
            if field not in {"subject", "body"} or field in fields:
                return None
            fields[field] = unquote(value, errors="strict")
    except UnicodeDecodeError:
        return None
    local, separator, domain = recipient.partition("@")
    if (
        not separator
        or not local
        or not domain
        or "@" in domain
        or len(recipient) > _RECIPIENT_MAXIMUM
        or _UNSAFE_RECIPIENT.search(recipient) is not None
    ):
        return None
    subject = fields.get("subject", "")
    body = fields.get("body", "")
    if len(subject) > _SUBJECT_MAXIMUM or _CONTROL.search(subject) is not None:
        return None
    if len(body) > _BODY_MAXIMUM or _CONTROL_BUT_NEWLINE.search(body) is not None:
        return None
    return {"to": recipient, "subject": subject, "body": body}


def _results(value: str) -> list[str]:
    """Split one `Authentication-Results` value, discarding comments and quoted text.

    A sender controls the text a relay copies into comments and quoted strings,
    including semicolons and a whole forged `dkim=pass`, so neither reaches the
    result list at all.
    """
    cleaned: list[str] = []
    depth = 0
    quoted = False
    escaped = False
    for char in unfold(value):
        if escaped:
            escaped = False
        elif char == "\\" and (quoted or depth):
            escaped = True
        elif quoted:
            if char == '"':
                quoted = False
                cleaned.append(" ")
        elif depth:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if not depth:
                    cleaned.append(" ")
        elif char == "(":
            depth = 1
        elif char == '"':
            quoted = True
        else:
            cleaned.append(char)
    return "".join(cleaned).split(";")


def _passing_domains(payload: object) -> set[str]:
    """The domains Gmail's own verdict reports DKIM passing for, and no others.

    Receiving servers prepend their headers, so Gmail's verdict is the first one
    claiming its identifier; a sender's forged copy always sits lower and a first
    verdict without a passing result is final.
    """
    for value in header_values(payload, "Authentication-Results"):
        results = _results(value)
        if results[0].strip().casefold() != GMAIL_AUTHSERV_ID:
            continue
        domains: set[str] = set()
        for result in results[1:]:
            match = _DKIM_PASS.match(result)
            if match is None:
                continue
            for token in result[match.end() :].split():
                name, _separator, identity = token.partition("=")
                if name.casefold() == "header.i":
                    domains.add(identity.rpartition("@")[2].casefold())
                elif name.casefold() == "header.d":
                    domains.add(identity.casefold())
        return domains
    return set()


def _signature_tags(value: str) -> dict[str, str]:
    """One `DKIM-Signature` tag list, with folding whitespace removed from each value."""
    tags: dict[str, str] = {}
    for item in unfold(value).split(";"):
        name, separator, raw = item.partition("=")
        if separator:
            tags.setdefault(name.strip().casefold(), _WHITESPACE.sub("", raw))
    return tags


def covered_headers(payload: object) -> list[str]:
    """The mechanism headers a signature Gmail verified as passing actually signs."""
    passing = _passing_domains(payload)
    if not passing:
        return []
    covered: set[str] = set()
    for value in header_values(payload, "DKIM-Signature"):
        tags = _signature_tags(value)
        domain = tags.get("d", "").casefold()
        # An agent user identifier below the signing domain is vouched for; a
        # signing subdomain is not vouched for by its parent.
        if not domain or not any(item == domain or item.endswith(f".{domain}") for item in passing):
            continue
        signed = {item.casefold() for item in tags.get("h", "").split(":")}
        covered |= signed & MECHANISM_HEADERS
    return sorted(covered)


def bulk(messages: list[Any]) -> dict[str, str]:
    """The newest received message's offer, which names no destination at all."""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        labels = message.get("labelIds")
        if isinstance(labels, list) and "SENT" in labels:
            continue
        payload = message.get("payload")
        return {
            "message_id": str(message.get("id", "")),
            "from": last_header(payload, "From"),
            "date": last_header(payload, "Date"),
            "list_id": normalize_list_id(last_header(payload, "List-Id")),
            "unsubscribe": offer(payload).offered,
        }
    return {"message_id": "", "from": "", "date": "", "list_id": "", "unsubscribe": NO_OFFER}
