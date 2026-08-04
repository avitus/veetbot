"""Programmatic orchestration bridge that re-enters the ordinary tool pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

type BridgeDispatch = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]


def bridge_call_id(script_hash: str, ordinal: int) -> str:
    material = script_hash.encode("ascii") + b"\x00" + str(ordinal).encode("ascii")
    return "bridge:" + hashlib.sha256(material).hexdigest()[:32]


class BridgeProtocolError(ValueError):
    pass


class ProgrammaticBridgeSession:
    def __init__(
        self,
        *,
        script_hash: str,
        token: str,
        dispatch: BridgeDispatch,
        maximum_calls: int = 64,
        approval_hold_seconds: float = 300,
    ) -> None:
        if len(script_hash) != 64:
            raise ValueError("script_hash must be a SHA-256 hex digest")
        self._script_hash = script_hash
        self._token = token
        self._dispatch = dispatch
        self._maximum_calls = maximum_calls
        self._approval_hold_seconds = approval_hold_seconds
        self._ordinal = 0

    @property
    def call_count(self) -> int:
        return self._ordinal

    @property
    def token(self) -> str:
        return self._token

    async def handle(self, request: bytes) -> bytes:
        if len(request) > 64 * 1024:
            raise BridgeProtocolError("bridge request exceeds 64 KiB")
        try:
            loaded: object = json.loads(request)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeProtocolError("bridge request is not valid JSON") from exc
        if not isinstance(loaded, dict):
            raise BridgeProtocolError("bridge request must be an object")
        token = loaded.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            return b'{"status":"denied","reason_code":"bridge.unauthorized","retryable":false}'
        call = loaded.get("call")
        arguments = loaded.get("arguments")
        requested_ordinal = loaded.get("ordinal")
        if (
            not isinstance(call, str)
            or not isinstance(arguments, dict)
            or requested_ordinal != self._ordinal
        ):
            raise BridgeProtocolError("bridge call, arguments, or ordinal is invalid")
        if self._ordinal >= self._maximum_calls:
            return b'{"status":"denied","reason_code":"bridge.call_limit","retryable":false}'
        ordinal = self._ordinal
        self._ordinal += 1
        call_id = bridge_call_id(self._script_hash, ordinal)
        try:
            response = await asyncio.wait_for(
                self._dispatch(
                    call,
                    {str(key): value for key, value in arguments.items()},
                    call_id,
                ),
                timeout=self._approval_hold_seconds,
            )
        except TimeoutError:
            response = {
                "status": "suspended",
                "reason_code": "bridge.approval_hold_expired",
                "retryable": True,
            }
        allowed = {"status", "result", "reason_code", "retryable"}
        filtered = {key: value for key, value in response.items() if key in allowed}
        return json.dumps(filtered, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class UnixToolBridgeServer:
    """One-turn newline-delimited JSON server over a mode-0600 Unix socket."""

    def __init__(self, socket_path: Path, session: ProgrammaticBridgeSession) -> None:
        self._socket_path = socket_path
        self._session = session
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=self._socket_path)
        await asyncio.to_thread(os.chmod, self._socket_path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._socket_path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while request := await reader.readline():
                try:
                    response = await self._session.handle(request)
                except BridgeProtocolError as exc:
                    response = json.dumps(
                        {
                            "status": "denied",
                            "reason_code": "bridge.protocol_error",
                            "retryable": False,
                            "result": {"message": str(exc)},
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                writer.write(response + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
