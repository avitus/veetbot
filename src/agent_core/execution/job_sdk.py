"""Small standard-library-only client for the job broker (inside the sandbox)."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
from typing import Any

_MESSAGE_BYTES = 64 * 1024
type _Call = Callable[[str, dict[str, Any]], dict[str, Any]]


class JobCallError(RuntimeError):
    """The broker could not complete the program's requested operation."""


class _HTTP:
    def __init__(self, call: _Call) -> None:
        self._call = call

    def request(self, method: str, url: str, *, json: object = None) -> dict[str, Any]:
        """Request one HTTP operation and return its JSON response envelope."""
        arguments: dict[str, Any] = {"method": method, "url": url}
        if json is not None:
            arguments["json"] = json
        return self._call("http.request", arguments)


class _Model:
    def __init__(self, call: _Call) -> None:
        self._call = call

    def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        """Request one explicit model completion and return its text."""
        result = self._call(
            "model.complete", {"prompt": prompt, "max_output_tokens": max_output_tokens}
        )
        text = result.get("text")
        if not isinstance(text, str):
            raise JobCallError("Model completion returned no text.")
        return text


class _Feed:
    def __init__(self, call: _Call) -> None:
        self._call = call

    def publish(
        self,
        feed_id: str,
        data: object,
        *,
        expected_interval_seconds: int,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish declared feed data, optionally supplying its schema."""
        arguments: dict[str, Any] = {
            "feed_id": feed_id,
            "data": data,
            "expected_interval_seconds": expected_interval_seconds,
        }
        if schema is not None:
            arguments["schema"] = schema
        return self._call("feed.publish", arguments)


class JobContext:
    """Synchronous broker operations; constructing the context performs no I/O.

    The execution-service relay supplies the one-time token. The library never
    receives a service credential and cannot turn a declaration into a grant.
    """

    def __init__(self) -> None:
        self._ordinal = 0
        self.http = _HTTP(self._call)
        self.model = _Model(self._call)
        self.feed = _Feed(self._call)

    def log(self, message: str) -> None:
        """Send a bounded diagnostic line through the broker."""
        if not isinstance(message, str) or len(message.encode()) > 2000:
            raise JobCallError("A job log line must be text of at most 2,000 bytes.")
        self._call("log", {"message": message})

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Send a declared operation through the bridge and raise useful refusal feedback."""
        path = os.environ.get("AGENT_TOOL_BRIDGE_SOCKET")
        if not path:
            raise JobCallError("Job broker is unavailable for this execution.")
        encoded = json.dumps(
            {"call": name, "arguments": arguments, "ordinal": self._ordinal},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        # Leave room for the relay's token and the newline in the existing frame.
        if len(encoded) > _MESSAGE_BYTES - 1024:
            raise JobCallError("Job broker request exceeds the bridge message limit.")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(60)
            connection.connect(path)
            connection.sendall(encoded + b"\n")
            with connection.makefile("rb") as stream:
                response = stream.readline(_MESSAGE_BYTES + 1)
        self._ordinal += 1
        if not response.endswith(b"\n") or len(response) > _MESSAGE_BYTES:
            raise JobCallError("Job broker response is missing or exceeds the message limit.")
        try:
            result = json.loads(response)
        except (UnicodeDecodeError, ValueError) as exc:
            raise JobCallError("Job broker response is not JSON.") from exc
        if not isinstance(result, dict):
            raise JobCallError("Job broker response is not an object.")
        if result.get("status") != "succeeded":
            raise JobCallError(str(result.get("reason_code", "Job broker call failed."))[:2000])
        value = result.get("result")
        if not isinstance(value, dict):
            raise JobCallError("Job broker result is not an object.")
        return value
