"""Explicit slow-path query preparation; lookup never performs provider I/O."""

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import Field

from agent_core.domain.agents import Principal
from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse
from agent_core.domain.errors import ConflictError
from agent_core.domain.local_embeddings import LocalEmbeddingConfig, local_execution
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


@dataclass(frozen=True)
class CachedSemanticQuery:
    expires_at: datetime
    query: RecallQuery
    value: SemanticQueryVector


def encoder_text(query: RecallQuery) -> str:
    assert query.text is not None
    text = query.text
    present = text.casefold()
    subjects: dict[str, str] = {}
    for subject in query.subjects:
        if subject.strip() and subject.casefold() not in present:
            subjects.setdefault(subject.casefold(), subject)
    if subjects:
        text += "\nRelevant subjects: " + "; ".join(subjects.values())
    return text


def people_narrowing(parent: RecallQuery, query: RecallQuery) -> bool:
    """Prove unchanged encoder input and no widening of hard query restrictions."""
    varying = {
        "people_scope",
        "include_ids",
        "expand_ids",
        "subjects",
        "sensitivity_ceiling",
        "max_items",
        "budget_tokens",
        "relationship_group",
        "next_people_after",
    }
    return (
        parent.people_scope is None
        and (query.people_scope is not None or query.relationship_group is not None)
        and (
            query.relationship_group == parent.relationship_group
            or (parent.relationship_group is None and query.relationship_group == "family")
        )
        and parent.model_dump(exclude=varying) == query.model_dump(exclude=varying)
        and encoder_text(parent) == encoder_text(query)
        and {subject.casefold() for subject in query.subjects}
        <= {subject.casefold() for subject in parent.subjects}
        and (
            parent.include_ids is None
            or (query.include_ids is not None and set(query.include_ids) <= set(parent.include_ids))
        )
        and SENSITIVITY_ORDER[query.sensitivity_ceiling]
        <= SENSITIVITY_ORDER[parent.sensitivity_ceiling]
        and query.max_items <= parent.max_items
        and query.budget_tokens <= parent.budget_tokens
    )


def output_budget_narrowing(parent: RecallQuery, query: RecallQuery) -> bool:
    """A tighter render limit changes neither encoder input nor retrieval authority."""
    return (
        query.budget_bytes is not None
        and (parent.budget_bytes is None or query.budget_bytes <= parent.budget_bytes)
        and parent.model_dump(exclude={"budget_bytes"})
        == query.model_dump(exclude={"budget_bytes"})
    )


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
        local: LocalEmbeddingConfig | None = None,
        context: Callable[[], SemanticQueryContext],
        refresh: Callable[[], Awaitable[bool]] | None = None,
        cache: OrderedDict[str, CachedSemanticQuery] | None = None,
    ) -> None:
        self._factory, self._clock, self._principal = factory, clock, principal
        self._provider = provider
        self._config = EmbeddingConfig.model_validate(config.model_dump())
        self._local = local_execution(self._config, local)
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
            ("semantic-query@3:" + self._config.key + context.model_dump_json()).encode()
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
            and (
                (query.as_of is None and query.known_at is None)
                or (self._local and self._config.historical_indexing)
            )
            and query.candidate_selection == "relevance"
        )

    def _key(self, query: RecallQuery, context_key: str) -> str:
        return hashlib.sha256((context_key + query_digest(query)).encode()).hexdigest()

    def lookup(self, query: RecallQuery) -> SemanticQueryVector | None:
        try:
            query = RecallQuery.model_validate(query.model_dump())
            _, context_key = self._snapshot()
            if not self._eligible(query):
                return None
            key = self._key(query, context_key)
            value = self._cache.get(key)
            if value is None:
                if (
                    query.people_scope is not None
                    or query.relationship_group is not None
                    or query.budget_bytes is not None
                ):
                    for parent_key, parent in reversed(tuple(self._cache.items())):
                        if (
                            parent_key == self._key(parent.query, context_key)
                            and self._clock.now() < parent.expires_at
                            and (
                                people_narrowing(parent.query, query)
                                or output_budget_narrowing(parent.query, query)
                            )
                        ):
                            self._cache.move_to_end(parent_key)
                            return parent.value.model_copy(
                                update={"query_sha256": query_digest(query)}
                            )
                return None
            if self._clock.now() >= value.expires_at:
                del self._cache[key]
                self._keys.discard(key)
                return None
            self._cache.move_to_end(key)
            return value.value
        except Exception:
            self.invalidate()
            return None

    def _text(self, query: RecallQuery, context: SemanticQueryContext) -> str | None:
        assert query.text is not None  # Called only for an eligible text query.
        # Serialization syntax and repeated inferred entities distort sentence
        # encoders. Cache identity remains the complete structured query; only
        # the encoder input is natural text, with missing subject context added.
        text = encoder_text(query)
        if len(text.encode()) > 4096:
            return None
        if self._local:
            return text
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
        return text

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
                    if (
                        self._eligible(query)
                        and query.people_scope is None
                        and self.lookup(query) is None
                    ):
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
                    self._cache[key] = CachedSemanticQuery(
                        self._clock.now() + timedelta(seconds=300),
                        query.model_copy(deep=True),
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
