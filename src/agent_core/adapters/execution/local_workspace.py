"""Local development implementation of the contained workspace handle."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from uuid import UUID

from agent_core.domain.errors import WorkspaceEscape, WorkspaceReadLimitExceededError
from agent_core.domain.execution import WorkspaceEntry, WorkspaceProvenance
from agent_core.ports.execution import WorkspaceHandle

_VIRTUAL_ROOT = PurePosixPath("/workspace")


def validated_workspace_components(path: str | PurePosixPath) -> tuple[str, ...]:
    raw = str(path)
    if "\x00" in raw or raw.startswith("/"):
        raise WorkspaceEscape("workspace path must be relative and contain no NUL")
    if raw == "":
        return ()
    components = tuple(raw.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise WorkspaceEscape("workspace path contains a forbidden component")
    if any(len(component.encode("utf-8")) > 255 for component in components):
        raise WorkspaceEscape("workspace path component exceeds 255 bytes")
    if len(raw.encode("utf-8")) > 4096:
        raise WorkspaceEscape("workspace path exceeds 4096 bytes")
    return components


class LocalWorkspaceHandle:
    def __init__(self, host_root: Path) -> None:
        self._host_root = host_root.resolve()
        self._host_root.mkdir(parents=True, exist_ok=True)
        self._provenance: dict[PurePosixPath, WorkspaceProvenance] = {}

    @property
    def root(self) -> PurePosixPath:
        return _VIRTUAL_ROOT

    @staticmethod
    def _components(path: str | PurePosixPath) -> tuple[str, ...]:
        return validated_workspace_components(path)

    def _host_path(self, path: str | PurePosixPath) -> Path:
        components = self._components(path)
        candidate = self._host_root.joinpath(*components)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self._host_root)
        except ValueError as exc:
            raise WorkspaceEscape("workspace path escapes its root") from exc
        return resolved

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath:
        components = self._components(path)
        self._host_path(path)
        return _VIRTUAL_ROOT.joinpath(*components)

    async def read(self, path: str) -> bytes:
        def _read() -> bytes:
            descriptor = self._open_regular(path)
            with os.fdopen(descriptor, "rb") as source:
                return source.read()

        return await asyncio.to_thread(_read)

    async def read_bounded(self, path: str, maximum_bytes: int) -> bytes:
        if maximum_bytes < 0:
            raise ValueError("maximum_bytes must not be negative")

        def _read() -> bytes:
            descriptor = self._open_regular(path)
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_size > maximum_bytes:
                    raise WorkspaceReadLimitExceededError("workspace file exceeds the read limit")
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    data = source.read(maximum_bytes + 1)
            finally:
                os.close(descriptor)
            if len(data) > maximum_bytes:
                raise WorkspaceReadLimitExceededError("workspace file exceeds the read limit")
            return data

        return await asyncio.to_thread(_read)

    async def stream(self, path: str, maximum_bytes: int) -> AsyncIterator[bytes]:
        if maximum_bytes < 0:
            raise ValueError("maximum_bytes must not be negative")
        descriptor = await asyncio.to_thread(self._open_regular, path)
        source = os.fdopen(descriptor, "rb")
        size = 0
        try:
            metadata = await asyncio.to_thread(os.fstat, source.fileno())
            if metadata.st_size > maximum_bytes:
                raise WorkspaceReadLimitExceededError("workspace file exceeds the read limit")
            while chunk := await asyncio.to_thread(source.read, 64 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise WorkspaceReadLimitExceededError("workspace file exceeds the read limit")
                yield chunk
        finally:
            await asyncio.to_thread(source.close)

    def _open_regular(self, path: str | PurePosixPath) -> int:
        components = self._components(path)
        if not components:
            raise IsADirectoryError(str(path))
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory = os.open(self._host_root, directory_flags)
        current_component = components[-1]
        try:
            try:
                for component in components[:-1]:
                    current_component = component
                    next_directory = os.open(component, directory_flags, dir_fd=directory)
                    os.close(directory)
                    directory = next_directory
                current_component = components[-1]
                descriptor = os.open(components[-1], file_flags, dir_fd=directory)
            except OSError as exc:
                symlink_component = exc.errno == errno.ELOOP
                if exc.errno == errno.ENOTDIR:
                    try:
                        metadata = os.stat(
                            current_component,
                            dir_fd=directory,
                            follow_symlinks=False,
                        )
                    except OSError:
                        metadata = None
                    symlink_component = metadata is not None and stat.S_ISLNK(metadata.st_mode)
                if symlink_component:
                    raise WorkspaceEscape("workspace path resolves through a symlink") from exc
                raise
        finally:
            os.close(directory)
        try:
            metadata = os.fstat(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise IsADirectoryError(str(path))
        return descriptor

    async def write(self, path: str, data: bytes) -> None:
        target = self._host_path(path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            temporary.write_bytes(data)
            temporary.replace(target)

        await asyncio.to_thread(_write)
        virtual = self.resolve(path).relative_to(_VIRTUAL_ROOT)
        self._provenance[virtual] = WorkspaceProvenance.TOOL_WRITTEN

    async def listdir(self, path: str, *, recursive: bool = False) -> tuple[WorkspaceEntry, ...]:
        target = self._host_path(path)
        relative_base = self.resolve(path).relative_to(_VIRTUAL_ROOT)

        def _list() -> tuple[WorkspaceEntry, ...]:
            if not target.is_dir():
                if target.exists():
                    raise NotADirectoryError(str(path))
                raise FileNotFoundError(str(path))
            iterator = target.rglob("*") if recursive else target.iterdir()
            entries: list[WorkspaceEntry] = []
            for item in iterator:
                if item.is_symlink():
                    continue
                item_relative = PurePosixPath(item.relative_to(target).as_posix())
                result_path = relative_base / item_relative
                entries.append(
                    WorkspaceEntry(
                        path=result_path,
                        kind="directory" if item.is_dir() else "file",
                        size_bytes=0 if item.is_dir() else item.stat().st_size,
                    )
                )
            return tuple(entries)

        return await asyncio.to_thread(_list)

    async def provenance(self, path: str) -> WorkspaceProvenance:
        relative = self.resolve(path).relative_to(_VIRTUAL_ROOT)
        return self._provenance.get(relative, WorkspaceProvenance.UNKNOWN)


class LocalWorkspaceFactory:
    def __init__(self, base_directory: Path) -> None:
        self._base_directory = base_directory
        self._handles: dict[tuple[str, UUID, int], LocalWorkspaceHandle] = {}

    def for_run(self, tenant_id: str, run_id: object, lease_epoch: int = 0) -> WorkspaceHandle:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        key = (tenant_id, run_id, lease_epoch)
        if key not in self._handles:
            self._handles[key] = LocalWorkspaceHandle(
                self._base_directory / tenant_key / str(run_id) / f"lease-{lease_epoch}"
            )
        return self._handles[key]
