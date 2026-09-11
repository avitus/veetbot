"""Versioned, bounded mailbox snapshots and resumable inline message bodies."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from gmail_mcp.constants import OUTPUT_MAXIMUM_BYTES, UPSTREAM_MAXIMUM_BYTES
from gmail_mcp.errors import GmailError, GmailResourceNotFoundError

if TYPE_CHECKING:
    from gmail_mcp.client import GmailClient

_HISTORY_ID = re.compile(r"[0-9]{1,20}\Z")
_HISTORY_CURSOR_PREFIX = "veetbot-history-v1:"


def _text(value: object, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise GmailError("gmail.provider_output_invalid")
    return value


def _history(value: object) -> str:
    text = _text(value, maximum=20)
    if _HISTORY_ID.fullmatch(text) is None:
        raise GmailError("gmail.provider_output_invalid")
    return text


def _labels(value: object) -> list[str]:
    if not isinstance(value, list):
        raise GmailError("gmail.provider_output_invalid")
    return [_text(item) for item in value]


def _bounded(value: dict[str, Any]) -> dict[str, Any]:
    if (
        len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        > OUTPUT_MAXIMUM_BYTES
    ):
        raise GmailError("gmail.provider_output_invalid")
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise GmailError("gmail.arguments_invalid")
    return value


def _inline_available(payload: object) -> bool:
    """Attachment-backed text and malformed encoded parts are never complete empty bodies."""
    if not isinstance(payload, dict):
        return False
    pending = [payload]
    while pending:
        part = pending.pop()
        children = part.get("parts", [])
        if not isinstance(children, list):
            return False
        pending.extend(item for item in children if isinstance(item, dict))
        if part.get("filename") or part.get("mimeType") not in {"text/plain", "text/html"}:
            continue
        body = part.get("body")
        if not isinstance(body, dict) or body.get("attachmentId"):
            return False
        encoded = body.get("data", "")
        if not isinstance(encoded, str):
            return False
        try:
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        except ValueError:
            return False
    return True


class GmailSync:
    """Read-only application contracts over the existing confined transport."""

    def __init__(self, client: GmailClient) -> None:
        self.client = client

    def _history_argument(self, value: str) -> str:
        value = self.client._required_text(value, "history_id", maximum=20)
        if _HISTORY_ID.fullmatch(value) is None:
            raise GmailError("gmail.arguments_invalid")
        return value

    def _history_position(
        self,
        token: str | None,
        start: str,
        limit: int,
    ) -> tuple[str | None, int, int, str | None]:
        if token is None:
            return None, limit, 0, None
        self.client._required_text(token, "page_token", maximum=65536)
        if not token.startswith(_HISTORY_CURSOR_PREFIX):
            self.client._required_text(token, "page_token", maximum=4096)
            return token, limit, 0, None
        encoded = token.removeprefix(_HISTORY_CURSOR_PREFIX)
        try:
            value = json.loads(
                base64.b64decode(
                    encoded + "=" * (-len(encoded) % 4),
                    altchars=b"-_",
                    validate=True,
                )
            )
        except (ValueError, UnicodeError) as exc:
            raise GmailError("gmail.arguments_invalid") from exc
        if (
            not isinstance(value, list)
            or len(value) != 7
            or value[:3] != [1, self.client.credential.account_id, start]
            or not isinstance(value[6], str)
            or re.fullmatch(r"[0-9a-f]{64}", value[6]) is None
        ):
            raise GmailError("gmail.arguments_invalid")
        page = value[3]
        if page is not None:
            self.client._required_text(page, "page_token", maximum=4096)
        return (
            page,
            _integer(value[4], 1, 100),
            _integer(value[5], 1, UPSTREAM_MAXIMUM_BYTES),
            value[6],
        )

    def _history_token(
        self,
        start: str,
        page: str | None,
        limit: int,
        offset: int,
        fingerprint: str,
    ) -> str:
        value = [1, self.client.credential.account_id, start, page, limit, offset, fingerprint]
        return _HISTORY_CURSOR_PREFIX + base64.urlsafe_b64encode(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    async def get_profile(self) -> dict[str, Any]:
        raw = await self.client._request("GET", "/profile")
        address = _text(raw.get("emailAddress"), maximum=320)
        if re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address) is None:
            raise GmailError("gmail.provider_output_invalid")
        counts = [raw.get("messagesTotal"), raw.get("threadsTotal")]
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts
        ):
            raise GmailError("gmail.provider_output_invalid")
        return {
            "schema_version": 1,
            "email_address": address,
            "verified_addresses": [address],
            "history_id": _history(raw.get("historyId")),
            "messages_total": counts[0],
            "threads_total": counts[1],
        }

    async def sync_changes(
        self,
        start_history_id: str,
        max_results: int,
        page_token: str | None,
    ) -> dict[str, Any]:
        start = self._history_argument(start_history_id)
        limit = _integer(max_results, 1, 100)
        provider_page, provider_limit, offset, expected_fingerprint = self._history_position(
            page_token,
            start,
            limit,
        )
        result: dict[str, Any] = {
            "schema_version": 1,
            "resync_required": False,
            "history_id": None,
            "next_page_token": None,
            "changes": [],
        }
        try:
            raw = await self.client._request(
                "GET",
                "/history",
                params={
                    "startHistoryId": start,
                    "maxResults": provider_limit,
                    **({"pageToken": provider_page} if provider_page is not None else {}),
                },
            )
        except GmailResourceNotFoundError:
            result["resync_required"] = True
            return result
        result["history_id"] = _history(raw.get("historyId"))
        if "nextPageToken" in raw:
            result["next_page_token"] = _text(raw["nextPageToken"], maximum=4096)
        histories = raw.get("history", [])
        if not isinstance(histories, list) or len(histories) > provider_limit:
            raise GmailError("gmail.provider_output_invalid")
        for history in histories:
            if not isinstance(history, dict):
                raise GmailError("gmail.provider_output_invalid")
            revision = _history(history.get("id"))
            for field, kind in (
                ("messagesAdded", "message_added"),
                ("messagesDeleted", "message_deleted"),
                ("labelsAdded", "labels_added"),
                ("labelsRemoved", "labels_removed"),
            ):
                changes = history.get(field, [])
                if not isinstance(changes, list):
                    raise GmailError("gmail.provider_output_invalid")
                for change in changes:
                    message = change.get("message") if isinstance(change, dict) else None
                    if not isinstance(message, dict):
                        raise GmailError("gmail.provider_output_invalid")
                    result["changes"].append(
                        {
                            "history_id": revision,
                            "kind": kind,
                            "message_id": _text(message.get("id")),
                            "thread_id": _text(message.get("threadId")),
                            "label_ids": _labels(
                                change.get("labelIds", message.get("labelIds", []))
                            ),
                        }
                    )
        normalized = result["changes"]
        provider_next = result["next_page_token"]
        fingerprint = hashlib.sha256(
            json.dumps(
                [normalized, provider_next],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if expected_fingerprint is not None and (
            expected_fingerprint != fingerprint or offset >= len(normalized)
        ):
            return {
                **result,
                "changes": [],
                "history_id": None,
                "next_page_token": None,
                "resync_required": True,
            }

        def page(count: int) -> dict[str, Any]:
            end = offset + count
            continuation = (
                self._history_token(start, provider_page, provider_limit, end, fingerprint)
                if end < len(normalized)
                else provider_next
            )
            return {**result, "changes": normalized[offset:end], "next_page_token": continuation}

        count = min(limit, len(normalized) - offset)
        candidate = page(count)
        # Google limits history records, not the number or byte size of their
        # nested changes. A local cursor resumes the same verified provider page.
        if (
            len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode())
            > OUTPUT_MAXIMUM_BYTES
        ):
            low, high = 0, count
            while low < high:
                middle = (low + high + 1) // 2
                if (
                    len(
                        json.dumps(page(middle), ensure_ascii=False, separators=(",", ":")).encode()
                    )
                    <= OUTPUT_MAXIMUM_BYTES
                ):
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise GmailError("gmail.provider_output_invalid")
            candidate = page(low)
        return _bounded(candidate)

    async def _thread_index(self, thread_id: str) -> tuple[str, list[str]]:
        raw = await self.client._request(
            "GET", f"/threads/{quote(thread_id, safe='')}", params={"format": "minimal"}
        )
        if raw.get("id") != thread_id or not isinstance(raw.get("messages"), list):
            raise GmailError("gmail.provider_output_invalid")
        identifiers = [
            _text(item.get("id")) if isinstance(item, dict) else _text(None)
            for item in raw["messages"]
        ]
        if len(set(identifiers)) != len(identifiers):
            raise GmailError("gmail.provider_output_invalid")
        return _history(raw.get("historyId")), identifiers

    def _page_token(self, thread_id: str, history_id: str, offset: int) -> str:
        data = [1, self.client.credential.account_id, thread_id, history_id, offset]
        return (
            base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )

    def _page_position(self, token: str, thread_id: str) -> tuple[str, int]:
        self.client._required_text(token, "page_token", maximum=4096)
        try:
            value = json.loads(
                base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            )
        except (ValueError, UnicodeError) as exc:
            raise GmailError("gmail.arguments_invalid") from exc
        if (
            not isinstance(value, list)
            or len(value) != 5
            or value[:3] != [1, self.client.credential.account_id, thread_id]
        ):
            raise GmailError("gmail.arguments_invalid")
        return self._history_argument(value[3]), _integer(value[4], 0, UPSTREAM_MAXIMUM_BYTES)

    def _message(self, raw: dict[str, Any], thread_id: str) -> dict[str, Any]:
        if raw.get("threadId") != thread_id:
            raise GmailError("gmail.provider_output_invalid")
        _text(raw.get("id"))
        labels = _labels(raw.get("labelIds", []))
        normalized = self.client._message(raw, body_budget=65536)
        headers = self.client._headers(raw.get("payload"))
        payload = raw.get("payload")
        header_rows = payload.get("headers", []) if isinstance(payload, dict) else []
        headers_complete = isinstance(header_rows, list) and all(
            isinstance(item, dict)
            and isinstance(item.get("value"), str)
            and len(item["value"]) <= 8192
            for item in header_rows
        )
        full_body, _attachments = self.client._payload_content(raw.get("payload"))
        body = str(normalized["body"])
        available = _inline_available(raw.get("payload"))
        normalized.update(
            {
                "history_id": _history(raw.get("historyId")),
                "internal_date": _history(raw.get("internalDate")),
                "headers_complete": headers_complete,
                "reply_to": headers.get("reply-to", ""),
                "message_id_header": headers.get("message-id", ""),
                "in_reply_to": headers.get("in-reply-to", ""),
                "references": headers.get("references", ""),
                "direction": "sent" if "SENT" in labels else "received",
                "body_complete": available and body == full_body,
                "body_available": available,
                "next_body_offset": len(body.encode()) if available and body != full_body else None,
            }
        )
        return normalized

    async def get_thread_page(
        self,
        thread_id: str,
        max_messages: int,
        page_token: str | None,
    ) -> dict[str, Any]:
        thread_id = self.client._required_text(thread_id, "thread_id", maximum=1024)
        limit = _integer(max_messages, 1, 10)
        position = self._page_position(page_token, thread_id) if page_token is not None else None
        revision, identifiers = await self._thread_index(thread_id)
        result: dict[str, Any] = {
            "schema_version": 1,
            "thread_id": thread_id,
            "history_id": revision,
            "total_messages": len(identifiers),
            "returned_messages": 0,
            "messages": [],
            "next_page_token": None,
            "complete": False,
            "source_changed": False,
        }
        if position is not None and position[0] != revision:
            result["source_changed"] = True
            return result
        offset = position[1] if position else 0
        if offset > len(identifiers):
            raise GmailError("gmail.arguments_invalid")
        semaphore = asyncio.Semaphore(5)

        async def fetch(identifier: str) -> dict[str, Any]:
            async with semaphore:
                raw = await self.client._request(
                    "GET", f"/messages/{quote(identifier, safe='')}", params={"format": "full"}
                )
                if raw.get("id") != identifier:
                    raise GmailError("gmail.provider_output_invalid")
                return self._message(raw, thread_id)

        tasks = [asyncio.create_task(fetch(item)) for item in identifiers[offset : offset + limit]]
        try:
            async with asyncio.timeout(30):
                messages = await asyncio.gather(*tasks)
                latest, _ids = await self._thread_index(thread_id)
        except BaseException as exc:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(exc, TimeoutError):
                raise GmailError("gmail.provider_unavailable") from exc
            raise
        if latest != revision or any(int(item["history_id"]) > int(revision) for item in messages):
            result.update({"history_id": latest, "source_changed": True})
            return result
        for message in messages:
            trial = {**result, "messages": [*result["messages"], message]}
            # Reserve room for the continuation token and count fields.
            if len(json.dumps(trial, ensure_ascii=False).encode()) > OUTPUT_MAXIMUM_BYTES - 8192:
                break
            result["messages"].append(message)
        count = len(result["messages"])
        if messages and not count:
            raise GmailError("gmail.provider_output_invalid")
        next_offset = offset + count
        result["returned_messages"] = count
        result["complete"] = next_offset == len(identifiers) and all(
            item["body_complete"] and item["headers_complete"] for item in result["messages"]
        )
        if next_offset < len(identifiers):
            result["next_page_token"] = self._page_token(thread_id, revision, next_offset)
        return _bounded(result)

    async def get_message_body(
        self,
        message_id: str,
        offset: int,
        max_bytes: int,
        expected_history_id: str | None,
    ) -> dict[str, Any]:
        message_id = self.client._required_text(message_id, "message_id", maximum=1024)
        offset = _integer(offset, 0, UPSTREAM_MAXIMUM_BYTES)
        maximum = _integer(max_bytes, 1024, 65536)
        if expected_history_id is not None:
            self._history_argument(expected_history_id)
        elif offset:
            raise GmailError("gmail.arguments_invalid")
        raw = await self.client._request(
            "GET", f"/messages/{quote(message_id, safe='')}", params={"format": "full"}
        )
        if raw.get("id") != message_id:
            raise GmailError("gmail.provider_output_invalid")
        revision = _history(raw.get("historyId"))
        result: dict[str, Any] = {
            "schema_version": 1,
            "message_id": message_id,
            "history_id": revision,
            "body": "",
            "offset": offset,
            "next_offset": None,
            "complete": False,
            "source_changed": False,
            "body_available": False,
        }
        if expected_history_id is not None and revision != expected_history_id:
            result["source_changed"] = True
            return result
        if not _inline_available(raw.get("payload")):
            return result
        body, _attachments = self.client._payload_content(raw.get("payload"))
        encoded = body.encode()
        if offset > len(encoded):
            raise GmailError("gmail.arguments_invalid")
        try:
            encoded[:offset].decode()
        except UnicodeDecodeError as exc:
            raise GmailError("gmail.arguments_invalid") from exc
        chunk = encoded[offset : offset + maximum].decode(errors="ignore")
        next_offset = offset + len(chunk.encode())
        result.update(
            {
                "body": chunk,
                "body_available": True,
                "complete": next_offset == len(encoded),
                "next_offset": next_offset if next_offset < len(encoded) else None,
            }
        )
        return _bounded(result)
