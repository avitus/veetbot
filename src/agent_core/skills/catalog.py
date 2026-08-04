"""Deterministic session-pinned skill catalogs and content loading."""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from uuid import UUID
from weakref import WeakValueDictionary

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.skills import (
    CatalogEntry,
    LoadedSkillBody,
    SessionSkillCatalog,
    SkillPin,
    SkillRef,
    SkillSource,
)
from agent_core.ports.context import TokenEstimator
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.skills import SkillPackageStore
from agent_core.skills.package import read_archive_member

type MCPPromptSource = Callable[[UUID, Principal], Awaitable[list[CatalogEntry]]]


class SkillCatalogService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        package_store: SkillPackageStore,
        estimator: TokenEstimator,
        *,
        mcp_prompts: MCPPromptSource | None = None,
        maximum_entries: int = 20,
        maximum_tokens: int = 1_500,
        maximum_loaded: int = 2,
        maximum_body_tokens: int = 6_000,
        cache_capacity: int = 1_024,
    ) -> None:
        if cache_capacity <= 0:
            raise ValueError("skill catalog cache capacity must be positive")
        self._uow_factory = uow_factory
        self._package_store = package_store
        self._estimator = estimator
        self._mcp_prompts = mcp_prompts
        self._maximum_entries = maximum_entries
        self._maximum_tokens = maximum_tokens
        self._maximum_loaded = maximum_loaded
        self._maximum_body_tokens = maximum_body_tokens
        self._cache_capacity = cache_capacity
        self._catalogs: OrderedDict[UUID, SessionSkillCatalog] = OrderedDict()
        self._locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()

    def _lock(self, session_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _remember(self, session_id: UUID, catalog: SessionSkillCatalog) -> None:
        self._catalogs[session_id] = catalog.model_copy(deep=True)
        self._catalogs.move_to_end(session_id)
        if len(self._catalogs) > self._cache_capacity:
            self._catalogs.popitem(last=False)

    async def _recorded_pins(
        self,
        session_id: UUID,
        principal: Principal,
    ) -> tuple[SkillPin, ...] | None:
        try:
            async with self._uow_factory() as uow:
                created = await uow.events.latest_before(
                    session_id,
                    (1 << 63) - 1,
                    "session.created",
                    principal,
                )
        except NotFoundError:
            return None
        if created is None or "skill_pins" not in created.payload:
            return None
        raw_pins = created.payload["skill_pins"]
        if not isinstance(raw_pins, list):
            raise ConflictError("session skill pins are malformed")
        try:
            return tuple(SkillPin.model_validate(raw) for raw in raw_pins)
        except ValueError as exc:
            raise ConflictError("session skill pins are malformed") from exc

    def _text_tokens(self, text: str) -> int:
        return self._estimator.estimate_text(text, "skill-catalog")

    async def open(
        self,
        session_id: UUID,
        agent: AgentSpec,
        principal: Principal,
    ) -> SessionSkillCatalog:
        return await self._open(session_id, agent, principal)

    async def _open(
        self,
        session_id: UUID,
        agent: AgentSpec | None,
        principal: Principal,
    ) -> SessionSkillCatalog:
        cached = self._catalogs.get(session_id)
        if cached is not None:
            self._catalogs.move_to_end(session_id)
            return cached.model_copy(deep=True)
        async with self._lock(session_id):
            cached = self._catalogs.get(session_id)
            if cached is not None:
                self._catalogs.move_to_end(session_id)
                return cached.model_copy(deep=True)
            recorded_pins = await self._recorded_pins(session_id, principal)
            configured: list[CatalogEntry] = []
            revisions = []
            async with self._uow_factory() as uow:
                refs = (
                    [
                        SkillRef(name=pin.name, revision=pin.revision)
                        for pin in recorded_pins
                        if pin.revision > 0
                    ]
                    if recorded_pins is not None
                    else (
                        [SkillRef.parse(raw_ref) for raw_ref in agent.enabled_skills]
                        if agent is not None
                        else []
                    )
                )
                if recorded_pins is None and agent is None:
                    raise NotFoundError("session skill pins are unavailable")
                for ref in refs:
                    revision = await uow.skills.resolve(
                        principal.tenant_id,
                        ref,
                    )
                    revisions.append(revision)
            for revision in revisions:
                configured.append(
                    CatalogEntry(
                        manifest=revision.manifest,
                        revision=revision.revision,
                        content_sha256=revision.content_sha256,
                        trust=revision.trust,
                        source=revision.source,
                        package_key=revision.package_key,
                    )
                )
            remote = (
                [] if self._mcp_prompts is None else await self._mcp_prompts(session_id, principal)
            )
            if recorded_pins is not None:
                by_pin = {(entry.manifest.name, entry.content_sha256): entry for entry in remote}
                matched: list[CatalogEntry] = []
                missing_pins: list[str] = []
                for pin in recorded_pins:
                    if pin.revision != 0:
                        continue
                    entry = by_pin.get((pin.name, pin.content_sha256))
                    if entry is None:
                        missing_pins.append(pin.name)
                    else:
                        matched.append(entry)
                remote = matched
            else:
                missing_pins = []
            ordered = [*configured, *remote]
            kept: list[CatalogEntry] = []
            dropped: list[str] = [*missing_pins]
            used_tokens = 0
            for index, entry in enumerate(ordered):
                rendered = self.render_entry(entry)
                tokens = self._text_tokens(rendered)
                if (
                    len(kept) >= self._maximum_entries
                    or used_tokens + tokens > self._maximum_tokens
                ):
                    dropped.extend(item.manifest.name for item in ordered[index:])
                    break
                kept.append(entry)
                used_tokens += tokens
            catalog = SessionSkillCatalog(entries=tuple(kept), dropped_names=tuple(dropped))
            self._remember(session_id, catalog)
            return catalog.model_copy(deep=True)

    def current(self, session_id: UUID) -> SessionSkillCatalog:
        try:
            catalog = self._catalogs[session_id]
            self._catalogs.move_to_end(session_id)
            return catalog.model_copy(deep=True)
        except KeyError as exc:
            raise NotFoundError("session skill catalog is not open") from exc

    @staticmethod
    def render_entry(entry: CatalogEntry) -> str:
        required = ",".join(entry.manifest.required_tools) or "none"
        return (
            f"name={entry.manifest.name}; version={entry.manifest.version}; "
            f"revision={entry.revision}; description={entry.manifest.description}; "
            f"required_tools={required}"
        )

    async def load(
        self,
        session_id: UUID,
        principal: Principal,
        name: str,
        path: str | None,
        loaded: tuple[LoadedSkillBody, ...],
        available_tools: frozenset[str],
    ) -> tuple[LoadedSkillBody, tuple[str, ...]]:
        try:
            catalog = self.current(session_id)
        except NotFoundError:
            catalog = await self._open(session_id, None, principal)
        entry = next((item for item in catalog.entries if item.manifest.name == name), None)
        if entry is None:
            visible = ", ".join(item.manifest.name for item in catalog.entries) or "none"
            raise NotFoundError(
                f"skill {name!r} is not in the pinned catalog; available: {visible}"
            )
        existing = next((item for item in loaded if item.name == name), None)
        if existing is not None:
            missing = tuple(sorted(set(entry.manifest.required_tools) - available_tools))
            return existing.model_copy(deep=True), missing
        if len(loaded) >= self._maximum_loaded:
            names = ", ".join(item.name for item in loaded)
            raise ConflictError(f"loaded skill cap reached; unload one of: {names}")
        if entry.source is SkillSource.MCP:
            if path is not None:
                raise NotFoundError("MCP prompt skills have no package members")
            content = entry.ephemeral_body
            if content is None:
                raise ConflictError("MCP prompt content is unavailable for this session")
        else:
            if entry.package_key is None:
                raise ConflictError("pinned skill package key is unavailable")
            async with self._uow_factory() as uow:
                revision = await uow.skills.resolve(
                    principal.tenant_id,
                    SkillRef(name=name, revision=entry.revision),
                )
            if (
                revision.content_sha256 != entry.content_sha256
                or revision.package_key != entry.package_key
            ):
                raise ConflictError("pinned skill revision hash changed")
            archive = await self._package_store.archive_bytes(entry.package_key)
            if hashlib.sha256(archive).hexdigest() != entry.content_sha256:
                raise ConflictError("pinned skill archive hash does not match its revision")
            if path is None:
                content = revision.body
            else:
                raw = read_archive_member(archive, path)
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("skill member is not UTF-8 text") from exc
        tokens = self._text_tokens(content)
        if sum(item.tokens for item in loaded) + tokens > self._maximum_body_tokens:
            raise ConflictError(
                f"loaded skill bodies exceed the {self._maximum_body_tokens}-token cap"
            )
        body = LoadedSkillBody(
            name=name,
            revision=entry.revision,
            path=path,
            content=content,
            tokens=tokens,
            trust=entry.trust,
            content_sha256=entry.content_sha256,
        )
        missing = tuple(sorted(set(entry.manifest.required_tools) - available_tools))
        return body, missing
