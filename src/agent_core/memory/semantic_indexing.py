"""Bounded background semantic indexing; never composed on the recall fast path."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from agent_core.domain.agents import Principal
from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import PROVIDER_EGRESS_SENSITIVITIES, SENSITIVITY_ORDER
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation_inputs import EgressSubject
from agent_core.domain.semantic_memory import SemanticSource, embedding_text
from agent_core.domain.semantic_work import QUANTUM, EmbeddingConfig, SemanticJob, SemanticSpend
from agent_core.memory.reconsolidation_policy import local_egress_policy
from agent_core.memory.reconsolidation_provider import unsafe_provider_text
from agent_core.ports.determinism import Clock
from agent_core.ports.embeddings import EmbeddingProvider
from agent_core.ports.persistence import UnitOfWorkFactory


@dataclass(frozen=True)
class IndexSlice:
    reason: str
    inspected: int = 0
    indexed: int = 0
    deferred: int = 0


@dataclass(frozen=True)
class _Prepared:
    job: SemanticJob
    request: EmbeddingRequest
    sources: tuple[SemanticSource, ...]
    spend: SemanticSpend
    full: int
    changed: int
    inspected: int
    deferred: int


class SemanticIndexingPass:
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        clock: Clock,
        principal: Principal,
        provider: EmbeddingProvider,
        config: EmbeddingConfig,
        *,
        admitted: Callable[[], bool],
    ) -> None:
        self._factory, self._clock, self._principal = factory, clock, principal
        self._provider = provider
        self._config = EmbeddingConfig.model_validate(config.model_dump())
        self._admitted = admitted
        self._policy = local_egress_policy(self._config.residency_provider)

    def _check(self) -> None:
        if not self._admitted():
            raise ConflictError("semantic admission withdrawn")

    def _permitted(self, source: SemanticSource, now: datetime) -> bool:
        record = source.record
        if unsafe_provider_text(record.subject + "\n" + record.statement):
            return False
        # The existing classifier uses only this resolved provider/model identity;
        # chat capabilities/prices are never used to price or send embeddings.
        identity = ResolvedModel(
            provider=self._config.space.provider, model=self._config.space.model, resolved_at=now
        )
        try:
            decision = self._policy(
                self._principal,
                identity,
                EgressSubject(
                    kind="memory",
                    id=record.id,
                    text=record.subject + "\n" + record.statement,
                    sensitivity_floor=record.sensitivity,
                    scope=record.scope,
                    portability=record.portability,
                    attribution=(),
                ),
                now,
            )
            return (
                decision.tenant_id == self._principal.tenant_id
                and decision.principal_id == self._principal.principal_id
                and decision.provider == identity.provider
                and decision.model == identity.model
                and decision.policy_version == self._config.egress_revision
                and decision.assessed_at == now
                and decision.permitted
                and decision.residency == "allowed"
                and decision.sensitivity is not None
                and decision.sensitivity in PROVIDER_EGRESS_SENSITIVITIES
                and SENSITIVITY_ORDER[decision.sensitivity] >= SENSITIVITY_ORDER[record.sensitivity]
            )
        except Exception:
            return False

    async def _prepare(self, job: SemanticJob) -> _Prepared | IndexSlice:
        async with self._factory() as uow, uow.people.lock(self._principal):
            self._check()
            job = await uow.semantic_work.current(
                self._principal, job.lease_token, self._clock.now()
            )
            full = await uow.semantic_memory.inventory(
                self._principal, after=job.full_cursor, through=job.full_bound, limit=16
            )
            changes = await uow.semantic_memory.changes(
                self._principal, after=job.change_cursor, through=job.change_bound, limit=16
            )
            inspected = full.cursor - job.full_cursor + changes.cursor - job.change_cursor
            ids = dict.fromkeys(
                [
                    *(s.belief_id for s in full.sources),
                    *(s.source.belief_id for s in changes.changes),
                ]
            )
            sources = []
            texts = []
            deferred = 0
            total = 0
            for key in ids:
                source = await uow.semantic_memory.capture(self._principal, key, self._config.space)
                if source is None or await uow.semantic_memory.indexed(self._principal, source):
                    continue
                if not self._permitted(source, self._clock.now()):
                    deferred += 1
                    continue
                text = embedding_text(source.record)
                size = len(text.encode()) + 1
                if size > 4097 or total + size > 65536:
                    deferred += 1
                    continue
                sources.append(source)
                texts.append(text)
                total += size
            self._check()
            if not texts:
                await uow.semantic_work.finish(
                    self._principal,
                    job.lease_token,
                    full.cursor,
                    changes.cursor,
                    True,
                    self._clock.now(),
                )
                return IndexSlice("scanned", inspected=inspected, deferred=deferred)
            request = EmbeddingRequest(space=self._config.space, texts=tuple(texts))
            spend = await uow.semantic_work.reserve(
                self._principal,
                job.lease_token,
                self._config,
                request.digest,
                max(QUANTUM, self._config.price(request.maximum_tokens)),
                full.cursor,
                changes.cursor,
                self._clock.now(),
            )
            if spend is None:
                await uow.semantic_work.finish(
                    self._principal,
                    job.lease_token,
                    full.cursor,
                    changes.cursor,
                    False,
                    self._clock.now(),
                )
                return IndexSlice(
                    "budget_deferred", inspected=inspected, deferred=len(texts) + deferred
                )
            prepared = _Prepared(
                job,
                request,
                tuple(sources),
                spend,
                full.cursor,
                changes.cursor,
                inspected,
                deferred,
            )
            self._check()
        return prepared

    async def _send(self, prepared: _Prepared) -> EmbeddingResponse:
        self._check()
        task = asyncio.create_task(self._provider.embed(prepared.request))
        try:
            async with asyncio.timeout(30):
                while not task.done():
                    await asyncio.wait({task}, timeout=1)
                    self._check()
                    async with self._factory() as uow:
                        await uow.semantic_work.current(
                            self._principal, prepared.job.lease_token, self._clock.now()
                        )
                return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _unknown(self, prepared: _Prepared) -> None:
        # Failure to finish cleanup leaves the committed full reservation charged.
        try:
            async with asyncio.timeout(5), self._factory() as uow:
                await uow.semantic_work.settle(
                    self._principal,
                    prepared.job.lease_token,
                    prepared.spend.id,
                    None,
                    self._clock.now(),
                )
                await uow.semantic_work.finish(
                    self._principal,
                    prepared.job.lease_token,
                    prepared.full,
                    prepared.changed,
                    False,
                    self._clock.now(),
                )
        except Exception:
            pass

    async def run_slice(self) -> IndexSlice:
        if not self._admitted():
            return IndexSlice("disabled")
        prepared: _Prepared | None = None
        try:
            async with asyncio.timeout(60):
                async with self._factory() as uow, uow.people.lock(self._principal):
                    self._check()
                    bounds = await uow.semantic_memory.bounds(self._principal)
                    job = await uow.semantic_work.claim(
                        self._principal, self._config, bounds, self._clock.now()
                    )
                if job is None:
                    return IndexSlice("not_due")
                preview = await self._prepare(job)
                if isinstance(preview, IndexSlice):
                    return preview
                prepared = preview
                response = await self._send(prepared)
                vectors = response.validate_for(prepared.request)
                async with self._factory() as uow, uow.people.lock(self._principal):
                    self._check()
                    await uow.semantic_work.current(
                        self._principal, job.lease_token, self._clock.now()
                    )
                    indexed = 0
                    for source, vector in zip(prepared.sources, vectors, strict=True):
                        indexed += int(
                            await uow.semantic_memory.put(self._principal, source, vector)
                        )
                    await uow.semantic_work.settle(
                        self._principal,
                        job.lease_token,
                        prepared.spend.id,
                        self._config.price(response.input_tokens),
                        self._clock.now(),
                    )
                    self._check()
                    await uow.semantic_work.finish(
                        self._principal,
                        job.lease_token,
                        prepared.full,
                        prepared.changed,
                        True,
                        self._clock.now(),
                    )
                return IndexSlice("indexed", prepared.inspected, indexed, prepared.deferred)
        except asyncio.CancelledError:
            if prepared is not None:
                await self._unknown(prepared)
            raise
        except Exception:
            if prepared is not None:
                await self._unknown(prepared)
            return IndexSlice("provider_failed" if prepared is not None else "admission_failed")
