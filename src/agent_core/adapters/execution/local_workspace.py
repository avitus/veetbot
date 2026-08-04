"""Local development implementation of the contained workspace handle."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

from agent_core.domain.errors import WorkspaceEscape
from agent_core.domain.execution import WorkspaceEntry, WorkspaceProvenance
from agent_core.ports.execution import WorkspaceHandle

_VIRTUAL_ROOT = PurePosixPath("/workspace")


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
        target = self._host_path(path)
        return await asyncio.to_thread(target.read_bytes)

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
        self._handles: dict[tuple[str, UUID], LocalWorkspaceHandle] = {}

    def for_run(self, tenant_id: str, run_id: object) -> WorkspaceHandle:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        key = (tenant_id, run_id)
        if key not in self._handles:
            self._handles[key] = LocalWorkspaceHandle(
                self._base_directory / tenant_key / str(run_id)
            )
        return self._handles[key]
