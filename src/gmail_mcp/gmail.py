"""The Gmail REST client: bearer calls, bounds, and stable failure mapping.

Every upstream failure leaves this module as a :class:`GmailServerError`
whose string form is one closed, content-free code. Google's response text,
headers, and token material never escape.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from html.parser import HTMLParser
from typing import Protocol

import httpx

from gmail_mcp.errors import (
    CREDENTIAL_REJECTED,
    INVALID_OUTPUT,
    RATE_LIMITED,
    REJECTED,
    UNAVAILABLE,
    GmailServerError,
)

MESSAGE_BODY_CHARACTER_BUDGET = 20_000
RESPONSE_BYTE_CEILING = 2 * 1024 * 1024
MAXIMUM_SEARCH_RESULTS = 25

_BLOCK_TAGS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"})


class TokenSource(Protocol):
    """Supplies a bearer access token, refreshing behind the seam if needed."""

    async def access_token(self) -> str: ...


class StaticTokenSource:
    """A fixed access token, for tests and short-lived tooling."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def access_token(self) -> str:
        return self._token


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._suppressed += 1
        if tag in _BLOCK_TAGS:
            self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._suppressed:
            self._suppressed -= 1
        if tag in _BLOCK_TAGS:
            self._pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self._pieces.append(data)

    def text(self) -> str:
        raw = "".join(self._pieces)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _html_to_text(markup: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(markup)
    return extractor.text()


def _decode_body(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        raise GmailServerError(INVALID_OUTPUT) from None


def _header(headers: object, name: str) -> str:
    if isinstance(headers, list):
        for entry in headers:
            if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name.lower():
                return str(entry.get("value", ""))
    return ""


def _walk_parts(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    parts = payload.get("parts")
    if isinstance(parts, list):
        found: list[dict[str, object]] = []
        for part in parts:
            found.extend(_walk_parts(part))
        return found
    return [payload]


def _message_body(payload: object) -> str:
    plain: list[str] = []
    html: list[str] = []
    for part in _walk_parts(payload):
        if str(part.get("filename", "")):
            continue
        body = part.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, str):
            continue
        mime_type = str(part.get("mimeType", ""))
        if mime_type == "text/plain":
            plain.append(_decode_body(data))
        elif mime_type == "text/html":
            html.append(_decode_body(data))
    text = "\n".join(plain) if plain else _html_to_text("\n".join(html))
    return text[:MESSAGE_BODY_CHARACTER_BUDGET]


def _message_attachments(payload: object) -> list[dict[str, object]]:
    attachments: list[dict[str, object]] = []
    for part in _walk_parts(payload):
        filename = str(part.get("filename", ""))
        if not filename:
            continue
        body = part.get("body")
        size = body.get("size") if isinstance(body, dict) else None
        attachments.append(
            {
                "filename": filename,
                "mime_type": str(part.get("mimeType", "")),
                "size": size if isinstance(size, int) else 0,
            }
        )
    return attachments


def _label_ids(message: dict[str, object]) -> list[str]:
    labels = message.get("labelIds")
    if isinstance(labels, list):
        return [str(label) for label in labels]
    return []


class GmailClient:
    """Typed access to the Gmail endpoints the rosters need."""

    def __init__(self, *, http: httpx.AsyncClient, token_source: TokenSource) -> None:
        self._http = http
        self._token_source = token_source

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        token = await self._token_source.access_token()
        try:
            response = await self._http.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            raise GmailServerError(UNAVAILABLE) from None
        if 300 <= response.status_code < 400:
            # A redirect is refused, never followed: a credentialed call
            # must not be walked to another host.
            raise GmailServerError(REJECTED)
        if response.status_code == 401:
            raise GmailServerError(CREDENTIAL_REJECTED)
        if response.status_code == 429:
            raise GmailServerError(RATE_LIMITED)
        if response.status_code >= 500:
            raise GmailServerError(UNAVAILABLE)
        if response.status_code >= 400:
            raise GmailServerError(REJECTED)
        content = response.content
        if len(content) > RESPONSE_BYTE_CEILING:
            raise GmailServerError(INVALID_OUTPUT)
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise GmailServerError(INVALID_OUTPUT) from None
        if not isinstance(payload, dict):
            raise GmailServerError(INVALID_OUTPUT)
        return payload

    async def _thread_metadata(self, thread_id: str) -> dict[str, object]:
        return await self._request_json(
            "GET",
            f"/gmail/v1/users/me/threads/{thread_id}",
            params={"format": "metadata"},
        )

    async def search_threads(
        self, query: str, max_results: int = 10, page_token: str | None = None
    ) -> dict[str, object]:
        if not 1 <= max_results <= MAXIMUM_SEARCH_RESULTS:
            raise GmailServerError(REJECTED)
        params = {"maxResults": str(max_results)}
        if query:
            params["q"] = query
        if page_token is not None:
            params["pageToken"] = page_token
        listing = await self._request_json("GET", "/gmail/v1/users/me/threads", params=params)
        listed = listing.get("threads")
        records: list[dict[str, object]] = []
        for stub in listed if isinstance(listed, list) else []:
            if not isinstance(stub, dict) or not isinstance(stub.get("id"), str):
                raise GmailServerError(INVALID_OUTPUT)
            thread_id = str(stub["id"])
            thread = await self._thread_metadata(thread_id)
            messages = thread.get("messages")
            if not isinstance(messages, list) or not messages:
                raise GmailServerError(INVALID_OUTPUT)
            first = messages[0]
            if not isinstance(first, dict):
                raise GmailServerError(INVALID_OUTPUT)
            senders: list[str] = []
            labels: list[str] = []
            for message in messages:
                if not isinstance(message, dict):
                    raise GmailServerError(INVALID_OUTPUT)
                payload = message.get("payload")
                headers = payload.get("headers") if isinstance(payload, dict) else None
                sender = _header(headers, "From")
                if sender and sender not in senders:
                    senders.append(sender)
                for label in _label_ids(message):
                    if label not in labels:
                        labels.append(label)
            first_payload = first.get("payload")
            first_headers = (
                first_payload.get("headers") if isinstance(first_payload, dict) else None
            )
            records.append(
                {
                    "thread_id": thread_id,
                    "subject": _header(first_headers, "Subject"),
                    "senders": senders,
                    "date": _header(first_headers, "Date"),
                    "snippet": str(stub.get("snippet", "")),
                    "label_ids": labels,
                }
            )
        next_token = listing.get("nextPageToken")
        return {
            "threads": records,
            "next_page_token": next_token if isinstance(next_token, str) else None,
        }

    async def get_thread(self, thread_id: str) -> dict[str, object]:
        thread = await self._request_json(
            "GET",
            f"/gmail/v1/users/me/threads/{thread_id}",
            params={"format": "full"},
        )
        raw_messages = thread.get("messages")
        if not isinstance(raw_messages, list):
            raise GmailServerError(INVALID_OUTPUT)
        messages: list[dict[str, object]] = []
        for message in raw_messages:
            if not isinstance(message, dict):
                raise GmailServerError(INVALID_OUTPUT)
            payload = message.get("payload")
            headers = payload.get("headers") if isinstance(payload, dict) else None
            messages.append(
                {
                    "message_id": str(message.get("id", "")),
                    "sender": _header(headers, "From"),
                    "to": _header(headers, "To"),
                    "subject": _header(headers, "Subject"),
                    "date": _header(headers, "Date"),
                    "label_ids": _label_ids(message),
                    "body": _message_body(payload),
                    "attachments": _message_attachments(payload),
                }
            )
        return {"thread_id": str(thread.get("id", thread_id)), "messages": messages}

    async def list_labels(self) -> dict[str, object]:
        listing = await self._request_json("GET", "/gmail/v1/users/me/labels")
        raw = listing.get("labels")
        if not isinstance(raw, list):
            raise GmailServerError(INVALID_OUTPUT)
        labels = []
        for label in raw:
            if not isinstance(label, dict):
                raise GmailServerError(INVALID_OUTPUT)
            labels.append(
                {
                    "id": str(label.get("id", "")),
                    "name": str(label.get("name", "")),
                    "type": str(label.get("type", "")),
                }
            )
        return {"labels": labels}
