"""Runtime-bound query vectors; explicit preparation and local-only recall."""

import asyncio
import hashlib
import inspect
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import Field

from agent_core.domain.agents import Principal
from agent_core.domain.local_embeddings import LocalEmbeddingConfig, local_execution
from agent_core.domain.memory import RecallQuery, Sensitivity
from agent_core.domain.runs import Run, RunCheckpoint, RunStatus
from agent_core.domain.semantic_work import EmbeddingConfig, SemanticValue
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.memory.semantic_queries import (
    CachedSemanticQuery,
    SemanticQueryContext,
    SemanticQueryPreparation,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.embeddings import EmbeddingProvider
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.semantic_memory import SemanticQueryLookup


class SemanticQueryAuthority(SemanticValue):
    """Trusted runtime authority; neither tool arguments nor a read-clearance label.

    Missing authority withholds semantic work. The composition must provide all
    fields from its effective policy and task-content classification, including
    the full retrieval-profile identity. The task digest is derived below.
    """

    policy_version: str = Field(min_length=1, max_length=128)
    retrieval_policy: str = Field(min_length=1, max_length=128)
    surface_id: str = Field(min_length=1, max_length=256)
    sensitivity_floor: Sensitivity


@dataclass(frozen=True)
class _Entry:
    run_id: UUID
    expires: datetime
    lookup: SemanticQueryPreparation


class RunSemanticQueries:
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        clock: Clock,
        principal: Principal,
        provider: EmbeddingProvider,
        config: EmbeddingConfig,
        *,
        admitted: Callable[[], bool],
        local: LocalEmbeddingConfig | None = None,
        authority: Callable[
            [Run, Session, RunCheckpoint],
            SemanticQueryAuthority | Awaitable[SemanticQueryAuthority | None] | None,
        ],
    ) -> None:
        self._factory, self._clock = factory, clock
        self._principal = principal.model_copy(deep=True)
        self._provider = provider
        self._config = EmbeddingConfig.model_validate(config.model_dump())
        self._local = local_execution(self._config, local)
        self._local_config = local
        self._admitted, self._authority = admitted, authority
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        # A single 128-vector cap across all run contexts, not 128 per run.
        self._vectors: OrderedDict[str, CachedSemanticQuery] = OrderedDict()

    def _enabled(self) -> bool:
        try:
            admitted = self._admitted()
        except Exception:
            admitted = False
        if not admitted:
            self._entries.clear()
            self._vectors.clear()
            return False
        return True

    async def _context(
        self, session_id: UUID, run_id: UUID, surface_id: str, *, require_search_budget: bool = True
    ) -> tuple[SemanticQueryContext, datetime] | None:
        if not self._enabled():
            return None
        async with self._factory() as uow:
            run = await uow.runs.get(run_id, self._principal)
            session = await uow.sessions.get(session_id, self._principal)
            checkpoint = await uow.checkpoints.latest(run_id)
        now = self._clock.now()
        if (
            run.session_id != session.id
            or run.tenant_id != self._principal.tenant_id
            or (session.tenant_id, session.principal_id)
            != (self._principal.tenant_id, self._principal.principal_id)
            or session.status != SessionStatus.ACTIVE
            or run.status != RunStatus.RUNNING
            or run.cancel_requested_at is not None
            or run.lease_owner is None
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
            or (run.deadline_at is not None and run.deadline_at <= now)
            or not run.principal_scopes <= self._principal.scopes
            or checkpoint is None
            or checkpoint.run_id != run.id
            or (
                require_search_budget
                and checkpoint.recall_work is not None
                and checkpoint.recall_work.deadline_at <= now
            )
        ):
            return None
        authority = self._authority(run, session, checkpoint)
        if inspect.isawaitable(authority):
            authority = await authority
            # Authority may need source I/O. Cancellation, fencing or a changed
            # task during that await must not return an earlier cached handle.
            async with self._factory() as uow:
                current_run = await uow.runs.get(run_id, self._principal)
                current_session = await uow.sessions.get(session_id, self._principal)
                current_checkpoint = await uow.checkpoints.latest(run_id)
            changing_counters = {
                "lease_expires_at",
                "updated_at",
                "usage",
                "model_call_count",
                "tool_call_count",
            }
            if (
                run.model_dump(exclude=changing_counters)
                != current_run.model_dump(exclude=changing_counters)
                or session.model_dump(exclude={"updated_at"})
                != current_session.model_dump(exclude={"updated_at"})
                or checkpoint != current_checkpoint
            ):
                return None
            run = current_run
        now = self._clock.now()
        if (
            authority is None
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
            or (run.deadline_at is not None and run.deadline_at <= now)
        ):
            return None
        authority = SemanticQueryAuthority.model_validate(authority.model_dump())
        if authority.surface_id != surface_id or not self._enabled():
            return None
        # Hash the entire checkpoint, including current message, working state,
        # provider/tool pins and compaction lineage. No task text is retained.
        # Mutable usage/heartbeat timestamps do not change a task identity.
        task = {
            "version": "semantic-runtime@1",
            "principal": {
                "tenant": self._principal.tenant_id,
                "id": self._principal.principal_id,
                "scopes": sorted(self._principal.scopes),
                "roles": sorted(self._principal.roles),
            },
            "session": {
                "id": str(session.id),
                "metadata": session.metadata,
                "agent_id": str(session.agent_id),
                "agent_version": session.agent_version,
            },
            "run": {
                "id": str(run.id),
                "parent": str(run.parent_run_id),
                "kind": run.kind,
                "agent_id": str(run.agent_id),
                "agent_version": run.agent_version,
                "step": run.step_count,
                "lease_epoch": run.lease_epoch,
                "lease_owner": run.lease_owner,
                "scopes": sorted(run.principal_scopes),
                "seed": run.seed_event_sequence,
                "limits": run.limits.model_dump(mode="json"),
                "provider_pin": None
                if run.provider_pin is None
                else run.provider_pin.model_dump(mode="json"),
            },
            "checkpoint": checkpoint.model_dump(mode="json"),
        }
        digest = hashlib.sha256(
            json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        expires = min(now + timedelta(seconds=300), run.lease_expires_at)
        if run.deadline_at is not None:
            expires = min(expires, run.deadline_at)
        if require_search_budget and checkpoint.recall_work is not None:
            expires = min(expires, checkpoint.recall_work.deadline_at)
        return SemanticQueryContext(**authority.model_dump(), task_sha256=digest), expires

    def _prune(self, run_id: UUID, key: str | None) -> None:
        for existing, entry in list(self._entries.items()):
            if self._clock.now() >= entry.expires or (entry.run_id == run_id and existing != key):
                entry.lookup.invalidate()
                del self._entries[existing]
        # Revoked handles cannot access vectors or publish more.

    async def _load(
        self, session_id: UUID, run_id: UUID, surface_id: str
    ) -> tuple[SemanticQueryContext, str, datetime] | None:
        try:
            binding = await self._context(session_id, run_id, surface_id)
        except Exception:
            binding = None
        context = None if binding is None else binding[0]
        key = (
            None
            if context is None
            else hashlib.sha256(context.model_dump_json().encode()).hexdigest()
        )
        self._prune(run_id, key)
        return (
            None
            if binding is None or context is None or key is None
            else (context, key, binding[1])
        )

    async def validate_authority(self, *, session_id: UUID, run_id: UUID, surface_id: str) -> bool:
        """Current read authority without reviving a vector or reserving search work."""
        async with asyncio.timeout(1):
            return (
                await self._context(session_id, run_id, surface_id, require_search_budget=False)
                is not None
            )

    async def bind(
        self, *, session_id: UUID, run_id: UUID, surface_id: str
    ) -> SemanticQueryLookup | None:
        # No creation, queue, reservation or provider call on an ordinary miss.
        async with asyncio.timeout(1):
            loaded = await self._load(session_id, run_id, surface_id)
        if loaded is None or loaded[1] not in self._entries:
            return None
        self._entries.move_to_end(loaded[1])
        return self._entries[loaded[1]].lookup

    async def prepare(
        self, queries: Sequence[RecallQuery], *, session_id: UUID, run_id: UUID, surface_id: str
    ) -> int:
        try:
            # Includes state reads as well as reservation/provider/settlement.
            async with asyncio.timeout(10):
                if not 1 <= len(queries) <= 4:
                    return 0
                loaded = await self._load(session_id, run_id, surface_id)
                if loaded is None:
                    return 0
                context, key, expires = loaded
                if key not in self._entries:

                    async def refresh() -> bool:
                        current = await self._load(session_id, run_id, surface_id)
                        return (
                            current is not None
                            and current[1] == key
                            and key in self._entries
                            and self._entries[key].lookup is lookup
                        )

                    lookup = SemanticQueryPreparation(
                        self._factory,
                        self._clock,
                        self._principal,
                        self._provider,
                        self._config,
                        admitted=lambda: (
                            self._enabled()
                            and key in self._entries
                            and self._entries[key].lookup is lookup
                            and self._clock.now() < self._entries[key].expires
                        ),
                        context=lambda: context,
                        refresh=refresh,
                        cache=self._vectors,
                        local=self._local_config,
                    )
                    self._entries[key] = _Entry(run_id, expires, lookup)
                    while len(self._entries) > 32:
                        _, evicted = self._entries.popitem(last=False)
                        evicted.lookup.invalidate()
                self._entries.move_to_end(key)
                return (await self._entries[key].lookup.prepare(queries)).prepared
        except Exception:
            return 0  # Explicit optional preparation cannot make sparse recall unavailable.
