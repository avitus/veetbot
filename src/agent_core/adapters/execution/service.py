"""Unix-socket client and server for the credential-free execution service."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import socket
import stat
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from uuid import UUID

from agent_core.adapters.execution.local_workspace import validated_workspace_components
from agent_core.domain.errors import (
    ExecutionRejected,
    ExecutionUnavailable,
    WorkspaceEscape,
    WorkspaceReadLimitExceededError,
)
from agent_core.domain.execution import (
    BridgeEndpoint,
    ChangeKind,
    EgressDestination,
    EgressMode,
    EgressPolicy,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    FileChange,
    KillReason,
    ResourceLimits,
    WorkspaceEntry,
    WorkspaceProvenance,
)
from agent_core.ports.execution import ExecutionEnvironment, WorkspaceHandle

_HEADER = struct.Struct("!Q")
_MAX_FRAME_BYTES = 512 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
_CONNECT_ATTEMPTS = 50
_CONNECT_RETRY_SECONDS = 0.1

_DATACLASSES: dict[str, type[Any]] = {
    item.__name__: item
    for item in (
        BridgeEndpoint,
        EgressDestination,
        EgressPolicy,
        EnvironmentHandle,
        EnvironmentSpec,
        ExecutionCommand,
        ExecutionResult,
        FileChange,
        ResourceLimits,
        WorkspaceEntry,
    )
}
_ENUMS: dict[str, type[Enum]] = {
    item.__name__: item
    for item in (
        ChangeKind,
        EgressMode,
        KillReason,
        WorkspaceProvenance,
    )
}
_REMOTE_ERRORS: dict[str, type[Exception]] = {
    "ExecutionRejected": ExecutionRejected,
    "ExecutionUnavailable": ExecutionUnavailable,
    "FileNotFoundError": FileNotFoundError,
    "IsADirectoryError": IsADirectoryError,
    "NotADirectoryError": NotADirectoryError,
    "ValueError": ValueError,
    "WorkspaceEscape": WorkspaceEscape,
    "WorkspaceReadLimitExceededError": WorkspaceReadLimitExceededError,
}


class _BridgeHandler(Protocol):
    async def handle(self, request: bytes) -> bytes: ...


class _ServiceEnvironment(ExecutionEnvironment, Protocol):
    def workspace(self, environment: EnvironmentHandle) -> WorkspaceHandle: ...

    async def execute_with_bridge(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
        endpoint: BridgeEndpoint,
        handler: _BridgeHandler,
    ) -> ExecutionResult: ...

    async def reap(
        self,
        live_leases: frozenset[tuple[object, int]],
        is_live: Callable[[UUID, int], Awaitable[bool]] | None = None,
    ) -> int: ...


def _encode(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__dataclass__": type(value).__name__,
            "fields": {
                field.name: _encode(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }
    if isinstance(value, Enum):
        return {"__enum__": type(value).__name__, "value": value.value}
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, PurePosixPath):
        return {"__posix_path__": str(value)}
    if isinstance(value, Mapping):
        return {"__mapping__": [[str(key), _encode(item)] for key, item in value.items()]}
    if isinstance(value, frozenset):
        return {"__frozenset__": [_encode(item) for item in value]}
    if isinstance(value, tuple):
        return {"__tuple__": [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"execution service cannot encode {type(value).__name__}")


def _decode(value: object) -> object:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__dataclass__" in value:
        name = str(value["__dataclass__"])
        data_type = _DATACLASSES.get(name)
        fields = value.get("fields")
        if data_type is None or not isinstance(fields, dict):
            raise ValueError("execution service received an unknown dataclass")
        return data_type(**{str(key): _decode(item) for key, item in fields.items()})
    if "__enum__" in value:
        enum_type = _ENUMS.get(str(value["__enum__"]))
        if enum_type is None:
            raise ValueError("execution service received an unknown enum")
        return enum_type(cast(Any, value.get("value")))
    if "__bytes__" in value:
        return base64.b64decode(str(value["__bytes__"]), validate=True)
    if "__datetime__" in value:
        return datetime.fromisoformat(str(value["__datetime__"]))
    if "__uuid__" in value:
        return UUID(str(value["__uuid__"]))
    if "__posix_path__" in value:
        return PurePosixPath(str(value["__posix_path__"]))
    if "__mapping__" in value:
        pairs = value["__mapping__"]
        if not isinstance(pairs, list):
            raise ValueError("execution service received an invalid mapping")
        return {str(pair[0]): _decode(pair[1]) for pair in pairs if isinstance(pair, list)}
    if "__frozenset__" in value:
        items = value["__frozenset__"]
        if not isinstance(items, list):
            raise ValueError("execution service received an invalid frozenset")
        return frozenset(_decode(item) for item in items)
    if "__tuple__" in value:
        items = value["__tuple__"]
        if not isinstance(items, list):
            raise ValueError("execution service received an invalid tuple")
        return tuple(_decode(item) for item in items)
    raise ValueError("execution service received an untagged object")


async def _write_frame(writer: asyncio.StreamWriter, payload: Mapping[str, object]) -> None:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > _MAX_FRAME_BYTES:
        raise ExecutionRejected("execution service frame exceeds the size limit")
    writer.write(_HEADER.pack(len(body)) + body)
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, object]:
    size = _HEADER.unpack(await reader.readexactly(_HEADER.size))[0]
    if size > _MAX_FRAME_BYTES:
        raise ExecutionRejected("execution service frame exceeds the size limit")
    decoded = json.loads((await reader.readexactly(size)).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ExecutionRejected("execution service frame must be an object")
    return cast(dict[str, object], decoded)


def _raise_remote(frame: Mapping[str, object]) -> None:
    error_type = str(frame.get("error_type", "ExecutionUnavailable"))
    message = str(frame.get("message", "execution service request failed"))
    exception_type = _REMOTE_ERRORS.get(error_type, ExecutionUnavailable)
    raise exception_type(message)


class ExecutionServiceWorkspace:
    def __init__(self, owner: ExecutionServiceClient, environment: EnvironmentHandle) -> None:
        self._owner = owner
        self._environment = environment

    @property
    def root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath:
        return self.root.joinpath(*validated_workspace_components(path))

    async def read(self, path: str) -> bytes:
        result = await self._owner._call(
            "workspace_read", {"environment": self._environment, "path": path}
        )
        return cast(bytes, result)

    async def read_bounded(self, path: str, maximum_bytes: int) -> bytes:
        result = await self._owner._call(
            "workspace_read_bounded",
            {"environment": self._environment, "path": path, "maximum_bytes": maximum_bytes},
        )
        return cast(bytes, result)

    async def stream(self, path: str, maximum_bytes: int) -> AsyncIterator[bytes]:
        async for chunk in self._owner._stream(
            "workspace_stream",
            {"environment": self._environment, "path": path, "maximum_bytes": maximum_bytes},
        ):
            yield chunk

    async def write(self, path: str, data: bytes) -> None:
        await self._owner._call(
            "workspace_write",
            {"environment": self._environment, "path": path, "data": data},
        )

    async def listdir(self, path: str, *, recursive: bool = False) -> tuple[WorkspaceEntry, ...]:
        result = await self._owner._call(
            "workspace_listdir",
            {"environment": self._environment, "path": path, "recursive": recursive},
        )
        return cast(tuple[WorkspaceEntry, ...], result)

    async def provenance(self, path: str) -> WorkspaceProvenance:
        result = await self._owner._call(
            "workspace_provenance", {"environment": self._environment, "path": path}
        )
        return cast(WorkspaceProvenance, result)


class ExecutionServiceClient:
    """ExecutionEnvironment adapter that gives workers no container-runtime access."""

    def __init__(self, socket_path: Path) -> None:
        if not socket_path.is_absolute():
            raise ValueError("execution service socket must be an absolute path")
        self._socket_path = socket_path

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        failure: OSError | None = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                return await asyncio.open_unix_connection(str(self._socket_path))
            except OSError as exc:
                failure = exc
                if attempt + 1 < _CONNECT_ATTEMPTS:
                    await asyncio.sleep(_CONNECT_RETRY_SECONDS)
        raise ExecutionUnavailable("execution service socket is unavailable") from failure

    async def _call(
        self,
        operation: str,
        payload: Mapping[str, object],
        callback: Callable[[str, object], Awaitable[object]] | None = None,
    ) -> object:
        reader, writer = await self._connect()
        try:
            await _write_frame(
                writer,
                {"kind": "request", "operation": operation, "payload": _encode(payload)},
            )
            while True:
                frame = await _read_frame(reader)
                kind = frame.get("kind")
                if kind == "callback":
                    if callback is None:
                        raise ExecutionRejected("execution service sent an unexpected callback")
                    callback_id = frame.get("id")
                    try:
                        callback_result = await callback(
                            str(frame.get("callback")), _decode(frame.get("payload"))
                        )
                    except Exception as exc:
                        await _write_frame(
                            writer,
                            {
                                "kind": "callback_response",
                                "id": callback_id,
                                "ok": False,
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                            },
                        )
                    else:
                        await _write_frame(
                            writer,
                            {
                                "kind": "callback_response",
                                "id": callback_id,
                                "ok": True,
                                "payload": _encode(callback_result),
                            },
                        )
                    continue
                if kind != "response":
                    raise ExecutionUnavailable("execution service returned an invalid frame")
                if not frame.get("ok"):
                    _raise_remote(frame)
                return _decode(frame.get("payload"))
        except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError) as exc:
            raise ExecutionUnavailable("execution service connection failed") from exc
        finally:
            writer.close()
            await writer.wait_closed()

    async def _stream(
        self,
        operation: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[bytes]:
        reader, writer = await self._connect()
        try:
            await _write_frame(
                writer,
                {"kind": "request", "operation": operation, "payload": _encode(payload)},
            )
            while True:
                frame = await _read_frame(reader)
                kind = frame.get("kind")
                if kind == "stream_chunk":
                    chunk = _decode(frame.get("payload"))
                    if not isinstance(chunk, bytes) or len(chunk) > _STREAM_CHUNK_BYTES:
                        raise ExecutionUnavailable("execution service returned an invalid stream")
                    yield chunk
                    continue
                if kind != "response":
                    raise ExecutionUnavailable("execution service returned an invalid stream frame")
                if not frame.get("ok"):
                    _raise_remote(frame)
                return
        except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError) as exc:
            raise ExecutionUnavailable("execution service connection failed") from exc
        finally:
            writer.close()
            await writer.wait_closed()

    async def resolve_image_digest(self, reference: str) -> str:
        return cast(str, await self._call("resolve_image_digest", {"reference": reference}))

    async def provision(self, specification: EnvironmentSpec) -> EnvironmentHandle:
        return cast(
            EnvironmentHandle,
            await self._call("provision", {"specification": specification}),
        )

    async def execute(
        self, environment: EnvironmentHandle, command: ExecutionCommand
    ) -> ExecutionResult:
        return cast(
            ExecutionResult,
            await self._call("execute", {"environment": environment, "command": command}),
        )

    async def execute_with_bridge(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
        endpoint: BridgeEndpoint,
        handler: _BridgeHandler,
    ) -> ExecutionResult:
        async def callback(name: str, payload: object) -> object:
            if name != "bridge" or not isinstance(payload, bytes):
                raise ExecutionRejected("execution service bridge callback is invalid")
            return await handler.handle(payload)

        return cast(
            ExecutionResult,
            await self._call(
                "execute_with_bridge",
                {"environment": environment, "command": command, "endpoint": endpoint},
                callback,
            ),
        )

    async def destroy(self, environment: EnvironmentHandle) -> None:
        await self._call("destroy", {"environment": environment})

    def workspace(self, environment: EnvironmentHandle) -> ExecutionServiceWorkspace:
        return ExecutionServiceWorkspace(self, environment)

    async def reap(
        self,
        live_leases: frozenset[tuple[UUID, int]],
        is_live: Callable[[UUID, int], Awaitable[bool]] | None = None,
    ) -> int:
        async def callback(name: str, payload: object) -> object:
            if name != "lease_is_live" or not isinstance(payload, tuple) or len(payload) != 2:
                raise ExecutionRejected("execution service lease callback is invalid")
            run_id, lease_epoch = payload
            if not isinstance(run_id, UUID) or not isinstance(lease_epoch, int):
                raise ExecutionRejected("execution service lease callback is invalid")
            return False if is_live is None else await is_live(run_id, lease_epoch)

        return cast(
            int,
            await self._call("reap", {"live_leases": live_leases}, callback),
        )


class _RemoteCallback:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0

    async def call(self, name: str, payload: object) -> object:
        self._next_id += 1
        callback_id = self._next_id
        await _write_frame(
            self._writer,
            {
                "kind": "callback",
                "id": callback_id,
                "callback": name,
                "payload": _encode(payload),
            },
        )
        frame = await _read_frame(self._reader)
        if frame.get("kind") != "callback_response" or frame.get("id") != callback_id:
            raise ExecutionUnavailable("execution service callback response is invalid")
        if not frame.get("ok"):
            _raise_remote(frame)
        return _decode(frame.get("payload"))

    async def send_stream_chunk(self, chunk: bytes) -> None:
        await _write_frame(
            self._writer,
            {"kind": "stream_chunk", "payload": _encode(chunk)},
        )


class _RemoteBridgeHandler:
    def __init__(self, callback: _RemoteCallback) -> None:
        self._callback = callback

    async def handle(self, request: bytes) -> bytes:
        return cast(bytes, await self._callback.call("bridge", request))


class ExecutionServiceServer:
    """Own the runtime adapter in a process that receives no application environment."""

    def __init__(
        self,
        environment: _ServiceEnvironment,
        socket_path: Path,
        *,
        resolve_image_digest: Callable[[str], Awaitable[str]],
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("execution service socket must be an absolute path")
        self._environment = environment
        self._socket_path = socket_path
        self._resolve_image_digest = resolve_image_digest
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        try:
            metadata = self._socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise ExecutionUnavailable("refusing to replace an unsafe execution service path")
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self._socket_path),
        )
        self._socket_path.chmod(0o660)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            metadata = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
            self._socket_path.unlink()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            frame = await _read_frame(reader)
            if frame.get("kind") != "request":
                raise ExecutionRejected("execution service expected one request")
            payload = _decode(frame.get("payload"))
            if not isinstance(payload, dict):
                raise ExecutionRejected("execution service request payload is invalid")
            callback = _RemoteCallback(reader, writer)
            result = await self._dispatch(str(frame.get("operation")), payload, callback)
        except Exception as exc:
            await _write_frame(
                writer,
                {
                    "kind": "response",
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        else:
            await _write_frame(
                writer,
                {"kind": "response", "ok": True, "payload": _encode(result)},
            )
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(
        self,
        operation: str,
        payload: dict[str, object],
        callback: _RemoteCallback,
    ) -> object:
        if operation == "resolve_image_digest":
            return await self._resolve_image_digest(cast(str, payload["reference"]))
        if operation == "provision":
            return await self._environment.provision(
                cast(EnvironmentSpec, payload["specification"])
            )
        environment = cast(EnvironmentHandle, payload.get("environment"))
        if operation == "execute":
            return await self._environment.execute(
                environment, cast(ExecutionCommand, payload["command"])
            )
        if operation == "execute_with_bridge":
            return await self._environment.execute_with_bridge(
                environment,
                cast(ExecutionCommand, payload["command"]),
                cast(BridgeEndpoint, payload["endpoint"]),
                _RemoteBridgeHandler(callback),
            )
        if operation == "destroy":
            await self._environment.destroy(environment)
            return None
        if operation.startswith("workspace_"):
            workspace = self._environment.workspace(environment)
            path = cast(str, payload["path"])
            if operation == "workspace_read":
                return await workspace.read(path)
            if operation == "workspace_read_bounded":
                return await workspace.read_bounded(path, cast(int, payload["maximum_bytes"]))
            if operation == "workspace_write":
                await workspace.write(path, cast(bytes, payload["data"]))
                return None
            if operation == "workspace_stream":
                maximum_bytes = cast(int, payload["maximum_bytes"])
                async for chunk in workspace.stream(path, maximum_bytes):
                    for offset in range(0, len(chunk), _STREAM_CHUNK_BYTES):
                        await callback.send_stream_chunk(
                            chunk[offset : offset + _STREAM_CHUNK_BYTES]
                        )
                return None
            if operation == "workspace_listdir":
                return tuple(
                    await workspace.listdir(path, recursive=cast(bool, payload["recursive"]))
                )
            if operation == "workspace_provenance":
                return await workspace.provenance(path)
        if operation == "reap":
            live_leases = cast(frozenset[tuple[object, int]], payload["live_leases"])

            async def is_live(run_id: UUID, lease_epoch: int) -> bool:
                return cast(
                    bool,
                    await callback.call("lease_is_live", (run_id, lease_epoch)),
                )

            return await self._environment.reap(live_leases, is_live)
        raise ExecutionRejected(f"unknown execution service operation: {operation}")


def unix_peer_credentials(writer: asyncio.StreamWriter) -> tuple[int, int, int] | None:
    """Return Linux peer pid/uid/gid when the platform exposes SO_PEERCRED."""

    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    raw = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return cast(tuple[int, int, int], struct.unpack("3i", raw))
