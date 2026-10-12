"""Explicit slow-path query preparation; lookup never performs provider I/O."""

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import Field

from agent_core.domain.agents import Principal
from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import (
    PROVIDER_EGRESS_SENSITIVITIES,
    SENSITIVITY_ORDER,
    Portability,
    RecallProfile,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation_inputs import EgressSubject
from agent_core.domain.semantic_memory import SemanticQueryVector, query_digest
from agent_core.domain.semantic_work import QUANTUM, EmbeddingConfig, SemanticSpend, SemanticValue
from agent_core.memory.reconsolidation_policy import local_egress_policy
from agent_core.memory.reconsolidation_provider import unsafe_provider_text
from agent_core.ports.determinism import Clock
from agent_core.ports.embeddings import EmbeddingProvider
from agent_core.ports.persistence import UnitOfWorkFactory


class SemanticQueryContext(SemanticValue):
    policy_version: str = Field(min_length=1, max_length=128)
    retrieval_policy: str = Field(min_length=1, max_length=128)
    surface_id: str = Field(min_length=1, max_length=256)
    task_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sensitivity_floor: Sensitivity


@dataclass(frozen=True)
class QueryPreparation:
    reason: str
    prepared: int = 0


class SemanticQueryPreparation:
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        clock: Clock,
        principal: Principal,
        provider: EmbeddingProvider,
        config: EmbeddingConfig,
        *,
        admitted: Callable[[], bool],
        context: Callable[[], SemanticQueryContext],
        refresh: Callable[[], Awaitable[bool]] | None = None,
        cache: OrderedDict[str, tuple[datetime, SemanticQueryVector]] | None = None,
    ) -> None:
        self._factory, self._clock, self._principal = factory, clock, principal
        self._provider = provider
        self._config = EmbeddingConfig.model_validate(config.model_dump())
        self._admitted, self._context = admitted, context
        self._refresh = refresh
        self._policy = local_egress_policy(self._config.residency_provider)
        self._cache = cache if cache is not None else OrderedDict()
        self._active_context: str | None = None
        self._keys: set[str] = set()

    def invalidate(self) -> None:
        """Revoke this context without clearing another run's shared vectors."""
        for key in self._keys:
            self._cache.pop(key, None)
        self._keys.clear()
        self._active_context = None

    def _snapshot(self) -> tuple[SemanticQueryContext, str]:
        if not self._admitted():
            self.invalidate()
            self._active_context = None
            raise ConflictError("semantic query admission withdrawn")
        context = SemanticQueryContext.model_validate(self._context().model_dump())
        key = hashlib.sha256(
            ("semantic-query@1:" + self._config.key + context.model_dump_json()).encode()
        ).hexdigest()
        if key != self._active_context:
            if self._active_context is not None:
                self.invalidate()
            self._active_context = key
        return context, key

    def _check(self, expected: str) -> None:
        if self._snapshot()[1] != expected:
            raise ConflictError("semantic query context changed")

    async def _check_current(self, expected: str) -> None:
        # Runtime refresh is outside every UOW: it may read the saved run state.
        if self._refresh is not None and not await self._refresh():
            self.invalidate()
            raise ConflictError("semantic runtime context changed")
        self._check(expected)

    def _eligible(self, query: RecallQuery) -> bool:
        return (
            (query.tenant_id, query.principal_id)
            == (self._principal.tenant_id, self._principal.principal_id)
            and query.text is not None
            and bool(query.text.strip())
            and query.profile != RecallProfile.CORE
            and query.as_of is None
            and query.known_at is None
            and query.people_scope is None
            and query.candidate_selection == "relevance"
        )

    def _key(self, query: RecallQuery, context_key: str) -> str:
        return hashlib.sha256((context_key + query_digest(query)).encode()).hexdigest()

    def lookup(self, query: RecallQuery) -> SemanticQueryVector | None:
        try:
            _, context_key = self._snapshot()
            if not self._eligible(query):
                return None
            key = self._key(query, context_key)
            value = self._cache.get(key)
            if value is None:
                return None
            if self._clock.now() >= value[0]:
                del self._cache[key]
                self._keys.discard(key)
                return None
            self._cache.move_to_end(key)
            return value[1]
        except Exception:
            self.invalidate()
            return None

    def _text(self, query: RecallQuery, context: SemanticQueryContext) -> str | None:
        assert query.text is not None  # Called only for an eligible text query.
        # Read clearance is not a sensitivity label for text sent to a provider.
        raw = "\n".join([query.text, *query.subjects])
        if unsafe_provider_text(raw):
            return None
        now = self._clock.now()
        identity = ResolvedModel(
            provider=self._config.space.provider,
            model=self._config.space.model,
            resolved_at=now,
        )
        decision = self._policy(
            self._principal,
            identity,
            EgressSubject(
                kind="query",
                id=UUID(hex=query_digest(query)[:32]),
                text=raw,
                sensitivity_floor=context.sensitivity_floor,
                scope=query.current_scope,
                portability=Portability.LOCAL,
                attribution=(),
            ),
            now,
        )
        if not (
            decision.tenant_id == self._principal.tenant_id
            and decision.principal_id == self._principal.principal_id
            and decision.provider == identity.provider
            and decision.model == identity.model
            and decision.policy_version == self._config.egress_revision
            and decision.assessed_at == now
            and decision.permitted
            and decision.residency == "allowed"
            and decision.sensitivity in PROVIDER_EGRESS_SENSITIVITIES
            and decision.sensitivity is not None
            and SENSITIVITY_ORDER[decision.sensitivity]
            >= SENSITIVITY_ORDER[context.sensitivity_floor]
        ):
            return None
        text = json.dumps([query.text, *query.subjects], ensure_ascii=False, separators=(",", ":"))
        return text if len(text.encode()) <= 4096 else None

    async def _send(
        self,
        request: EmbeddingRequest,
        spend: SemanticSpend,
        context_key: str,
    ) -> EmbeddingResponse:
        await self._check_current(context_key)
        task = asyncio.create_task(self._provider.embed(request))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=1)
                await self._check_current(context_key)
                async with self._factory() as uow:
                    await uow.semantic_work.query_current(
                        self._principal, spend.id, self._clock.now()
                    )
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _unknown(self, spend: SemanticSpend) -> None:
        try:
            async with asyncio.timeout(3), self._factory() as uow:
                await uow.semantic_work.settle_query(
                    self._principal, spend.id, None, self._clock.now()
                )
        except Exception:
            pass  # A committed reservation remains fully charged if cleanup cannot finish.

    async def prepare(self, queries: Sequence[RecallQuery]) -> QueryPreparation:
        spend: SemanticSpend | None = None
        try:
            async with asyncio.timeout(10):
                context, context_key = self._snapshot()
                if not 1 <= len(queries) <= 4:
                    return QueryPreparation("input_bound")
                pending: dict[str, tuple[RecallQuery, str]] = {}
                for original in queries:
                    query = RecallQuery.model_validate(original.model_dump())
                    if self._eligible(query) and self.lookup(query) is None:
                        text = self._text(query, context)
                        if text is not None:
                            pending[self._key(query, context_key)] = (query, text)
                if not pending:
                    return QueryPreparation(
                        "cached" if all(self.lookup(q) for q in queries) else "ineligible"
                    )
                request = EmbeddingRequest(
                    space=self._config.space, texts=tuple(p[1] for p in pending.values())
                )
                await self._check_current(context_key)
                async with self._factory() as uow:
                    self._check(context_key)
                    spend = await uow.semantic_work.reserve_query(
                        self._principal,
                        self._config,
                        request.digest,
                        max(QUANTUM, self._config.price(request.maximum_tokens)),
                        self._clock.now(),
                    )
                    self._check(context_key)
                if spend is None:
                    return QueryPreparation("budget_or_lease_deferred")
                response = await self._send(request, spend, context_key)
                vectors = response.validate_for(request)
                await self._check_current(context_key)
                async with self._factory() as uow:
                    self._check(context_key)
                    await uow.semantic_work.settle_query(
                        self._principal,
                        spend.id,
                        self._config.price(response.input_tokens),
                        self._clock.now(),
                    )
                    self._check(context_key)
                await self._check_current(context_key)
                for (key, (query, _)), vector in zip(pending.items(), vectors, strict=True):
                    self._cache[key] = (
                        self._clock.now() + timedelta(seconds=300),
                        SemanticQueryVector(
                            query_sha256=query_digest(query), space=request.space, vector=vector
                        ),
                    )
                    self._keys.add(key)
                    self._cache.move_to_end(key)
                    while len(self._cache) > 128:
                        self._cache.popitem(last=False)
                self._keys.intersection_update(self._cache)
                return QueryPreparation("prepared", len(pending))
        except asyncio.CancelledError:
            if spend is not None:
                await self._unknown(spend)
            raise
        except Exception:
            if spend is not None:
                await self._unknown(spend)
            return QueryPreparation("provider_failed" if spend is not None else "admission_failed")
