"""Procedural-memory repository and package-store ports."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.skills import (
    AuthoringContext,
    LoadedSkillBody,
    SessionSkillCatalog,
    SkillPackage,
    SkillPackagePut,
    SkillRef,
    SkillRevision,
    SkillSource,
)


class SkillRepository(Protocol):
    async def install(
        self,
        tenant_id: str,
        package: SkillPackage,
        source: SkillSource,
        expected_revision: int | None,
        authored_by: AuthoringContext | None,
    ) -> SkillRevision: ...

    async def resolve(self, tenant_id: str, ref: SkillRef) -> SkillRevision: ...

    async def list_active(self, tenant_id: str, limit: int) -> list[SkillRevision]: ...

    async def archive(
        self,
        tenant_id: str,
        name: str,
        revision: int,
        authored_by: AuthoringContext | None = None,
    ) -> SkillRevision: ...


class SkillPackageStore(Protocol):
    async def put(
        self, tenant_id: str, skill_id: UUID, revision: int, archive: bytes
    ) -> SkillPackagePut: ...

    async def open_member(self, key: str, path: str) -> bytes: ...

    async def archive_bytes(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class SkillCatalog(Protocol):
    async def open(
        self,
        session_id: UUID,
        agent: AgentSpec,
        principal: Principal,
    ) -> SessionSkillCatalog: ...

    def current(self, session_id: UUID) -> SessionSkillCatalog: ...

    async def discard(self, session_id: UUID) -> None:
        """Forget uncommitted session catalog state."""
        ...

    async def load(
        self,
        session_id: UUID,
        principal: Principal,
        name: str,
        path: str | None,
        loaded: tuple[LoadedSkillBody, ...],
        available_tools: frozenset[str],
    ) -> tuple[LoadedSkillBody, tuple[str, ...]]: ...
