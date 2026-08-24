"""In-memory fake of the Gmail REST API for the gmail_mcp contract suite.

The fake renders real Gmail wire shapes — ``threads.list``, ``threads.get``
in both ``metadata`` and ``full`` formats, ``labels.list`` — from a small
internal mailbox, enforces bearer authentication, and lets a test force the
next response to fail in a chosen way. Upstream bodies carry loud marker
strings so leakage assertions can prove no upstream text crosses the pipe.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

ACCESS_TOKEN = "fake-access-token-3f1c"
UPSTREAM_ERROR_MARKER = "UPSTREAM-PRIVATE-DETAIL"


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@dataclass
class FakeMessage:
    message_id: str
    sender: str
    to: str
    subject: str
    date: str
    label_ids: tuple[str, ...] = ("INBOX", "UNREAD")
    body_text: str | None = None
    body_html: str | None = None
    attachments: tuple[tuple[str, str, int], ...] = ()  # (filename, mime type, size)

    def headers(self) -> list[dict[str, str]]:
        return [
            {"name": "From", "value": self.sender},
            {"name": "To", "value": self.to},
            {"name": "Subject", "value": self.subject},
            {"name": "Date", "value": self.date},
        ]

    def searchable_text(self) -> str:
        return " ".join(
            filter(None, (self.sender, self.to, self.subject, self.body_text, self.body_html))
        ).lower()

    def payload(self, *, full: bool) -> dict[str, object]:
        if not full:
            return {"mimeType": "multipart/mixed", "headers": self.headers()}
        parts: list[dict[str, object]] = []
        if self.body_text is not None:
            parts.append(
                {
                    "mimeType": "text/plain",
                    "headers": [],
                    "body": {"size": len(self.body_text), "data": _b64url(self.body_text)},
                }
            )
        if self.body_html is not None:
            parts.append(
                {
                    "mimeType": "text/html",
                    "headers": [],
                    "body": {"size": len(self.body_html), "data": _b64url(self.body_html)},
                }
            )
        for filename, mime_type, size in self.attachments:
            parts.append(
                {
                    "mimeType": mime_type,
                    "filename": filename,
                    "headers": [],
                    "body": {"size": size, "attachmentId": f"attachment-{filename}"},
                }
            )
        return {"mimeType": "multipart/mixed", "headers": self.headers(), "parts": parts}

    def wire(self, *, full: bool) -> dict[str, object]:
        return {
            "id": self.message_id,
            "labelIds": list(self.label_ids),
            "snippet": (self.body_text or "")[:80],
            "payload": self.payload(full=full),
        }


@dataclass
class FakeGmail:
    """The mailbox, the wire renderer, and the failure injector in one."""

    token: str = ACCESS_TOKEN
    threads: dict[str, list[FakeMessage]] = field(default_factory=dict)
    labels: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "UNREAD", "name": "UNREAD", "type": "system"},
            {"id": "Label_7", "name": "receipts", "type": "user"},
        ]
    )
    forced: list[httpx.Response] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    drafts: dict[str, dict[str, object]] = field(default_factory=dict)
    sent: list[dict[str, object]] = field(default_factory=list)

    def seed_thread(self, thread_id: str, messages: list[FakeMessage]) -> None:
        self.threads[thread_id] = messages

    def force(self, response: httpx.Response) -> None:
        """Queue a response returned verbatim for the next request."""

        self.forced.append(response)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.forced:
            return self.forced.pop(0)
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(
                401, json={"error": {"message": f"{UPSTREAM_ERROR_MARKER} bad bearer"}}
            )
        path = request.url.path
        query = parse_qs(request.url.query.decode())
        if request.method == "GET" and path == "/gmail/v1/users/me/labels":
            return httpx.Response(200, json={"labels": self.labels})
        if request.method == "GET" and path == "/gmail/v1/users/me/threads":
            return self._list_threads(query)
        if request.method == "POST" and path == "/gmail/v1/users/me/drafts":
            return self._create_draft(request)
        if request.method == "POST" and path == "/gmail/v1/users/me/messages/send":
            return self._send_message(request)
        if request.method == "POST" and path.endswith(("/modify", "/trash", "/untrash")):
            _, thread_id, action = path.rsplit("/", 2)
            return self._thread_action(thread_id, action, request)
        if request.method == "GET" and path.startswith("/gmail/v1/users/me/threads/"):
            return self._get_thread(path.rsplit("/", 1)[1], query)
        return httpx.Response(
            404, json={"error": {"message": f"{UPSTREAM_ERROR_MARKER} no route {path}"}}
        )

    @staticmethod
    def _decode_raw(raw: str) -> str:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded).decode(errors="replace")

    def _create_draft(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        message = body.get("message", {})
        draft_id = f"draft-{len(self.drafts) + 1}"
        self.drafts[draft_id] = {
            "mime": self._decode_raw(str(message.get("raw", ""))),
            "thread_id": message.get("threadId"),
        }
        return httpx.Response(200, json={"id": draft_id, "message": {"id": f"message-{draft_id}"}})

    def _send_message(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        thread_id = body.get("threadId")
        message_id = f"message-sent-{len(self.sent) + 1}"
        self.sent.append(
            {"mime": self._decode_raw(str(body.get("raw", ""))), "thread_id": thread_id}
        )
        return httpx.Response(
            200,
            json={"id": message_id, "threadId": thread_id or f"thread-{message_id}"},
        )

    def _thread_action(self, thread_id: str, action: str, request: httpx.Request) -> httpx.Response:
        messages = self.threads.get(thread_id)
        if messages is None:
            return httpx.Response(
                404, json={"error": {"message": f"{UPSTREAM_ERROR_MARKER} unknown {thread_id}"}}
            )
        if action == "modify":
            body = json.loads(request.read().decode())
            add = [str(label) for label in body.get("addLabelIds", [])]
            remove = {str(label) for label in body.get("removeLabelIds", [])}
        elif action == "trash":
            add, remove = ["TRASH"], {"INBOX"}
        else:
            add, remove = ["INBOX"], {"TRASH"}
        for message in messages:
            labels = [label for label in message.label_ids if label not in remove]
            labels.extend(label for label in add if label not in labels)
            message.label_ids = tuple(labels)
        return httpx.Response(200, json={"id": thread_id})

    def _matches(self, messages: list[FakeMessage], q: str) -> bool:
        haystack = " ".join(message.searchable_text() for message in messages)
        return all(token.lower() in haystack for token in q.split())

    def _list_threads(self, query: dict[str, list[str]]) -> httpx.Response:
        q = query.get("q", [""])[0]
        limit = int(query.get("maxResults", ["10"])[0])
        offset = int(query.get("pageToken", ["0"])[0])
        matches = [
            (thread_id, messages)
            for thread_id, messages in sorted(self.threads.items())
            if not q or self._matches(messages, q)
        ]
        page = matches[offset : offset + limit]
        body: dict[str, object] = {
            "threads": [
                {"id": thread_id, "snippet": (messages[0].body_text or "")[:80]}
                for thread_id, messages in page
            ],
            "resultSizeEstimate": len(matches),
        }
        if offset + limit < len(matches):
            body["nextPageToken"] = str(offset + limit)
        return httpx.Response(200, json=body)

    def _get_thread(self, thread_id: str, query: dict[str, list[str]]) -> httpx.Response:
        messages = self.threads.get(thread_id)
        if messages is None:
            return httpx.Response(
                404, json={"error": {"message": f"{UPSTREAM_ERROR_MARKER} unknown {thread_id}"}}
            )
        full = query.get("format", ["full"])[0] == "full"
        return httpx.Response(
            200,
            json={
                "id": thread_id,
                "messages": [message.wire(full=full) for message in messages],
            },
        )


def seeded_fake() -> FakeGmail:
    fake = FakeGmail()
    fake.seed_thread(
        "thread-lunch",
        [
            FakeMessage(
                message_id="message-1",
                sender="Ada Lovelace <ada@example.org>",
                to="owner@example.com",
                subject="Lunch on Thursday?",
                date="Mon, 24 Aug 2026 09:00:00 +0000",
                body_text="Shall we get lunch at noon on Thursday?",
            ),
            FakeMessage(
                message_id="message-2",
                sender="owner@example.com",
                to="Ada Lovelace <ada@example.org>",
                subject="Re: Lunch on Thursday?",
                date="Mon, 24 Aug 2026 09:05:00 +0000",
                label_ids=("SENT",),
                body_text="Noon works. See you there.",
            ),
        ],
    )
    fake.seed_thread(
        "thread-receipt",
        [
            FakeMessage(
                message_id="message-3",
                sender="Store <billing@example.net>",
                to="owner@example.com",
                subject="Your receipt",
                date="Sun, 23 Aug 2026 18:30:00 +0000",
                label_ids=("INBOX",),
                body_html="<html><body><p>Total: <b>$42.00</b></p></body></html>",
                attachments=(("receipt.pdf", "application/pdf", 52_133),),
            )
        ],
    )
    return fake
