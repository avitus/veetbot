"""Bounded Gmail REST client owned entirely by the first-party MCP package."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import httpx

from gmail_mcp.constants import (
    GMAIL_API_ROOT,
    GOOGLE_TOKEN_ENDPOINT,
    OUTPUT_MAXIMUM_BYTES,
    UPSTREAM_MAXIMUM_BYTES,
)
from gmail_mcp.errors import GmailError

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_ACCOUNT_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_THREAD_FANOUT_CONCURRENCY = 5
_THREAD_FANOUT_TIMEOUT_SECONDS = 30.0
type _QueryScalar = str | int | float | bool | None
type _QueryValue = _QueryScalar | list[_QueryScalar]


@dataclass(frozen=True, slots=True)
class GmailCredential:
    """The opaque credential document supplied to exactly one server mode."""

    client_id: str
    client_secret: str
    refresh_token: str
    scope: str
    account_id: str | None = None

    @classmethod
    def parse(
        cls,
        value: str,
        *,
        expected_scope: str,
        expected_account_id: str | None = None,
    ) -> GmailCredential:
        try:
            loaded: object = json.loads(value)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise GmailError("gmail.credential_rejected") from exc
        expected_fields = {
            "client_id",
            "client_secret",
            "refresh_token",
            "scope",
        }
        if expected_account_id is not None:
            expected_fields.add("account_id")
        if not isinstance(loaded, dict) or set(loaded) != expected_fields:
            raise GmailError("gmail.credential_rejected")
        client_id = loaded.get("client_id")
        client_secret = loaded.get("client_secret")
        refresh_token = loaded.get("refresh_token")
        fields = (client_id, client_secret, refresh_token)
        if any(not isinstance(field, str) or not field or len(field) > 4096 for field in fields):
            raise GmailError("gmail.credential_rejected")
        assert isinstance(client_id, str)
        assert isinstance(client_secret, str)
        assert isinstance(refresh_token, str)
        scope = loaded.get("scope")
        if scope != expected_scope:
            raise GmailError("gmail.credential_rejected")
        account_id = loaded.get("account_id")
        if expected_account_id is not None and (
            _ACCOUNT_ID.fullmatch(expected_account_id) is None or account_id != expected_account_id
        ):
            raise GmailError("gmail.credential_rejected")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            scope=expected_scope,
            account_id=expected_account_id,
        )

    def as_json(self) -> str:
        document = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
        }
        if self.account_id is not None:
            document["account_id"] = self.account_id
        return json.dumps(document, sort_keys=True, separators=(",", ":"))


class GmailClient:
    """A no-redirect Gmail client whose public failures contain stable codes only."""

    def __init__(
        self,
        credential: GmailCredential,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.credential = credential
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
        )
        self._owns_http_client = http_client is None
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    async def authenticate(self) -> None:
        await self._token()

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def _token(self) -> str:
        if (
            self._access_token is not None
            and self._access_token_expires_at > time.monotonic() + 30.0
        ):
            return self._access_token
        try:
            response = await self._http_client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "client_id": self.credential.client_id,
                    "client_secret": self.credential.client_secret,
                    "refresh_token": self.credential.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise GmailError("gmail.provider_unavailable") from exc
        if 300 <= response.status_code < 400:
            raise GmailError("gmail.provider_rejected")
        if response.status_code in {400, 401, 403}:
            raise GmailError("gmail.credential_rejected")
        if response.status_code == 429:
            raise GmailError("gmail.rate_limited")
        if response.status_code >= 500:
            raise GmailError("gmail.provider_unavailable")
        if response.status_code >= 400 or len(response.content) > UPSTREAM_MAXIMUM_BYTES:
            raise GmailError("gmail.provider_rejected")
        try:
            payload: object = response.json()
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise GmailError("gmail.provider_output_invalid") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        expires_in = payload.get("expires_in", 3600) if isinstance(payload, dict) else None
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(expires_in, (int, float))
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise GmailError("gmail.provider_output_invalid")
        self._access_token = token
        self._access_token_expires_at = time.monotonic() + float(expires_in)
        return token

    async def _read_body(self, response: httpx.Response, *, mutating: bool) -> bytes:
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > UPSTREAM_MAXIMUM_BYTES:
                    code = "gmail.outcome_unknown" if mutating else "gmail.provider_output_invalid"
                    raise GmailError(code)
        finally:
            await response.aclose()
        return bytes(body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, _QueryValue] | None = None,
        body: dict[str, object] | None = None,
        mutating: bool = False,
        allow_reauthentication: bool = True,
    ) -> dict[str, Any]:
        token = await self._token()
        request = self._http_client.build_request(
            method,
            f"{GMAIL_API_ROOT}{path}",
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            response = await self._http_client.send(request, stream=True)
        except httpx.HTTPError as exc:
            code = "gmail.outcome_unknown" if mutating else "gmail.provider_unavailable"
            raise GmailError(code) from exc
        status = response.status_code
        raw = await self._read_body(response, mutating=mutating)
        if status == 401:
            if mutating:
                raise GmailError("gmail.outcome_unknown")
            if allow_reauthentication:
                self._access_token = None
                self._access_token_expires_at = 0.0
                return await self._request(
                    method,
                    path,
                    params=params,
                    body=body,
                    mutating=False,
                    allow_reauthentication=False,
                )
            raise GmailError("gmail.credential_rejected")
        if status == 429:
            raise GmailError("gmail.outcome_unknown" if mutating else "gmail.rate_limited")
        if status >= 500:
            raise GmailError("gmail.outcome_unknown" if mutating else "gmail.provider_unavailable")
        if 300 <= status < 400 or status >= 400:
            raise GmailError("gmail.provider_rejected")
        try:
            payload: object = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            code = "gmail.outcome_unknown" if mutating else "gmail.provider_output_invalid"
            raise GmailError(code) from exc
        if not isinstance(payload, dict):
            code = "gmail.outcome_unknown" if mutating else "gmail.provider_output_invalid"
            raise GmailError(code)
        return payload

    @staticmethod
    def _required_text(value: object, name: str, *, maximum: int = 4096) -> str:
        del name
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise GmailError("gmail.arguments_invalid")
        if "\x00" in value:
            raise GmailError("gmail.arguments_invalid")
        return value

    @classmethod
    def _required_header(cls, value: object, name: str, *, maximum: int) -> str:
        normalized = cls._required_text(value, name, maximum=maximum)
        if "\r" in normalized or "\n" in normalized:
            raise GmailError("gmail.arguments_invalid")
        return normalized

    @staticmethod
    def _decode_body(value: object) -> str:
        if not isinstance(value, str) or not value:
            return ""
        try:
            padded = value + "=" * (-len(value) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        except (ValueError, UnicodeError):
            return ""

    @classmethod
    def _payload_content(cls, payload: object) -> tuple[str, list[dict[str, object]]]:
        if not isinstance(payload, dict):
            return "", []
        plain: list[str] = []
        rendered_html: list[str] = []
        attachments: list[dict[str, object]] = []
        stack: list[dict[str, object]] = [payload]
        while stack:
            part = stack.pop()
            nested = part.get("parts")
            if isinstance(nested, list):
                stack.extend(item for item in reversed(nested) if isinstance(item, dict))
            mime_type = part.get("mimeType")
            filename = part.get("filename")
            body = part.get("body")
            if isinstance(filename, str) and filename:
                size = body.get("size", 0) if isinstance(body, dict) else 0
                attachments.append(
                    {
                        "filename": filename[:1024],
                        "mime_type": mime_type if isinstance(mime_type, str) else "",
                        "size": size if isinstance(size, int) and size >= 0 else 0,
                    }
                )
                continue
            data = body.get("data") if isinstance(body, dict) else None
            decoded = cls._decode_body(data)
            if mime_type == "text/plain" and decoded:
                plain.append(decoded)
            elif mime_type == "text/html" and decoded:
                stripped = _HTML_TAG.sub(" ", html.unescape(decoded))
                rendered_html.append(_WHITESPACE.sub(" ", stripped).strip())
        return "\n".join(plain or rendered_html), attachments

    @staticmethod
    def _headers(payload: object) -> dict[str, str]:
        if not isinstance(payload, dict) or not isinstance(payload.get("headers"), list):
            return {}
        result: dict[str, str] = {}
        for item in payload["headers"]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if isinstance(name, str) and isinstance(value, str):
                result[name.casefold()] = value[:8192]
        return result

    @classmethod
    def _message(cls, value: object, *, body_budget: int) -> dict[str, object]:
        if not isinstance(value, dict):
            raise GmailError("gmail.provider_output_invalid")
        payload = value.get("payload")
        headers = cls._headers(payload)
        body, attachments = cls._payload_content(payload)
        body = body.encode("utf-8")[:body_budget].decode("utf-8", errors="ignore")
        label_ids = value.get("labelIds")
        return {
            "id": str(value.get("id", "")),
            "thread_id": str(value.get("threadId", "")),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "bcc": headers.get("bcc", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "body": body,
            "label_ids": [str(item) for item in label_ids] if isinstance(label_ids, list) else [],
            "attachments": attachments,
        }

    @staticmethod
    def _bounded(payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) <= OUTPUT_MAXIMUM_BYTES:
            return payload
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise GmailError("gmail.provider_output_invalid")
        bounded: dict[str, Any] = {
            key: value for key, value in payload.items() if key != "messages"
        }
        bounded["messages"] = []
        bounded["truncated"] = True
        for message in messages:
            candidate = {**bounded, "messages": [*bounded["messages"], message]}
            if len(json.dumps(candidate, ensure_ascii=False).encode()) > OUTPUT_MAXIMUM_BYTES:
                break
            bounded["messages"].append(message)
        return bounded

    async def search_threads(
        self,
        query: str,
        max_results: int,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        self._required_text(query, "query", maximum=4096)
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 25
        ):
            raise GmailError("gmail.arguments_invalid")
        if page_token is not None:
            self._required_text(page_token, "page_token", maximum=4096)
        payload = await self._request(
            "GET",
            "/threads",
            params={
                "q": query,
                "maxResults": max_results,
                **({"pageToken": page_token} if page_token is not None else {}),
            },
        )
        declarations = payload.get("threads", [])
        if not isinstance(declarations, list):
            raise GmailError("gmail.provider_output_invalid")
        identifiers: list[str] = []
        for declaration in declarations[:max_results]:
            thread_id = declaration.get("id") if isinstance(declaration, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise GmailError("gmail.provider_output_invalid")
            identifiers.append(thread_id)

        semaphore = asyncio.Semaphore(_THREAD_FANOUT_CONCURRENCY)

        async def fetch_detail(thread_id: str) -> dict[str, Any]:
            async with semaphore:
                return await self._request(
                    "GET",
                    f"/threads/{quote(thread_id, safe='')}",
                    params={
                        "format": "metadata",
                        "metadataHeaders": ["From", "Subject", "Date"],
                    },
                )

        tasks = [asyncio.create_task(fetch_detail(thread_id)) for thread_id in identifiers]
        try:
            async with asyncio.timeout(_THREAD_FANOUT_TIMEOUT_SECONDS):
                details = await asyncio.gather(*tasks)
        except TimeoutError as exc:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise GmailError("gmail.provider_unavailable") from exc
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        threads: list[dict[str, object]] = []
        for thread_id, detail in zip(identifiers, details, strict=True):
            messages = detail.get("messages")
            if not isinstance(messages, list) or not messages:
                raise GmailError("gmail.provider_output_invalid")
            latest = messages[-1]
            normalized = self._message(latest, body_budget=0)
            threads.append(
                {
                    "thread_id": thread_id,
                    "senders": [normalized["from"]] if normalized["from"] else [],
                    "subject": normalized["subject"],
                    "date": normalized["date"],
                    "snippet": str(latest.get("snippet", ""))[:8192]
                    if isinstance(latest, dict)
                    else "",
                    "label_ids": normalized["label_ids"],
                }
            )
        result: dict[str, Any] = {"threads": threads}
        next_page = payload.get("nextPageToken")
        if isinstance(next_page, str) and next_page:
            result["next_page_token"] = next_page
        return result

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        thread_id = self._required_text(thread_id, "thread_id", maximum=1024)
        payload = await self._request(
            "GET",
            f"/threads/{quote(thread_id, safe='')}",
            params={"format": "full"},
        )
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise GmailError("gmail.provider_output_invalid")
        body_budget = max(1, (OUTPUT_MAXIMUM_BYTES - 64 * 1024) // max(1, len(messages)))
        return self._bounded(
            {
                "thread_id": str(payload.get("id", thread_id)),
                "messages": [
                    self._message(message, body_budget=body_budget) for message in messages
                ],
            }
        )

    async def list_labels(self) -> dict[str, Any]:
        payload = await self._request("GET", "/labels")
        labels = payload.get("labels")
        if not isinstance(labels, list):
            raise GmailError("gmail.provider_output_invalid")
        return {
            "labels": [
                {
                    "id": str(label.get("id", "")),
                    "name": str(label.get("name", "")),
                    "type": str(label.get("type", "")),
                }
                for label in labels
                if isinstance(label, dict)
            ]
        }

    @classmethod
    def _raw_message(
        cls,
        *,
        to: str,
        subject: str,
        body: str,
        cc: str | None,
        bcc: str | None,
    ) -> str:
        message = EmailMessage()
        message["To"] = cls._required_header(to, "to", maximum=8192)
        message["Subject"] = cls._required_header(subject, "subject", maximum=998)
        cls._required_text(body, "body", maximum=512 * 1024)
        if cc is not None:
            message["Cc"] = cls._required_header(cc, "cc", maximum=8192)
        if bcc is not None:
            message["Bcc"] = cls._required_header(bcc, "bcc", maximum=8192)
        message.set_content(body)
        return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")

    async def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        message: dict[str, object] = {
            "raw": self._raw_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        }
        if thread_id is not None:
            message["threadId"] = self._required_text(thread_id, "thread_id", maximum=1024)
        payload = await self._request("POST", "/drafts", body={"message": message}, mutating=True)
        nested = payload.get("message")
        return {
            "draft_id": str(payload.get("id", "")),
            "message_id": str(nested.get("id", "")) if isinstance(nested, dict) else "",
            "thread_id": str(nested.get("threadId", "")) if isinstance(nested, dict) else "",
        }

    async def modify_labels(
        self,
        thread_ids: list[str],
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(thread_ids, list) or not 1 <= len(thread_ids) <= 25:
            raise GmailError("gmail.arguments_invalid")
        if len(set(thread_ids)) != len(thread_ids):
            raise GmailError("gmail.arguments_invalid")
        normalized_threads = [
            self._required_text(item, "thread_id", maximum=1024) for item in thread_ids
        ]
        additions = list(add_label_ids or [])
        removals = list(remove_label_ids or [])
        if not additions and not removals:
            raise GmailError("gmail.arguments_invalid")
        if len(additions) > 100 or len(removals) > 100:
            raise GmailError("gmail.arguments_invalid")
        additions = [self._required_text(item, "label_id", maximum=1024) for item in additions]
        removals = [self._required_text(item, "label_id", maximum=1024) for item in removals]
        changed: list[str] = []
        for thread_id in normalized_threads:
            try:
                await self._request(
                    "POST",
                    f"/threads/{quote(thread_id, safe='')}/modify",
                    body={"addLabelIds": additions, "removeLabelIds": removals},
                    mutating=True,
                )
            except GmailError as exc:
                if changed:
                    raise GmailError("gmail.outcome_unknown") from exc
                raise
            changed.append(thread_id)
        return {"thread_ids": changed, "add_label_ids": additions, "remove_label_ids": removals}

    async def _thread_action(self, thread_id: str, action: str) -> dict[str, Any]:
        thread_id = self._required_text(thread_id, "thread_id", maximum=1024)
        payload = await self._request(
            "POST",
            f"/threads/{quote(thread_id, safe='')}/{action}",
            mutating=True,
        )
        return {
            "thread_id": str(payload.get("id", thread_id)),
            "label_ids": [str(item) for item in payload.get("labelIds", [])]
            if isinstance(payload.get("labelIds"), list)
            else [],
        }

    async def trash_thread(self, thread_id: str) -> dict[str, Any]:
        return await self._thread_action(thread_id, "trash")

    async def untrash_thread(self, thread_id: str) -> dict[str, Any]:
        return await self._thread_action(thread_id, "untrash")

    async def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, object] = {
            "raw": self._raw_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        }
        if thread_id is not None:
            request["threadId"] = self._required_text(thread_id, "thread_id", maximum=1024)
        payload = await self._request("POST", "/messages/send", body=request, mutating=True)
        return {
            "message_id": str(payload.get("id", "")),
            "thread_id": str(payload.get("threadId", "")),
        }
