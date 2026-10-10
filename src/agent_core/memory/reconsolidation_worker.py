"""One maintenance slice composes the existing inventory, executor and writers."""

from collections.abc import Callable
from decimal import Decimal

from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import DreamingRun
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation import ReconsolidationJob
from agent_core.domain.reconsolidation_execution import ReconsolidationExecution
from agent_core.memory.reconsolidation import ReconsolidationInventoryPass
from agent_core.memory.reconsolidation_apply import apply_review
from agent_core.memory.reconsolidation_execution import ReconsolidationBatchExecutor
from agent_core.memory.reconsolidation_inputs import EgressPolicy
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.models import ModelProvider
from agent_core.ports.persistence import UnitOfWorkFactory


class ReconsolidationPass(ReconsolidationInventoryPass):
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        clock: Clock,
        principal: Principal,
        *,
        worker_id: str,
        admitted: Callable[[], bool],
        ids: IdFactory,
        model: ResolvedModel,
        provider: ModelProvider,
        egress_policy: EgressPolicy,
        merge_only: bool = False,
        owner_review: bool = False,
    ) -> None:
        super().__init__(factory, clock, principal, worker_id=worker_id, admitted=admitted)
        self._model = model.model_copy(deep=True)
        self._provider = provider
        self._ids = ids
        self._merge_only = merge_only
        self._owner_review = owner_review
        self._last_execution: ReconsolidationExecution | None = None
        self._proposal_count = 0
        self._executor = ReconsolidationBatchExecutor(
            factory, clock, principal, admitted=admitted, egress_policy=egress_policy
        )

    async def run_once(self) -> int:
        if not self._owner_review:
            return await super().run_once()
        if not self._admitted():
            return 0
        async with self._uow_factory() as uow:
            run = await uow.reconsolidation.claim_dreaming_run(self._principal, self._clock.now())
        if run is None:
            return 0
        self._last_execution = None
        self._proposal_count = 0
        try:
            queued = await super().run_once()
        except Exception:
            await self._finish_daily(run, failed=True)
            raise
        await self._finish_daily(run)
        return queued

    async def _finish_daily(self, run: DreamingRun, *, failed: bool = False) -> None:
        result = self._last_execution
        calls = () if result is None else result.calls
        reason: str = "no_work_available" if result is None else result.reason
        outcome = (
            "proposed"
            if self._proposal_count
            else "no_change"
            if reason in {"reviewed", "no_work_available"}
            else "blocked"
        )
        if failed:
            outcome, reason = "failed", "execution_failed"
        charges = [call.spend.charged_usd for call in calls]
        charged = (
            sum((c for c in charges if c is not None), Decimal(0))
            if all(c is not None for c in charges)
            else None
        )
        async with self._uow_factory() as uow:
            await uow.reconsolidation.finish_dreaming_run(
                self._principal,
                run.model_copy(
                    update={
                        "finished_at": self._clock.now(),
                        "outcome": outcome,
                        "reason": reason,
                        "proposals": self._proposal_count,
                        "provider_calls": len(calls),
                        "charged_usd": charged,
                    }
                ),
            )

    async def _process(self, lease: ReconsolidationJob) -> int:
        if not self._admitted():
            return 0
        groups = []
        async with self._uow_factory() as uow:
            for _ in range(4):
                group = await uow.reconsolidation.claim_group(
                    self._principal, lease.lease_token, self._clock.now()
                )
                if group is None:
                    break
                groups.append(group.id)
        if not groups:
            return 0
        result = await self._executor.run(
            lease,
            tuple(groups),
            model=self._model,
            provider=self._provider,
            batch_id=self._ids.new_id(),
        )
        self._last_execution = result
        finished = set()
        count = 0
        if result.prepared is not None and result.review is not None:
            applied = await apply_review(
                self._uow_factory,
                self._clock,
                self._principal,
                result.prepared,
                result.review,
                admitted=self._admitted,
                merge_only=self._merge_only,
                owner_review=self._owner_review,
            )
            finished = {g.group_id for g in applied if g.outcome != "deferred"}
            count = sum(len(g.operation_ids) for g in applied)
        # Every claimed group needs an outcome, including candidates excluded
        # before serialization. Unsettled spend or lost leases remain recoverable.
        for group_id in groups:
            if group_id in finished:
                continue
            try:
                async with self._uow_factory() as uow:
                    await uow.reconsolidation.finish_group(
                        self._principal, lease.lease_token, group_id, "retry", self._clock.now()
                    )
            except ConflictError:
                break
        self._proposal_count = count
        return count
