"""Two reserved, pinned calls outside transactions, with conservative settlement."""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Literal, cast
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.messages import (
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelUsage,
    ReasoningDeltaEvent,
    ResolvedModel,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ToolCallDeltaEvent,
)
from agent_core.domain.reconsolidation import (
    SLICE_USD,
    CallCompletion,
    CallTokens,
    ReconsolidationJob,
    ReconsolidationSpend,
    StageDecision,
)
from agent_core.domain.reconsolidation_execution import (
    ExecutionReason,
    ReconsolidationCall,
    ReconsolidationExecution,
)
from agent_core.domain.reconsolidation_inputs import PreparedProposalRequest
from agent_core.memory.reconsolidation_admission import ReconsolidationRequestAdmission
from agent_core.memory.reconsolidation_inputs import EgressPolicy
from agent_core.memory.reconsolidation_provider import (
    ProviderBatchError,
    review_prepared_verification,
)
from agent_core.model.cost import price_usage
from agent_core.model.streaming import ModelStreamError, validated_stream
from agent_core.ports.determinism import Clock
from agent_core.ports.models import ModelProvider
from agent_core.ports.persistence import UnitOfWorkFactory

HEARTBEAT_SECONDS = 30
SLICE_SECONDS = 120
CALL_SECONDS = 30
IDLE_SECONDS = 10
SETTLEMENT_SECONDS = 5


class ExecutionStoppedError(Exception):
    def __init__(self, reason: ExecutionReason) -> None:
        self.reason = reason
        super().__init__(reason)


def _bill(
    usage: ModelUsage, model: ResolvedModel, prepared: PreparedProposalRequest
) -> Decimal | None:
    """Only complete, bounded, attributable usage can release reserved headroom."""
    if (
        (usage.provider, usage.model) != (model.provider, model.model)
        or not 0 < usage.input_tokens <= prepared.estimated_input_tokens
        or not 0 < usage.output_tokens <= (prepared.request.maximum_output_tokens or 0)
        or (model.pricing.reasoning_priced_separately and usage.reasoning_tokens is None)
    ):
        return None
    try:
        # Revalidate even a mutable/model_copy value supplied by an adapter.
        usage = ModelUsage.model_validate(usage.model_dump())
        amount = price_usage(usage, model.pricing).cost.quantize(
            Decimal("0.0000000001"), rounding=ROUND_CEILING
        )
        if amount.is_finite() and 0 <= amount <= prepared.maximum_cost_usd:
            return amount
    except (ValueError, ArithmeticError):
        pass
    return None


class ReconsolidationBatchExecutor:
    """Run one proposal/verifier batch on a caller-owned lease and claimed groups.

    The caller resolves the model before entry. We copy that exact resolution,
    reserve each request atomically, and return only a local-validation review.
    The caller still owns group completion, operation planning/commit and lease
    release. Production composition and comparative activation remain separate.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        principal: Principal,
        *,
        admitted: Callable[[], bool],
        egress_policy: EgressPolicy,
    ) -> None:
        self._factory = uow_factory
        self._clock = clock
        self._principal = principal
        self._admitted = admitted
        self._admission = ReconsolidationRequestAdmission(
            uow_factory, clock, principal, admitted=admitted, egress_policy=egress_policy
        )

    def _check(self, lease: ReconsolidationJob) -> None:
        if not self._admitted():
            raise ExecutionStoppedError("admission_withdrawn")
        if self._clock.now() >= lease.slice_started_at + timedelta(seconds=120):
            raise ExecutionStoppedError("timeout")

    async def _current(self, lease: ReconsolidationJob) -> ReconsolidationJob:
        self._check(lease)
        async with self._factory() as uow:
            current = await uow.reconsolidation.renew(
                self._principal, lease.lease_token, self._clock.now()
            )
        if current.id != lease.id or current.slice_started_at != lease.slice_started_at:
            raise ExecutionStoppedError("lease_lost")
        self._check(current)
        return current

    async def _heartbeat(self, lease: ReconsolidationJob) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._current(lease)

    async def _response(
        self,
        prepared: PreparedProposalRequest,
        spend: ReconsolidationSpend,
        model: ResolvedModel,
        provider: ModelProvider,
        stage: Literal["proposal", "verification"],
        lease: ReconsolidationJob,
    ) -> ModelCompletedEvent:
        request = prepared.request
        if (
            self._factory.is_open()
            or provider.name != model.provider
            or request.maximum_provider_attempts != 1
            or request.metadata.get("provider") != model.provider
            or request.metadata.get("model") != model.model
            or request.metadata.get("pricing_digest")
            != hashlib.sha256(model.pricing.model_dump_json().encode()).hexdigest()
            or spend.request_digest != prepared.request_digest
            or spend.maximum_usd != prepared.maximum_cost_usd
            or hashlib.sha256(prepared.serialized_request.encode()).hexdigest()
            != spend.request_digest
        ):
            raise ExecutionStoppedError("unavailable")
        self._check(lease)
        attempt = ModelAttempt(
            attempt_id=uuid5(spend.id, "reconsolidation-provider-attempt"),
            run_id=lease.id,
            step_number=1 if stage == "proposal" else 2,
            attempt_number=1,
            started_at=self._clock.now(),
        )
        terminal = None
        streamed_bytes = 0
        stream = provider.stream(request, model.model_copy(deep=True), attempt)
        try:
            async with asyncio.timeout(min(CALL_SECONDS, request.timeout_seconds)):
                async with aclosing(
                    cast(AsyncGenerator[ModelEvent, None], validated_stream(stream))
                ) as events:
                    while True:
                        try:
                            async with asyncio.timeout(
                                min(IDLE_SECONDS, request.stream_idle_seconds)
                            ):
                                event = await anext(events)
                        except StopAsyncIteration:
                            break
                        self._check(lease)
                        if (event.attempt_id, event.run_id, event.step_number) != (
                            attempt.attempt_id,
                            attempt.run_id,
                            attempt.step_number,
                        ):
                            raise ExecutionStoppedError("invalid_response")
                        if isinstance(event, ToolCallDeltaEvent):
                            raise ExecutionStoppedError("invalid_response")
                        if isinstance(event, (TextDeltaEvent, ReasoningDeltaEvent)):
                            streamed_bytes += len(event.text.encode("utf-8"))
                            if streamed_bytes > 65536:
                                raise ExecutionStoppedError("invalid_response")
                        if isinstance(event, ModelFailedEvent):
                            raise ExecutionStoppedError("unavailable")
                        if isinstance(event, ModelCompletedEvent):
                            terminal = event
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                async with asyncio.timeout(SETTLEMENT_SECONDS):
                    await close()
        if terminal is None or terminal.internal_retry_count:
            raise ExecutionStoppedError("invalid_response")
        return terminal

    async def _call(
        self,
        prepared: PreparedProposalRequest,
        spend: ReconsolidationSpend,
        model: ResolvedModel,
        provider: ModelProvider,
        stage: Literal["proposal", "verification"],
        lease: ReconsolidationJob,
        calls: list[ReconsolidationCall],
    ) -> str:
        started = self._clock.now()
        actual = None
        usage = None
        reason: ExecutionReason = "unavailable"
        cancelled = False
        try:
            completed = await self._response(prepared, spend, model, provider, stage, lease)
            usage = completed.turn.usage.model_copy(deep=True)
            if usage.input_tokens > prepared.estimated_input_tokens or usage.output_tokens > (
                prepared.request.maximum_output_tokens or 0
            ):
                raise ExecutionStoppedError("invalid_response")
            actual = _bill(usage, model, prepared)
            if (
                completed.stop_reason != StopReason.END_TURN
                or completed.turn.stop_reason != StopReason.END_TURN
                or completed.turn.tool_calls
                or len(completed.turn.assistant_messages) != 1
            ):
                raise ExecutionStoppedError("invalid_response")
            parts = completed.turn.assistant_messages[0].content
            if not parts or any(not isinstance(part, TextPart) for part in parts):
                raise ExecutionStoppedError("invalid_response")
            text = "".join(part.text for part in parts if isinstance(part, TextPart))
            if len(text.encode("utf-8")) > 65536:
                raise ExecutionStoppedError("invalid_response")
            reason = "reviewed"
            return text
        except asyncio.CancelledError:
            cancelled = True
            actual = None
            raise
        except TimeoutError:
            reason = "timeout"
            raise ExecutionStoppedError(reason) from None
        except ExecutionStoppedError as exc:
            reason = exc.reason
            raise
        except (ModelStreamError, UnicodeError):
            reason = "invalid_response"
            raise ExecutionStoppedError(reason) from None
        except Exception:
            raise ExecutionStoppedError("unavailable") from None
        finally:
            settled = spend
            finished = self._clock.now()
            try:
                # Cleanup is independent of the admission switch. If lease loss,
                # DB failure or repeated cancellation prevents it, the durable
                # reservation remains fully charged until crash recovery.
                async with asyncio.timeout(SETTLEMENT_SECONDS):
                    async with self._factory() as uow:
                        committed = await uow.reconsolidation.settle(
                            self._principal,
                            lease.lease_token,
                            spend.id,
                            actual,
                            self._clock.now(),
                            completion=CallCompletion.model_validate(
                                {
                                    "reason": "cancelled"
                                    if cancelled
                                    else "completed"
                                    if reason == "reviewed"
                                    else reason,
                                    "finished_at": finished,
                                    "elapsed_ms": max(
                                        0, int((finished - started).total_seconds() * 1000)
                                    ),
                                    "tokens": CallTokens.model_validate(
                                        {
                                            key: getattr(usage, key)
                                            for key in CallTokens.model_fields
                                        }
                                    )
                                    if actual is not None and usage is not None
                                    else None,
                                }
                            ),
                        )
                    settled = committed
            except Exception:
                reason = "settlement_failed"
            calls.append(
                ReconsolidationCall(
                    stage=stage,
                    spend=settled,
                    reason=reason,
                    input_tokens=usage.input_tokens if usage is not None else None,
                    output_tokens=usage.output_tokens if usage is not None else None,
                )
            )
            if reason == "settlement_failed" and not cancelled:
                # A response with uncommitted settlement cannot authorize step 2.
                raise ExecutionStoppedError(reason) from None

    async def _decide(
        self, lease: ReconsolidationJob, calls: list[ReconsolidationCall], decision: StageDecision
    ) -> None:
        async with self._factory() as uow:
            spend = await uow.reconsolidation.record_stage_decision(
                self._principal, lease.lease_token, calls[-1].spend.id, decision, self._clock.now()
            )
        calls[-1] = calls[-1].model_copy(update={"spend": spend})

    async def _batch(
        self,
        lease: ReconsolidationJob,
        group_ids: tuple[UUID, ...],
        model: ResolvedModel,
        provider: ModelProvider,
        batch_id: UUID,
        calls: list[ReconsolidationCall],
    ) -> ReconsolidationExecution:
        current = await self._current(lease)
        if current.requests:
            # A resumed/duplicate invocation never reuses an admitted send.
            return ReconsolidationExecution(reason="deferred")
        first, spend = await self._admission.proposal(
            lease.lease_token, group_ids, model=model, batch_id=batch_id
        )
        if first.prepared is None or spend is None:
            return ReconsolidationExecution(reason="deferred")
        proposal_json = await self._call(
            first.prepared, spend, model, provider, "proposal", lease, calls
        )
        current = await self._current(lease)
        try:
            second, verification_spend = await self._admission.verification(
                first.prepared,
                proposal_json,
                model=model,
                remaining_usd=SLICE_USD - current.slice_spent,
            )
        except ProviderBatchError:
            await self._decide(lease, calls, StageDecision(status="invalid_response"))
            raise
        await self._decide(
            lease,
            calls,
            StageDecision(
                status="prepared" if second.prepared is not None else "deferred",
                validated=0
                if second.prepared is None
                else len(second.prepared.proposal.operations) - len(second.rejected),
                rejected=len(second.rejected),
                deferred_groups=len(second.deferred),
            ),
        )
        if second.prepared is None or verification_spend is None:
            return ReconsolidationExecution(reason="deferred", calls=tuple(calls))
        verification_json = await self._call(
            second.prepared, verification_spend, model, provider, "verification", lease, calls
        )
        await self._current(lease)
        try:
            review = review_prepared_verification(second.prepared, verification_json)
        except ProviderBatchError:
            await self._decide(lease, calls, StageDecision(status="invalid_response"))
            raise
        validated = sum(candidate.requires_local_validation for candidate in review.candidates)
        await self._decide(
            lease,
            calls,
            StageDecision(
                status="reviewed", validated=validated, rejected=len(review.candidates) - validated
            ),
        )
        return ReconsolidationExecution(
            reason="reviewed", calls=tuple(calls), prepared=second.prepared, review=review
        )

    async def run(
        self,
        lease: ReconsolidationJob,
        group_ids: tuple[UUID, ...],
        *,
        model: ResolvedModel,
        provider: ModelProvider,
        batch_id: UUID,
    ) -> ReconsolidationExecution:
        calls: list[ReconsolidationCall] = []
        reason: ExecutionReason = "unavailable"
        try:
            if self._factory.is_open() or provider.name != model.provider:
                raise ExecutionStoppedError("unavailable")
            self._check(lease)
            pinned = model.model_copy(deep=True)
            remaining = (
                lease.slice_started_at + timedelta(seconds=120) - self._clock.now()
            ).total_seconds()
            async with asyncio.timeout(min(SLICE_SECONDS, remaining)), asyncio.TaskGroup() as tasks:
                heartbeat = tasks.create_task(self._heartbeat(lease))
                try:
                    result = await self._batch(lease, group_ids, pinned, provider, batch_id, calls)
                finally:
                    heartbeat.cancel()
            return result
        except TimeoutError:
            reason = "timeout"
        except ExecutionStoppedError as exc:
            reason = exc.reason
        except ExceptionGroup as exc:
            # TaskGroup wraps both body and lease-heartbeat failures. Retain only
            # finite local reasons, never exception/provider prose.
            reasons = [
                e.reason
                if isinstance(e, ExecutionStoppedError)
                else "lease_lost"
                if isinstance(e, (ConflictError, NotFoundError))
                else "invalid_response"
                if isinstance(e, ProviderBatchError)
                else "unavailable"
                for e in exc.exceptions
            ]
            reason = reasons[0]
        except (ConflictError, NotFoundError):
            reason = "lease_lost"
        except ProviderBatchError:
            reason = "invalid_response"
        except Exception:
            reason = "unavailable"
        return ReconsolidationExecution(reason=reason, calls=tuple(calls))
