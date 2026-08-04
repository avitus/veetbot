"""Programmatic orchestration bridge that re-enters the ordinary tool pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

type BridgeDispatch = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]

logger = logging.getLogger(__name__)


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
        if not isinstance(token, str) or not hmac.compare_digest(
            token.encode("utf-8"), self._token.encode("utf-8")
        ):
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
        self._directory_fd: int | None = None

    async def start(self) -> None:
        self._socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_fd = await asyncio.to_thread(os.open, self._socket_path.parent, flags)
        try:
            await asyncio.to_thread(os.fchmod, directory_fd, 0o700)
            with suppress(FileNotFoundError):
                await asyncio.to_thread(os.unlink, self._socket_path.name, dir_fd=directory_fd)
            directory_identity = os.fstat(directory_fd)
            descriptor_root = Path("/proc/self/fd")
            if descriptor_root.is_dir():
                bind_path = f"{descriptor_root}/{directory_fd}/{self._socket_path.name}"
            else:
                visible_identity = os.stat(self._socket_path.parent, follow_symlinks=False)
                if (visible_identity.st_dev, visible_identity.st_ino) != (
                    directory_identity.st_dev,
                    directory_identity.st_ino,
                ):
                    raise RuntimeError("bridge directory is not a stable real directory")
                bind_path = str(self._socket_path)
            bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                await asyncio.to_thread(bound_socket.bind, bind_path)
                visible_identity = os.stat(self._socket_path.parent, follow_symlinks=False)
                if (visible_identity.st_dev, visible_identity.st_ino) != (
                    directory_identity.st_dev,
                    directory_identity.st_ino,
                ):
                    raise RuntimeError("bridge directory changed while binding")
                await asyncio.to_thread(
                    os.chmod,
                    self._socket_path.name,
                    0o600,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                bound_socket.setblocking(False)
                self._server = await asyncio.start_unix_server(
                    self._handle, sock=bound_socket, limit=64 * 1024 + 1
                )
            except BaseException:
                bound_socket.close()
                with suppress(FileNotFoundError):
                    os.unlink(self._socket_path.name, dir_fd=directory_fd)
                raise
        except BaseException:
            os.close(directory_fd)
            raise
        self._directory_fd = directory_fd

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._directory_fd is not None:
            with suppress(FileNotFoundError):
                await asyncio.to_thread(
                    os.unlink,
                    self._socket_path.name,
                    dir_fd=self._directory_fd,
                )
            os.close(self._directory_fd)
            self._directory_fd = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    request = await reader.readline()
                except ValueError:
                    writer.write(_bridge_denial("bridge.request_too_large") + b"\n")
                    await writer.drain()
                    return
                if not request:
                    return
                try:
                    response = await self._session.handle(request)
                except BridgeProtocolError as exc:
                    response = _bridge_denial("bridge.protocol_error", str(exc))
                except Exception:
                    logger.exception("bridge_dispatch_failed")
                    response = _bridge_denial("bridge.internal_error")
                writer.write(response + b"\n")
                await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


def _bridge_denial(reason_code: str, message: str | None = None) -> bytes:
    payload: dict[str, object] = {
        "status": "denied",
        "reason_code": reason_code,
        "retryable": False,
    }
    if message is not None:
        payload["result"] = {"message": message}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")
