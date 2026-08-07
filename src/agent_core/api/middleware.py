"""Request identifiers and bounded request bodies at the ASGI edge."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_core.domain.agents import Principal
from agent_core.domain.errors import AuthenticationError

MAX_BODY_BYTES = 1024 * 1024
MAX_CONCURRENT_BUFFERED_BODIES = 16
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class PayloadTooLargeError(ValueError):
    """The ASGI receive stream exceeded the public request cap."""


class RequestBoundaryMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        new_request_id: Callable[[], str],
        early_authenticate: Callable[[Scope], Principal],
    ) -> None:
        self._app = app
        self._new_request_id = new_request_id
        self._early_authenticate = early_authenticate
        self._body_slots = asyncio.Semaphore(MAX_CONCURRENT_BUFFERED_BODIES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied if REQUEST_ID.fullmatch(supplied) else self._new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        has_body = method in {"POST", "PUT", "PATCH"}
        is_versioned_api = path.startswith("/v1/")
        if has_body and is_versioned_api:
            try:
                state["authenticated_principal"] = self._early_authenticate(scope)
            except AuthenticationError:
                await _boundary_error(
                    send,
                    request_id,
                    "authentication_failed",
                    401,
                    "Authentication failed.",
                    headers=[(b"www-authenticate", b"Bearer")],
                )
                return
        requires_json = method == "POST" and (
            path == "/v1/sessions"
            or path.endswith("/messages")
            or path.endswith("/input")
            or path.endswith("/resolve")
        )
        media_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if requires_json and media_type != b"application/json":
            await _boundary_error(
                send,
                request_id,
                "unsupported_media_type",
                415,
                "Content-Type must be application/json.",
            )
            return

        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                await _boundary_error(
                    send,
                    request_id,
                    "malformed_request",
                    400,
                    "Content-Length must be a non-negative integer.",
                )
                return
            if declared_length < 0:
                await _boundary_error(
                    send,
                    request_id,
                    "malformed_request",
                    400,
                    "Content-Length must be a non-negative integer.",
                )
                return
            if declared_length > MAX_BODY_BYTES:
                await _boundary_error(send, request_id, "payload_too_large", 413)
                return

        consumed = 0
        buffered: deque[Message] = deque()
        body_slot_acquired = False
        if has_body and is_versioned_api:
            await self._body_slots.acquire()
            body_slot_acquired = True
            try:
                while True:
                    message = await receive()
                    buffered.append(message)
                    if message["type"] != "http.request":
                        break
                    consumed += len(message.get("body", b""))
                    if consumed > MAX_BODY_BYTES:
                        self._body_slots.release()
                        body_slot_acquired = False
                        await _boundary_error(send, request_id, "payload_too_large", 413)
                        return
                    if not message.get("more_body", False):
                        break
            except BaseException:
                self._body_slots.release()
                body_slot_acquired = False
                raise
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal consumed
            if buffered:
                return buffered.popleft()
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > MAX_BODY_BYTES:
                    raise PayloadTooLargeError
            return message

        async def identified_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        try:
            await self._app(scope, bounded_receive, identified_send)
        except PayloadTooLargeError:
            if response_started:
                raise
            await _boundary_error(send, request_id, "payload_too_large", 413)
        finally:
            if body_slot_acquired:
                self._body_slots.release()


async def _boundary_error(
    send: Send,
    request_id: str,
    code: str,
    status: int,
    message: str = "The request body exceeds the 1 MiB limit.",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    import json

    body = json.dumps(
        {
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "request_id": request_id,
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-request-id", request_id.encode("ascii")),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
