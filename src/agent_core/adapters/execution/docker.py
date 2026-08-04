"""Development Docker and configurable gVisor execution-service adapter.

This is the only package allowed to invoke the container runtime. Docker is a
development fallback; selecting ``runtime=runsc`` makes the same service use
gVisor where it is installed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Protocol
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
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    FileChange,
    KillReason,
    WorkspaceEntry,
    WorkspaceProvenance,
)
from agent_core.ports.determinism import Clock, IdFactory

_VIRTUAL_ROOT = PurePosixPath("/workspace")
_DOCKER_COMMAND_TIMEOUT_SECONDS = 60.0
_SNAPSHOT_SCRIPT = r"""
import hashlib,json,os,stat
result={}
for base,dirs,files in os.walk('/workspace',followlinks=False):
  dirs[:]=[d for d in dirs if not os.path.islink(os.path.join(base,d))]
  for name in files:
    path=os.path.join(base,name)
    try:
      mode=os.lstat(path).st_mode
      if not stat.S_ISREG(mode): continue
      h=hashlib.sha256()
      with open(path,'rb') as source:
        while chunk:=source.read(65536): h.update(chunk)
      result[os.path.relpath(path,'/workspace')] = [os.path.getsize(path),h.hexdigest()]
    except (FileNotFoundError,PermissionError,OSError): pass
print(json.dumps(result,separators=(',',':'),sort_keys=True))
"""
_WORKSPACE_USAGE_SCRIPT = r"""
import os,stat
size=0
inodes=0
for base,dirs,files in os.walk('/workspace',followlinks=False):
  dirs[:]=[d for d in dirs if not os.path.islink(os.path.join(base,d))]
  for name in files:
    try:
      mode=os.lstat(os.path.join(base,name)).st_mode
      if stat.S_ISREG(mode):
        size += os.path.getsize(os.path.join(base,name))
        inodes += 1
    except (FileNotFoundError,PermissionError,OSError): pass
print(f'{size} {inodes}')
"""
_PROXY_READY_SCRIPT = r"""
import socket
with socket.create_connection(('egress-proxy',3128),timeout=1): pass
"""
_BRIDGE_READY_SCRIPT = r"""
import os,stat,sys
mode=os.stat(sys.argv[1]).st_mode
raise SystemExit(0 if stat.S_ISSOCK(mode) and mode & 0o777 == 0o600 else 44)
"""
_BRIDGE_STOP_SCRIPT = r"""
import os,signal,sys
for entry in os.listdir('/proc'):
  if not entry.isdigit(): continue
  try:
    arguments=open(f'/proc/{entry}/cmdline','rb').read().split(b'\0')
    if b'agent_core.execution.bridge_relay' in arguments:
      os.kill(int(entry),signal.SIGTERM)
  except (FileNotFoundError,PermissionError,ProcessLookupError): pass
try: os.unlink(sys.argv[1])
except FileNotFoundError: pass
"""
_WORKSPACE_READ_SCRIPT = r"""
import errno,os,stat,sys
parts=[] if sys.argv[1] in ('','.') else sys.argv[1].split('/')
limit=int(sys.argv[2])
directory_flags=os.O_RDONLY|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
file_flags=os.O_RDONLY|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOFOLLOW',0)
def fail(exc,parent,name):
  if exc.errno==errno.ENOENT: raise SystemExit(44)
  if exc.errno==errno.ELOOP: raise SystemExit(47)
  if exc.errno==errno.ENOTDIR:
    try: mode=os.stat(name,dir_fd=parent,follow_symlinks=False).st_mode
    except OSError: raise SystemExit(48)
    raise SystemExit(47 if stat.S_ISLNK(mode) else 48)
  raise exc
directory=os.open('/workspace',directory_flags)
descriptor=None
try:
  if not parts: raise SystemExit(45)
  for component in parts[:-1]:
    try: next_directory=os.open(component,directory_flags,dir_fd=directory)
    except OSError as exc: fail(exc,directory,component)
    os.close(directory); directory=next_directory
  try: descriptor=os.open(parts[-1],file_flags,dir_fd=directory)
  except OSError as exc: fail(exc,directory,parts[-1])
  metadata=os.fstat(descriptor)
  if not stat.S_ISREG(metadata.st_mode): raise SystemExit(45)
  if limit>=0 and metadata.st_size>limit: raise SystemExit(46)
  observed=0
  while True:
    chunk=os.read(descriptor,65536)
    if not chunk: break
    observed+=len(chunk)
    if limit>=0 and observed>limit: raise SystemExit(46)
    sys.stdout.buffer.write(chunk)
finally:
  if descriptor is not None: os.close(descriptor)
  os.close(directory)
"""
_WORKSPACE_WRITE_SCRIPT = r"""
import errno,os,stat,sys
parts=[] if sys.argv[1] in ('','.') else sys.argv[1].split('/')
directory_flags=os.O_RDONLY|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
file_flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOFOLLOW',0)
def fail(exc,parent,name):
  if exc.errno==errno.ENOENT: raise SystemExit(44)
  if exc.errno==errno.ELOOP: raise SystemExit(47)
  if exc.errno in (errno.EISDIR,errno.ENXIO): raise SystemExit(45)
  if exc.errno==errno.ENOTDIR:
    try: mode=os.stat(name,dir_fd=parent,follow_symlinks=False).st_mode
    except OSError: raise SystemExit(48)
    raise SystemExit(47 if stat.S_ISLNK(mode) else 48)
  raise exc
directory=os.open('/workspace',directory_flags)
descriptor=None
try:
  if not parts: raise SystemExit(45)
  for component in parts[:-1]:
    try: next_directory=os.open(component,directory_flags,dir_fd=directory)
    except OSError as exc:
      if exc.errno==errno.ENOENT:
        try: os.mkdir(component,0o700,dir_fd=directory)
        except FileExistsError: pass
        try: next_directory=os.open(component,directory_flags,dir_fd=directory)
        except OSError as retry_exc: fail(retry_exc,directory,component)
      else: fail(exc,directory,component)
    os.close(directory); directory=next_directory
  try: descriptor=os.open(parts[-1],file_flags,0o600,dir_fd=directory)
  except OSError as exc: fail(exc,directory,parts[-1])
  if not stat.S_ISREG(os.fstat(descriptor).st_mode): raise SystemExit(45)
  os.ftruncate(descriptor,0)
  data=sys.stdin.buffer.read()
  offset=0
  while offset<len(data): offset+=os.write(descriptor,data[offset:])
finally:
  if descriptor is not None: os.close(descriptor)
  os.close(directory)
"""
_WORKSPACE_LIST_SCRIPT = r"""
import errno,json,os,stat,sys
parts=[] if sys.argv[1] in ('','.') else sys.argv[1].split('/')
recursive=sys.argv[2]=='1'
directory_flags=os.O_RDONLY|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
def fail(exc,parent,name):
  if exc.errno==errno.ENOENT: raise SystemExit(44)
  if exc.errno==errno.ELOOP: raise SystemExit(47)
  if exc.errno==errno.ENOTDIR:
    try: mode=os.stat(name,dir_fd=parent,follow_symlinks=False).st_mode
    except OSError: raise SystemExit(48)
    raise SystemExit(47 if stat.S_ISLNK(mode) else 48)
  raise exc
directory=os.open('/workspace',directory_flags)
try:
  for component in parts:
    try: next_directory=os.open(component,directory_flags,dir_fd=directory)
    except OSError as exc: fail(exc,directory,component)
    os.close(directory); directory=next_directory
  result=[]
  def walk(parent,prefix):
    for name in sorted(os.listdir(parent)):
      try: metadata=os.stat(name,dir_fd=parent,follow_symlinks=False)
      except OSError: continue
      if stat.S_ISLNK(metadata.st_mode): continue
      relative='/'.join((*prefix,name))
      if stat.S_ISDIR(metadata.st_mode):
        result.append({'path':relative,'kind':'directory','size_bytes':0})
        if recursive:
          try: child=os.open(name,directory_flags,dir_fd=parent)
          except OSError: continue
          try: walk(child,(*prefix,name))
          finally: os.close(child)
      else:
        result.append({'path':relative,'kind':'file','size_bytes':metadata.st_size})
  walk(directory,tuple(parts))
  print(json.dumps(result,separators=(',',':')))
finally: os.close(directory)
"""
_MAX_BRIDGE_MESSAGE_BYTES = 64 * 1024


class _BridgeHandler(Protocol):
    async def handle(self, request: bytes) -> bytes: ...


class _DockerCommandError(ExecutionUnavailable):
    def __init__(self, return_code: int, stderr: bytes = b"") -> None:
        detail = stderr[-2048:].decode("utf-8", errors="replace").strip()
        message = "container runtime operation failed"
        if detail:
            message += f": {detail}"
        super().__init__(message)
        self.return_code = return_code
        self.stderr = stderr[-2048:]


async def _docker(
    *arguments: str,
    stdin: bytes | None = None,
    timeout_seconds: float = _DOCKER_COMMAND_TIMEOUT_SECONDS,
) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *arguments,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ExecutionUnavailable("container runtime is unavailable") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout_seconds)
    except TimeoutError as exc:
        process.kill()
        await asyncio.gather(process.wait(), return_exceptions=True)
        raise ExecutionUnavailable("container runtime operation timed out") from exc
    except asyncio.CancelledError:
        process.kill()
        await asyncio.gather(process.wait(), return_exceptions=True)
        raise
    if process.returncode != 0:
        if process.returncode is None:
            raise ExecutionUnavailable("container runtime did not report an exit status")
        raise _DockerCommandError(process.returncode, stderr)
    return stdout


async def _bounded_pipe(
    source: asyncio.StreamReader,
    maximum_bytes: int,
    shared_budget: _CaptureBudget,
    any_exceeded: asyncio.Event,
    this_exceeded: asyncio.Event,
) -> bytes:
    captured = bytearray()
    while chunk := await source.read(64 * 1024):
        remaining = min(maximum_bytes - len(captured), shared_budget.remaining)
        if remaining > 0:
            accepted = chunk[:remaining]
            captured.extend(accepted)
            shared_budget.remaining -= len(accepted)
        if len(chunk) > remaining:
            this_exceeded.set()
            any_exceeded.set()
    return bytes(captured)


@dataclass(slots=True)
class _CaptureBudget:
    remaining: int


@dataclass(slots=True)
class _DockerState:
    specification: EnvironmentSpec
    container_id: str
    volume_name: str
    network_name: str | None = None
    proxy_container_id: str | None = None
    provenance: dict[PurePosixPath, WorkspaceProvenance] = field(default_factory=dict)


class DockerWorkspaceHandle:
    def __init__(self, owner: DockerExecutionEnvironment, handle: EnvironmentHandle) -> None:
        self._owner = owner
        self._handle = handle

    @property
    def root(self) -> PurePosixPath:
        return _VIRTUAL_ROOT

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath:
        return _VIRTUAL_ROOT.joinpath(*validated_workspace_components(path))

    async def read(self, path: str) -> bytes:
        return await self._read(path, None)

    async def read_bounded(self, path: str, maximum_bytes: int) -> bytes:
        if maximum_bytes < 0:
            raise ValueError("maximum_bytes must not be negative")
        return await self._read(path, maximum_bytes)

    async def _read(self, path: str, maximum_bytes: int | None) -> bytes:
        relative = self.resolve(path).relative_to(_VIRTUAL_ROOT).as_posix()
        state = self._owner._state(self._handle)
        limit = -1 if maximum_bytes is None else maximum_bytes
        try:
            return await _docker(
                "exec",
                state.container_id,
                "python",
                "-c",
                _WORKSPACE_READ_SCRIPT,
                relative,
                str(limit),
            )
        except _DockerCommandError as exc:
            if exc.return_code == 44:
                raise FileNotFoundError(path) from exc
            if exc.return_code == 45:
                raise IsADirectoryError(path) from exc
            if exc.return_code == 46:
                raise WorkspaceReadLimitExceededError("workspace file exceeds read limit") from exc
            if exc.return_code == 47:
                raise WorkspaceEscape("workspace path resolves through a symlink") from exc
            if exc.return_code == 48:
                raise NotADirectoryError(path) from exc
            raise

    async def stream(self, path: str, maximum_bytes: int) -> AsyncIterator[bytes]:
        if maximum_bytes < 0:
            raise ValueError("maximum_bytes must not be negative")
        relative = self.resolve(path).relative_to(_VIRTUAL_ROOT).as_posix()
        state = self._owner._state(self._handle)
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            state.container_id,
            "python",
            "-c",
            _WORKSPACE_READ_SCRIPT,
            relative,
            str(maximum_bytes),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if process.stdout is None:
            process.kill()
            await asyncio.gather(process.wait(), return_exceptions=True)
            raise ExecutionUnavailable("container runtime did not provide an output pipe")
        observed = 0
        try:
            while chunk := await asyncio.wait_for(
                process.stdout.read(64 * 1024), _DOCKER_COMMAND_TIMEOUT_SECONDS
            ):
                observed += len(chunk)
                if observed > maximum_bytes:
                    process.kill()
                    await process.wait()
                    raise WorkspaceReadLimitExceededError("workspace file exceeds read limit")
                yield chunk
            return_code = await asyncio.wait_for(process.wait(), _DOCKER_COMMAND_TIMEOUT_SECONDS)
            if return_code == 44:
                raise FileNotFoundError(path)
            if return_code == 45:
                raise IsADirectoryError(path)
            if return_code == 46:
                raise WorkspaceReadLimitExceededError("workspace file exceeds read limit")
            if return_code == 47:
                raise WorkspaceEscape("workspace path resolves through a symlink")
            if return_code == 48:
                raise NotADirectoryError(path)
            if return_code != 0:
                raise ExecutionUnavailable("container workspace stream failed")
        except TimeoutError as exc:
            raise ExecutionUnavailable("container workspace stream timed out") from exc
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.gather(process.wait(), return_exceptions=True)

    async def write(self, path: str, data: bytes) -> None:
        relative = self.resolve(path).relative_to(_VIRTUAL_ROOT).as_posix()
        state = self._owner._state(self._handle)
        try:
            await _docker(
                "exec",
                "-i",
                state.container_id,
                "python",
                "-c",
                _WORKSPACE_WRITE_SCRIPT,
                relative,
                stdin=data,
            )
        except _DockerCommandError as exc:
            if exc.return_code == 45:
                raise IsADirectoryError(path) from exc
            if exc.return_code == 47:
                raise WorkspaceEscape("workspace path resolves through a symlink") from exc
            if exc.return_code == 48:
                raise NotADirectoryError(path) from exc
            raise
        state.provenance[PurePosixPath(relative)] = WorkspaceProvenance.TOOL_WRITTEN

    async def listdir(self, path: str, *, recursive: bool = False) -> tuple[WorkspaceEntry, ...]:
        relative = self.resolve(path).relative_to(_VIRTUAL_ROOT).as_posix()
        state = self._owner._state(self._handle)
        try:
            raw_payload = await _docker(
                "exec",
                state.container_id,
                "python",
                "-c",
                _WORKSPACE_LIST_SCRIPT,
                relative,
                "1" if recursive else "0",
            )
        except _DockerCommandError as exc:
            if exc.return_code == 44:
                raise FileNotFoundError(path) from exc
            if exc.return_code in {45, 48}:
                raise NotADirectoryError(path) from exc
            if exc.return_code == 47:
                raise WorkspaceEscape("workspace path resolves through a symlink") from exc
            raise
        payload = json.loads(raw_payload.decode("utf-8"))
        return tuple(
            WorkspaceEntry(
                path=PurePosixPath(item["path"]),
                kind=str(item["kind"]),
                size_bytes=int(item["size_bytes"]),
            )
            for item in payload
        )

    async def provenance(self, path: str) -> WorkspaceProvenance:
        relative = self.resolve(path).relative_to(_VIRTUAL_ROOT)
        return self._owner._state(self._handle).provenance.get(
            relative, WorkspaceProvenance.UNKNOWN
        )


class DockerExecutionEnvironment:
    """Container-backed execution service with no host bind mounts or inherited env."""

    def __init__(
        self,
        clock: Clock,
        ids: IdFactory,
        *,
        runtime: str | None = None,
        hard_cap_seconds: int = 300,
        reaper_grace_seconds: int = 60,
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._runtime = runtime
        self._hard_cap_seconds = hard_cap_seconds
        self._reaper_grace_seconds = reaper_grace_seconds
        self._states: dict[str, _DockerState] = {}
        self._lock = asyncio.Lock()

    async def provision(self, specification: EnvironmentSpec) -> EnvironmentHandle:
        if not specification.image_digest.startswith("sha256:"):
            raise ExecutionRejected("sandbox image must be an immutable sha256 digest")
        environment_id = str(self._ids.new_id())
        volume = f"agent-ws-{environment_id}"
        container_name = f"agent-sbx-{environment_id}"
        network_name: str | None = None
        proxy_container: str | None = None
        now = self._clock.now()
        expires_at = now + timedelta(
            seconds=min(self._hard_cap_seconds, specification.limits.wall_clock_seconds)
        )
        labels = (
            "--label",
            "agent.core.sandbox=true",
            "--label",
            f"agent.core.environment_id={environment_id}",
            "--label",
            f"agent.core.run_id={specification.run_id}",
            "--label",
            f"agent.core.lease_epoch={specification.lease_epoch}",
            "--label",
            f"agent.core.expires_at={int(expires_at.timestamp())}",
            "--label",
            f"agent.core.created_at={int(now.timestamp())}",
        )
        await _docker("volume", "create", *labels, volume)
        try:
            await _docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "65534:65534",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                f"type=volume,source={volume},target=/workspace",
                specification.image_digest,
                "sh",
                "-c",
                (
                    "mkdir -p /workspace/.agent && "
                    "touch /workspace/.agent-initialized && chmod 0700 /workspace/.agent"
                ),
            )
            arguments = [
                "create",
                "--name",
                container_name,
                *labels,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65534:65534",
                "--pids-limit",
                str(specification.limits.pids_max),
                "--memory",
                str(specification.limits.memory_bytes),
                "--cpus",
                str(specification.limits.cpu_millicores / 1000),
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=67108864",
                "--mount",
                f"type=volume,source={volume},target=/workspace",
            ]
            if specification.egress.mode.value == "allowlist":
                network_name = f"agent-net-{environment_id}"
                proxy_name = f"agent-proxy-{environment_id}"
                await _docker("network", "create", "--internal", network_name)
                policy = json.dumps(
                    {
                        "mode": specification.egress.mode.value,
                        "destinations": [
                            {"host": item.host, "ports": sorted(item.ports)}
                            for item in specification.egress.destinations
                        ],
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                proxy_container = (
                    (
                        await _docker(
                            "run",
                            "--detach",
                            "--name",
                            proxy_name,
                            "--network",
                            network_name,
                            "--network-alias",
                            "egress-proxy",
                            "--read-only",
                            "--cap-drop",
                            "ALL",
                            "--security-opt",
                            "no-new-privileges",
                            "--user",
                            "65534:65534",
                            "--env",
                            f"AGENT_EGRESS_POLICY={policy}",
                            "--env",
                            f"AGENT_TENANT_ID={specification.tenant_id}",
                            "--env",
                            f"AGENT_RUN_ID={specification.run_id}",
                            "--env",
                            "AGENT_PROXY_BIND_HOST=egress-proxy",
                            specification.image_digest,
                            "python",
                            "-m",
                            "agent_core.execution.proxy",
                        )
                    )
                    .decode("ascii")
                    .strip()
                )
                await _docker(
                    "network",
                    "connect",
                    "bridge",
                    proxy_container,
                )
                arguments.extend(("--network", network_name))
            else:
                arguments.extend(("--network", "none"))
            if self._runtime is not None:
                arguments.extend(("--runtime", self._runtime))
            arguments.extend((specification.image_digest, "sleep", "infinity"))
            container_id = (await _docker(*arguments)).decode("ascii").strip()
            await _docker("start", container_id)
            if proxy_container is not None:
                await self._wait_for_proxy(container_id)
        except BaseException:
            await self._discard(container_name, volume, proxy_container, network_name)
            raise
        handle = EnvironmentHandle(
            environment_id=environment_id,
            tenant_id=specification.tenant_id,
            run_id=specification.run_id,
            lease_epoch=specification.lease_epoch,
            created_at=now,
            expires_at=expires_at,
        )
        async with self._lock:
            self._states[environment_id] = _DockerState(
                specification, container_id, volume, network_name, proxy_container
            )
        return handle

    def workspace(self, environment: EnvironmentHandle) -> DockerWorkspaceHandle:
        self._state(environment)
        return DockerWorkspaceHandle(self, environment)

    async def _snapshot(self, state: _DockerState) -> dict[str, tuple[int, str]]:
        raw = await _docker("exec", state.container_id, "python", "-c", _SNAPSHOT_SCRIPT)
        parsed = json.loads(raw.decode("utf-8"))
        return {str(path): (int(value[0]), str(value[1])) for path, value in parsed.items()}

    @staticmethod
    async def _wait_for_proxy(container_id: str) -> None:
        for _attempt in range(30):
            try:
                await _docker("exec", container_id, "python", "-c", _PROXY_READY_SCRIPT)
                return
            except _DockerCommandError:
                await asyncio.sleep(0.1)
        raise ExecutionUnavailable("sandbox egress proxy did not become ready")

    @staticmethod
    async def _monitor_workspace_limits(
        state: _DockerState, stop: asyncio.Event
    ) -> KillReason | None:
        while not stop.is_set():
            try:
                raw = await _docker(
                    "exec", state.container_id, "python", "-c", _WORKSPACE_USAGE_SCRIPT
                )
            except ExecutionUnavailable:
                if stop.is_set():
                    return None
                await asyncio.sleep(0.1)
                continue
            try:
                size, inodes = (int(value) for value in raw.decode("ascii").split())
            except (UnicodeDecodeError, ValueError):
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=0.1)
                continue
            if (
                size > state.specification.limits.workspace_bytes
                or inodes > state.specification.limits.inodes_max
            ):
                return KillReason.DISK
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.1)
        return None

    @staticmethod
    async def _bridge_pump(process: asyncio.subprocess.Process, handler: _BridgeHandler) -> None:
        if process.stdout is None or process.stdin is None:
            raise ExecutionUnavailable("bridge relay did not provide transport pipes")
        while True:
            try:
                request = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                process.stdin.write(
                    b'{"status":"denied","reason_code":"bridge.request_too_large",'
                    b'"retryable":false}\n'
                )
                await process.stdin.drain()
                return
            if not request:
                return
            try:
                response = await handler.handle(request.rstrip(b"\n"))
            except Exception:
                response = (
                    b'{"status":"unavailable","reason_code":"bridge.internal_error",'
                    b'"retryable":false}'
                )
            if len(response) > _MAX_BRIDGE_MESSAGE_BYTES:
                response = (
                    b'{"status":"denied","reason_code":"bridge.response_too_large",'
                    b'"retryable":false}'
                )
            process.stdin.write(response + b"\n")
            await process.stdin.drain()

    @staticmethod
    async def _wait_for_bridge(container_id: str, socket_path: PurePosixPath) -> None:
        for _attempt in range(30):
            try:
                await _docker(
                    "exec",
                    container_id,
                    "python",
                    "-c",
                    _BRIDGE_READY_SCRIPT,
                    str(socket_path),
                )
                return
            except _DockerCommandError:
                await asyncio.sleep(0.1)
        raise ExecutionUnavailable("sandbox tool bridge did not become ready")

    async def execute_with_bridge(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
        endpoint: BridgeEndpoint,
        handler: _BridgeHandler,
    ) -> ExecutionResult:
        state = self._state(environment)
        relay = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            "--user",
            "65534:65534",
            state.container_id,
            "env",
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH=/opt/agent",
            f"AGENT_TOOL_BRIDGE_SOCKET={endpoint.socket_path}",
            "python",
            "-m",
            "agent_core.execution.bridge_relay",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 + 1,
        )
        if relay.stdin is None:
            relay.terminate()
            await relay.wait()
            raise ExecutionUnavailable("bridge relay did not provide an input pipe")
        relay.stdin.write(endpoint.token.encode("utf-8") + b"\n")
        await relay.stdin.drain()
        pump = asyncio.create_task(self._bridge_pump(relay, handler))
        try:
            await self._wait_for_bridge(state.container_id, endpoint.socket_path)
            return await self._execute(environment, command, bridge=endpoint)
        finally:
            with suppress(ExecutionUnavailable):
                await _docker(
                    "exec",
                    state.container_id,
                    "python",
                    "-c",
                    _BRIDGE_STOP_SCRIPT,
                    str(endpoint.socket_path),
                )
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(relay.wait(), timeout=1)
            if relay.returncode is None:
                relay.terminate()
                try:
                    await asyncio.wait_for(relay.wait(), timeout=1)
                except TimeoutError:
                    relay.kill()
                    await asyncio.gather(relay.wait(), return_exceptions=True)
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    async def execute(
        self, environment: EnvironmentHandle, command: ExecutionCommand
    ) -> ExecutionResult:
        return await self._execute(environment, command)

    async def _execute(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
        *,
        bridge: BridgeEndpoint | None = None,
    ) -> ExecutionResult:
        state = self._state(environment)
        if not command.argv or any("\x00" in item for item in command.argv):
            raise ExecutionRejected("command must be a non-empty NUL-free argument vector")
        raw_working = str(command.working_directory)
        working = PurePosixPath(
            *(() if raw_working == "." else validated_workspace_components(raw_working))
        )
        before = await self._snapshot(state)
        started = self._clock.now()
        effective_timeout = min(
            self._hard_cap_seconds,
            state.specification.limits.wall_clock_seconds,
            command.timeout_seconds,
            max(1, int((environment.expires_at - self._clock.now()).total_seconds())),
        )
        child_environment = dict(state.specification.environment)
        if bridge is not None:
            child_environment.update(
                {
                    "AGENT_TOOL_BRIDGE_SOCKET": str(bridge.socket_path),
                }
            )
        if state.specification.egress.mode.value == "allowlist":
            child_environment.update(
                {
                    "HTTP_PROXY": "http://egress-proxy:3128",
                    "HTTPS_PROXY": "http://egress-proxy:3128",
                    "NO_PROXY": "",
                }
            )
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            "-w",
            str(_VIRTUAL_ROOT / working),
            state.container_id,
            "env",
            "-i",
            *(f"{name}={value}" for name, value in sorted(child_environment.items())),
            *command.argv,
            stdin=(
                asyncio.subprocess.PIPE if command.stdin is not None else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise ExecutionUnavailable("container runtime did not provide output pipes")
        if command.stdin is not None:
            if process.stdin is None:
                raise ExecutionUnavailable("container runtime did not provide an input pipe")
            process.stdin.write(command.stdin)
            await process.stdin.drain()
            process.stdin.close()
        exceeded = asyncio.Event()
        stdout_exceeded = asyncio.Event()
        stderr_exceeded = asyncio.Event()
        capture_budget = _CaptureBudget(command.maximum_output_bytes)
        stdout_task = asyncio.create_task(
            _bounded_pipe(
                process.stdout,
                command.maximum_output_bytes,
                capture_budget,
                exceeded,
                stdout_exceeded,
            )
        )
        stderr_task = asyncio.create_task(
            _bounded_pipe(
                process.stderr,
                command.maximum_output_bytes,
                capture_budget,
                exceeded,
                stderr_exceeded,
            )
        )
        wait_task = asyncio.create_task(process.wait())
        exceeded_task = asyncio.create_task(exceeded.wait())
        disk_monitor_stop = asyncio.Event()
        disk_task = asyncio.create_task(self._monitor_workspace_limits(state, disk_monitor_stop))
        timed_out = False
        killed_by: KillReason | None = None
        cancellation: asyncio.CancelledError | None = None
        container_stopped = False
        try:
            try:
                done, _pending = await asyncio.wait(
                    {wait_task, exceeded_task, disk_task},
                    timeout=effective_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if exceeded_task in done and exceeded.is_set():
                    killed_by = KillReason.OUTPUT_LIMIT
                elif disk_task in done and disk_task.result() is KillReason.DISK:
                    killed_by = KillReason.DISK
                elif wait_task not in done:
                    timed_out = True
                    killed_by = KillReason.TIMEOUT
            except asyncio.CancelledError as exc:
                killed_by = KillReason.CANCELLED
                cancellation = exc
            if killed_by is not None:
                await _docker("kill", state.container_id)
                container_stopped = True
                with suppress(ProcessLookupError):
                    process.kill()
            disk_monitor_stop.set()
            await wait_task
            if container_stopped:
                await _docker("start", state.container_id)
                container_stopped = False
            monitored_kill = await disk_task
            if killed_by is None and monitored_kill is not None:
                killed_by = monitored_kill
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            if cancellation is not None:
                raise cancellation
            stdout_truncated = stdout_exceeded.is_set()
            stderr_truncated = stderr_exceeded.is_set()
            exit_code = process.returncode
            if killed_by is not None:
                exit_code = None
            elif exit_code in {137, 139}:
                killed_by = KillReason.MEMORY
                exit_code = None
            after = {} if killed_by is not None else await self._snapshot(state)
            workspace_size = sum(item[0] for item in after.values())
            if killed_by is None and (
                workspace_size > state.specification.limits.workspace_bytes
                or len(after) > state.specification.limits.inodes_max
            ):
                killed_by = KillReason.DISK
                exit_code = None
                await _docker("kill", state.container_id)
                container_stopped = True
                await _docker("start", state.container_id)
                container_stopped = False
            if (
                killed_by is None
                and process.returncode != 0
                and (b"Resource temporarily unavailable" in stderr or b"can't fork" in stderr)
            ):
                killed_by = KillReason.PIDS
                exit_code = None
            changes = self._changes(before, after)
            for change in changes:
                if change.change is not ChangeKind.DELETED:
                    state.provenance[change.path] = WorkspaceProvenance.SANDBOX_WRITTEN
            return ExecutionResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                timed_out=timed_out,
                killed_by=killed_by,
                files_changed=changes[:1000],
                duration_ms=max(0, int((self._clock.now() - started).total_seconds() * 1000)),
            )
        finally:
            disk_monitor_stop.set()
            for task in (stdout_task, stderr_task, wait_task, exceeded_task, disk_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
                exceeded_task,
                disk_task,
                return_exceptions=True,
            )
            if process.returncode is None:
                try:
                    await _docker("kill", state.container_id)
                except ExecutionUnavailable:
                    pass
                else:
                    container_stopped = True
                with suppress(ProcessLookupError):
                    process.kill()
                await asyncio.gather(process.wait(), return_exceptions=True)
            if container_stopped:
                with suppress(ExecutionUnavailable):
                    await _docker("start", state.container_id)

    @staticmethod
    def _changes(
        before: dict[str, tuple[int, str]], after: dict[str, tuple[int, str]]
    ) -> tuple[FileChange, ...]:
        result: list[FileChange] = []
        for path in sorted(before.keys() | after.keys()):
            if path not in after:
                result.append(FileChange(PurePosixPath(path), ChangeKind.DELETED, 0, None))
            elif path not in before:
                result.append(FileChange(PurePosixPath(path), ChangeKind.CREATED, *after[path]))
            elif before[path] != after[path]:
                result.append(FileChange(PurePosixPath(path), ChangeKind.MODIFIED, *after[path]))
        return tuple(result)

    async def destroy(self, environment: EnvironmentHandle) -> None:
        async with self._lock:
            state = self._states.pop(environment.environment_id, None)
        if state is not None:
            await self._discard(
                state.container_id,
                state.volume_name,
                state.proxy_container_id,
                state.network_name,
            )

    async def egress_log(self, environment: EnvironmentHandle) -> tuple[dict[str, object], ...]:
        state = self._state(environment)
        if state.proxy_container_id is None:
            return ()
        raw = await _docker("logs", state.proxy_container_id)
        return tuple(
            json.loads(line)
            for line in raw.decode("utf-8").splitlines()
            if line.strip().startswith("{")
        )

    async def reap(self, live_leases: frozenset[tuple[object, int]]) -> int:
        raw = await _docker(
            "ps",
            "--all",
            "--filter",
            "label=agent.core.sandbox=true",
            "--format",
            (
                '{{.ID}}\t{{.Label "agent.core.environment_id"}}\t'
                '{{.Label "agent.core.run_id"}}\t{{.Label "agent.core.lease_epoch"}}\t'
                '{{.Label "agent.core.expires_at"}}'
                '\t{{.Label "agent.core.created_at"}}'
            ),
        )
        reaped = 0
        now_epoch = int(self._clock.now().timestamp())
        for line in raw.decode("utf-8").splitlines():
            try:
                (
                    container_id,
                    environment_id,
                    raw_run_id,
                    raw_epoch,
                    raw_expiry,
                    raw_created,
                ) = line.split("\t")
                run_id = UUID(raw_run_id)
                lease_epoch = int(raw_epoch)
                expired = int(raw_expiry) <= now_epoch
                old_enough = (
                    not raw_created or int(raw_created) + self._reaper_grace_seconds <= now_epoch
                )
            except (ValueError, TypeError):
                continue
            if not expired and (run_id, lease_epoch) in live_leases:
                continue
            if not expired and not old_enough:
                continue
            state = self._states.get(environment_id)
            if state is not None:
                handle = EnvironmentHandle(
                    environment_id=environment_id,
                    tenant_id=state.specification.tenant_id,
                    run_id=state.specification.run_id,
                    lease_epoch=state.specification.lease_epoch,
                    created_at=self._clock.now(),
                    expires_at=self._clock.now(),
                )
                await self.destroy(handle)
            else:
                await self._discard(
                    container_id,
                    f"agent-ws-{environment_id}",
                    f"agent-proxy-{environment_id}",
                    f"agent-net-{environment_id}",
                )
            reaped += 1
        return reaped

    def live_environment_ids(self) -> frozenset[str]:
        return frozenset(self._states)

    def _state(self, handle: EnvironmentHandle) -> _DockerState:
        state = self._states.get(handle.environment_id)
        if state is None:
            raise ExecutionRejected("execution environment is gone")
        spec = state.specification
        if (spec.tenant_id, spec.run_id, spec.lease_epoch) != (
            handle.tenant_id,
            handle.run_id,
            handle.lease_epoch,
        ):
            raise ExecutionRejected("execution handle does not match its environment")
        return state

    @staticmethod
    async def _discard(
        container: str,
        volume: str,
        proxy_container: str | None = None,
        network: str | None = None,
    ) -> None:
        operations: list[tuple[str, ...]] = [("rm", "--force", container)]
        if proxy_container is not None:
            operations.append(("rm", "--force", proxy_container))
        operations.append(("volume", "rm", "--force", volume))
        if network is not None:
            operations.append(("network", "rm", network))
        for arguments in operations:
            with suppress(ExecutionUnavailable):
                await _docker(*arguments)


async def resolve_local_image_digest(reference: str) -> str:
    raw = await _docker("image", "inspect", reference, "--format", "{{.Id}}")
    digest = raw.decode("ascii").strip()
    if not digest.startswith("sha256:"):
        raise ExecutionUnavailable("sandbox image did not resolve to a sha256 digest")
    return digest
