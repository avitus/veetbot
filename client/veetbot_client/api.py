"""Dependency-free HTTP and SSE client for the public Veetbot API."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from . import __version__

MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SSE_FRAME_BYTES = 1024 * 1024


class ClientError(RuntimeError):
    """Base class for safe, user-displayable client failures."""


class ConfigurationError(ClientError):
    """The local client configuration is unsafe or malformed."""


class ConnectionFailureError(ClientError):
    """The API could not be reached or the connection was interrupted."""


class ProtocolError(ClientError):
    """The API returned a response outside its declared contract."""


class ApiError(ClientError):
    """A structured error returned by the Veetbot API."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = dict(details or {})
        self.request_id = request_id

    def __str__(self) -> str:
        suffix = f" (request {self.request_id})" if self.request_id else ""
        return f"{self.code}: {super().__str__()}{suffix}"


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """One parsed server-sent event; transient events have no identifier."""

    event: str
    data: dict[str, object]
    event_id: int | None = None


class _OpenedResponse(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def __iter__(self) -> Iterator[bytes]: ...

    def __enter__(self) -> _OpenedResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class _Opener(Protocol):
    def open(
        self,
        fullurl: Request,
        data: bytes | None = None,
        timeout: float = 0,
    ) -> _OpenedResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep bearer credentials on the configured origin only."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _read_limited(response: _OpenedResponse, limit: int = MAX_JSON_RESPONSE_BYTES) -> bytes:
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ProtocolError("API response exceeded the client safety limit")
    return payload


def _json_object(payload: bytes, *, context: str) -> dict[str, object]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"{context} was not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError(f"{context} was not a JSON object")
    return cast(dict[str, object], decoded)


def _dispatch_sse(
    *, event_name: str | None, event_id: str | None, data_lines: list[str]
) -> SSEEvent | None:
    if not data_lines:
        return None
    raw_data = "\n".join(data_lines)
    try:
        decoded = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise ProtocolError("SSE data was not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("SSE data was not a JSON object")
    parsed_id: int | None = None
    if event_id is not None:
        try:
            parsed_id = int(event_id)
        except ValueError as exc:
            raise ProtocolError("SSE id was not an integer") from exc
        if parsed_id < 0:
            raise ProtocolError("SSE id was negative")
    return SSEEvent(
        event=event_name or "message",
        data=cast(dict[str, object], decoded),
        event_id=parsed_id,
    )


def parse_sse(lines: Iterable[bytes]) -> Iterator[SSEEvent]:
    """Parse SSE without assigning identifiers to transient frames."""

    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    frame_bytes = 0
    for raw_line in lines:
        frame_bytes += len(raw_line)
        if frame_bytes > MAX_SSE_FRAME_BYTES:
            raise ProtocolError("SSE frame exceeded the client safety limit")
        try:
            line = raw_line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise ProtocolError("SSE stream was not valid UTF-8") from exc
        if not line:
            event = _dispatch_sse(
                event_name=event_name,
                event_id=event_id,
                data_lines=data_lines,
            )
            if event is not None:
                yield event
            event_name = None
            event_id = None
            data_lines = []
            frame_bytes = 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
    event = _dispatch_sse(event_name=event_name, event_id=event_id, data_lines=data_lines)
    if event is not None:
        yield event


class ApiClient:
    """Small synchronous client over the fourteen-route public API."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 45.0,
        opener: _Opener | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ConfigurationError("API URL must be an absolute http or https URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigurationError("API URL must not contain credentials, a query, or a fragment")
        if timeout_seconds <= 15:
            raise ConfigurationError("timeout must exceed the API's 15-second heartbeat interval")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._parsed_url = parsed
        self._token: str | None = None
        self._opener = opener or cast(_Opener, build_opener(_NoRedirectHandler()))
        self.set_token(token)

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def set_token(self, token: str | None) -> None:
        normalized = None if token is None else token.strip()
        if not normalized:
            self._token = None
            return
        hostname = self._parsed_url.hostname or ""
        loopback = hostname == "localhost"
        with suppress(ValueError):
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
        if self._parsed_url.scheme != "https" and not loopback:
            raise ConfigurationError("bearer tokens require HTTPS for non-loopback API URLs")
        self._token = normalized

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("API path must start with a slash")
        return f"{self.base_url}{path}"

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"veetbot-client/{__version__}",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        if extra:
            headers.update(extra)
        return headers

    def _api_error(self, error: HTTPError) -> ApiError:
        try:
            body = error.read(MAX_JSON_RESPONSE_BYTES + 1)
            if len(body) > MAX_JSON_RESPONSE_BYTES:
                raise ProtocolError("API error response exceeded the client safety limit")
            payload = _json_object(body, context="API error response")
            raw_error = payload.get("error")
            if not isinstance(raw_error, dict):
                raise ProtocolError("API error response had no error object")
            code = raw_error.get("code")
            message = raw_error.get("message")
            details = raw_error.get("details")
            request_id = raw_error.get("request_id")
            if not isinstance(code, str) or not isinstance(message, str):
                raise ProtocolError("API error response omitted its code or message")
            return ApiError(
                status=error.code,
                code=code,
                message=message,
                details=details if isinstance(details, dict) else None,
                request_id=request_id if isinstance(request_id, str) else None,
            )
        except (ProtocolError, OSError, TimeoutError, URLError):
            return ApiError(
                status=error.code,
                code=f"http_{error.code}",
                message=f"API returned HTTP {error.code}",
            )

    def _open(self, request: Request) -> _OpenedResponse:
        try:
            return self._opener.open(request, None, self.timeout_seconds)
        except HTTPError as exc:
            raise self._api_error(exc) from exc
        except (OSError, TimeoutError, URLError) as exc:
            raise ConnectionFailureError(
                f"could not connect to API at {self.base_url}; "
                "verify the server is running and the URL is correct"
            ) from exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        encoded = None
        request_headers = self._headers(headers)
        if body is not None:
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(  # noqa: S310 - ApiClient restricts base URLs to HTTP(S).
            self._url(path),
            data=encoded,
            headers=request_headers,
            method=method,
        )
        with self._open(request) as response:
            return _json_object(_read_limited(response), context="API response")

    def health_ready(self) -> dict[str, object]:
        return self._request_json("GET", "/health/ready")

    def create_session(self, agent_id: str = "general") -> dict[str, object]:
        return self._request_json(
            "POST",
            "/v1/sessions",
            body={"agent_id": agent_id, "metadata": {}},
        )

    def get_session(self, session_id: str) -> dict[str, object]:
        return self._request_json("GET", f"/v1/sessions/{quote(session_id, safe='')}")

    def submit_message(
        self,
        session_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        key = idempotency_key or str(uuid4())
        return self._request_json(
            "POST",
            f"/v1/sessions/{quote(session_id, safe='')}/messages",
            body={"content": [{"type": "text", "text": text}]},
            headers={"Idempotency-Key": key},
        )

    def get_run(self, run_id: str) -> dict[str, object]:
        return self._request_json("GET", f"/v1/runs/{quote(run_id, safe='')}")

    def cancel_run(self, run_id: str) -> dict[str, object]:
        return self._request_json("POST", f"/v1/runs/{quote(run_id, safe='')}/cancel")

    def deliver_input(self, run_id: str, text: str, question_id: str | None) -> dict[str, object]:
        body: dict[str, object] = {"content": [{"type": "text", "text": text}]}
        if question_id is not None:
            body["question_id"] = question_id
        return self._request_json(
            "POST",
            f"/v1/runs/{quote(run_id, safe='')}/input",
            body=body,
        )

    def get_approval(self, approval_id: str) -> dict[str, object]:
        return self._request_json("GET", f"/v1/approvals/{quote(approval_id, safe='')}")

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        reason: str | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {"decision": decision}
        if reason:
            body["reason"] = reason
        return self._request_json(
            "POST",
            f"/v1/approvals/{quote(approval_id, safe='')}/resolve",
            body=body,
        )

    def stream_events(self, run_id: str, last_event_id: int | None = None) -> Iterator[SSEEvent]:
        headers = self._headers({"Accept": "text/event-stream"})
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        request = Request(  # noqa: S310 - ApiClient restricts base URLs to HTTP(S).
            self._url(f"/v1/runs/{quote(run_id, safe='')}/events"),
            headers=headers,
            method="GET",
        )
        try:
            with self._open(request) as response:
                yield from parse_sse(response)
        except (ApiError, ProtocolError):
            raise
        except (OSError, TimeoutError, URLError) as exc:
            raise ConnectionFailureError("event stream was interrupted") from exc
