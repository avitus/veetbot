"""In-memory and filesystem stores for immutable skill archives."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath
from uuid import UUID

from agent_core.domain.errors import NotFoundError
from agent_core.domain.skills import SkillPackagePut
from agent_core.skills.package import read_archive_member


def skill_archive_key(tenant_id: str, skill_id: UUID, revision: int) -> str:
    if (
        not tenant_id
        or any(character in tenant_id for character in "/\\")
        or tenant_id in {".", ".."}
    ):
        raise ValueError("tenant id is not safe for an object key")
    if revision <= 0:
        raise ValueError("skill revision must be positive")
    return f"skills/{tenant_id}/{skill_id}/{revision}.tar.zst"


class InMemorySkillPackageStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put(
        self, tenant_id: str, skill_id: UUID, revision: int, archive: bytes
    ) -> SkillPackagePut:
        key = skill_archive_key(tenant_id, skill_id, revision)
        existing = self._objects.get(key)
        if existing is not None and existing != archive:
            raise ValueError("immutable skill archive key already contains different bytes")
        created = existing is None
        self._objects[key] = bytes(archive)
        return SkillPackagePut(key=key, created=created)

    async def open_member(self, key: str, path: str) -> bytes:
        return read_archive_member(await self.archive_bytes(key), path)

    async def archive_bytes(self, key: str) -> bytes:
        try:
            return bytes(self._objects[key])
        except KeyError as exc:
            raise NotFoundError("skill archive not found") from exc

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    def object_count(self) -> int:
        return len(self._objects)


class FilesystemSkillPackageStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _resolve(self, key: str) -> Path:
        relative = PurePosixPath(key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("skill archive key escapes its store")
        target = self._root.joinpath(*relative.parts).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("skill archive key escapes its store")
        return target

    async def put(
        self, tenant_id: str, skill_id: UUID, revision: int, archive: bytes
    ) -> SkillPackagePut:
        key = skill_archive_key(tenant_id, skill_id, revision)
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.{hashlib.sha256(archive).hexdigest()[:12]}-",
            delete=False,
        ) as staged:
            staged.write(archive)
            staged.flush()
            os.fsync(staged.fileno())
            temporary = Path(staged.name)
        try:
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != archive:
                    raise ValueError(
                        "immutable skill archive key already contains different bytes"
                    ) from None
                return SkillPackagePut(key=key, created=False)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return SkillPackagePut(key=key, created=True)
        finally:
            temporary.unlink(missing_ok=True)

    async def open_member(self, key: str, path: str) -> bytes:
        return read_archive_member(await self.archive_bytes(key), path)

    async def archive_bytes(self, key: str) -> bytes:
        target = self._resolve(key)
        if not target.is_file():
            raise NotFoundError("skill archive not found")
        return target.read_bytes()

    async def delete(self, key: str) -> None:
        target = self._resolve(key)
        target.unlink(missing_ok=True)
