"""Durable, content-derived outbound delivery for messaging surfaces."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from agent_core.application.trajectory_service import TrajectoryRedactor
from agent_core.domain.agents import Principal
from agent_core.domain.devices import PushProvider, PushTarget
from agent_core.domain.notifications import (
    DeliveryOutcome,
    NotificationKind,
    PushMessage,
    PushOutcome,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.surfaces import (
    SurfaceReply,
    SurfaceReplyStatus,
    SurfaceTransportOutcome,
    SurfaceTransportResult,
    chunk_surface_text,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.dispatch import WorkerService
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.schedules import SchedulePrincipalDirectory

logger = logging.getLogger(__name__)


class SurfaceDelivery(Protocol):
    async def __call__(
        self,
        chat_ref: str,
        text: str,
        *,
        last_inbound_at: datetime | None,
        now: datetime,
    ) -> SurfaceTransportResult: ...


class SurfaceTemplatePolicy(Protocol):
    """Channel rule that replaces stale freeform content with one safe template."""

    def freeform_allowed(self, *, last_inbound_at: datetime | None, now: datetime) -> bool: ...

    async def deliver_template(self, chat_ref: str) -> SurfaceTransportResult: ...


class SurfaceLoopService(WorkerService, Protocol):
    async def run_once(self) -> int: ...


class SurfaceReplyDispatcher:
    """Claim reply rows, derive safe content from the run, and resume by chunk."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        deliveries: Mapping[PushProvider, SurfaceDelivery],
        template_policies: Mapping[PushProvider, SurfaceTemplatePolicy] | None = None,
        clock: Clock,
        redactor: TrajectoryRedactor,
        worker_id: str,
        batch_size: int,
        lease_seconds: int,
        retry_delays: tuple[float, ...],
        chunk_size: int = 4096,
    ) -> None:
        if not deliveries:
            raise ValueError("surface reply dispatcher requires a transport")
        if batch_size <= 0 or lease_seconds <= 0 or chunk_size <= 0:
            raise ValueError("surface reply dispatcher limits must be positive")
        if not retry_delays or any(delay <= 0 for delay in retry_delays):
            raise ValueError("surface reply retry delays must be positive")
        if not worker_id.strip():
            raise ValueError("surface reply worker identifier cannot be blank")
        self._uow_factory = uow_factory
        self._deliveries = dict(deliveries)
        self._template_policies = dict(template_policies or {})
        self._clock = clock
        self._redactor = redactor
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._retry_delays = retry_delays
        self._chunk_size = chunk_size

    async def run_once(self) -> int:
        async with self._uow_factory() as uow:
            claimed = await uow.surfaces.replies.claim_due(
                self._clock.now(),
                self._batch_size,
                self._worker_id,
                self._lease_seconds,
            )
        for reply in claimed:
            try:
                await self._dispatch(reply)
            except Exception:
                logger.exception(
                    "surface_reply_dispatch_failed",
                    extra={"reply_id": str(reply.id), "run_id": str(reply.run_id)},
                )
                await self._retry(reply)
        return len(claimed)

    async def _dispatch(self, reply: SurfaceReply) -> None:
        principal = Principal(
            tenant_id=reply.tenant_id,
            principal_id=reply.principal_id,
            roles=set(),
            scopes=set(),
        )
        async with self._uow_factory() as uow:
            run = await uow.runs.get(reply.run_id, principal)
            mapping = await uow.surfaces.sessions.for_session(run.session_id)
            device = await uow.devices.get(reply.surface_id, principal)
        delivery = (
            None if device.push_provider is None else self._deliveries.get(device.push_provider)
        )
        if delivery is None or mapping is None:
            await self._settle(reply, SurfaceReplyStatus.FAILED)
            return
        assert device.push_provider is not None
        policy = self._template_policies.get(device.push_provider)
        if policy is not None and not policy.freeform_allowed(
            last_inbound_at=mapping.last_inbound_at,
            now=self._clock.now(),
        ):
            outcome = await policy.deliver_template(reply.chat_ref)
            if outcome.outcome is SurfaceTransportOutcome.RETRY:
                await self._retry(reply)
                return
            if outcome.outcome is SurfaceTransportOutcome.REJECTED:
                await self._settle(reply, SurfaceReplyStatus.FAILED)
                return
            current = reply
            if current.chunks_sent == 0 and current.chunks_total is None:
                async with self._uow_factory() as uow:
                    current = await uow.surfaces.replies.record_chunk(
                        current.id,
                        worker_id=self._worker_id,
                        chunks_total=1,
                        at=self._clock.now(),
                    )
            await self._settle(current, SurfaceReplyStatus.DISPATCHED)
            return
        text = self._reply_text(run.status, run.final_message)
        payload: dict[str, object] = {"text": text}
        self._redactor.redact([payload])
        redacted = payload["text"]
        if not isinstance(redacted, str):
            raise RuntimeError("surface reply redaction changed the text type")
        chunks = chunk_surface_text(redacted, limit=self._chunk_size)
        if not chunks:
            chunks = ("surface.empty_reply",)
        if reply.chunks_total is not None and reply.chunks_total != len(chunks):
            raise RuntimeError("surface reply content changed after partial delivery")
        current = reply
        for chunk in chunks[current.chunks_sent :]:
            outcome = await delivery(
                current.chat_ref,
                chunk,
                last_inbound_at=mapping.last_inbound_at,
                now=self._clock.now(),
            )
            if outcome.outcome is SurfaceTransportOutcome.RETRY:
                await self._retry(current)
                return
            if outcome.outcome is SurfaceTransportOutcome.REJECTED:
                await self._settle(current, SurfaceReplyStatus.FAILED)
                return
            async with self._uow_factory() as uow:
                current = await uow.surfaces.replies.record_chunk(
                    current.id,
                    worker_id=self._worker_id,
                    chunks_total=len(chunks),
                    at=self._clock.now(),
                )
        await self._settle(current, SurfaceReplyStatus.DISPATCHED)

    @staticmethod
    def _reply_text(status: RunStatus, final_message: str | None) -> str:
        if status is RunStatus.COMPLETED and final_message:
            return final_message
        if status is RunStatus.CANCELLED:
            return "surface.run_cancelled"
        return "surface.run_failed"

    async def _retry(self, reply: SurfaceReply) -> None:
        delay = self._retry_delays[min(max(reply.attempts - 1, 0), len(self._retry_delays) - 1)]
        async with self._uow_factory() as uow:
            await uow.surfaces.replies.retry(
                reply.id,
                worker_id=self._worker_id,
                next_attempt_at=self._clock.now() + timedelta(seconds=delay),
            )

    async def _settle(self, reply: SurfaceReply, status: SurfaceReplyStatus) -> None:
        async with self._uow_factory() as uow:
            await uow.surfaces.replies.settle(
                reply.id,
                worker_id=self._worker_id,
                status=status,
                at=self._clock.now(),
            )


class SurfaceNotificationTransport:
    """Resolve content-free notification details for one paired chat."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        principals: SchedulePrincipalDirectory,
        deliveries: Mapping[PushProvider, SurfaceDelivery],
        template_policies: Mapping[PushProvider, SurfaceTemplatePolicy] | None,
        clock: Clock,
        redactor: TrajectoryRedactor,
        chunk_size: int = 4096,
    ) -> None:
        if not deliveries or chunk_size <= 0:
            raise ValueError("surface notification transport requires bounded deliveries")
        self._uow_factory = uow_factory
        self._principals = principals
        self._deliveries = dict(deliveries)
        self._template_policies = dict(template_policies or {})
        self._clock = clock
        self._redactor = redactor
        self._chunk_size = chunk_size

    async def deliver(self, target: PushTarget, message: PushMessage) -> PushOutcome:
        delivery = self._deliveries.get(target.provider)
        if delivery is None:
            return PushOutcome(
                outcome=DeliveryOutcome.REJECTED,
                provider_reason="surface.provider_unsupported",
            )
        chat_ref = target.token.get_secret_value()
        async with self._uow_factory() as uow:
            pairing = await uow.surfaces.pairings.live_pairing(
                target.device_id,
                chat_ref,
            )
            if pairing is None:
                return PushOutcome(
                    outcome=DeliveryOutcome.REJECTED,
                    provider_reason="surface.pairing_unavailable",
                )
            authority = await self._principals.current(
                pairing.tenant_id,
                pairing.principal_id,
            )
            if authority is None or not authority.enabled:
                return PushOutcome(
                    outcome=DeliveryOutcome.REJECTED,
                    provider_reason="surface.principal_unavailable",
                )
            scopes = pairing.granted_scopes.intersection(authority.principal.scopes)
            text = message.payload.title
            identifier: UUID | None = None
            if message.payload.kind is NotificationKind.QUESTION_ASKED:
                identifier = message.payload.question_id
                if "run.read" in scopes and message.payload.run_id is not None:
                    checkpoint = await uow.checkpoints.latest(message.payload.run_id)
                    if checkpoint is not None:
                        question = checkpoint.working_state.get("outstanding_question_text")
                        current_id = checkpoint.working_state.get("outstanding_question_id")
                        if isinstance(question, str) and str(identifier) == str(current_id):
                            text = question
            elif message.payload.kind is NotificationKind.APPROVAL_REQUESTED:
                identifier = message.payload.approval_id
                if "approval.read" in scopes and identifier is not None:
                    approval = await uow.approvals.get(identifier, authority.principal)
                    text = f"Approval needed\n{approval.action_summary}"
            if identifier is not None:
                short_identifier = str(identifier).split("-", 1)[0]
                text = f"{text}\nID: {short_identifier}"
                if message.payload.kind is NotificationKind.APPROVAL_REQUESTED:
                    text = f"{text}\n/approve {short_identifier} or /deny {short_identifier}"

        payload: dict[str, object] = {"text": text}
        self._redactor.redact([payload])
        redacted = payload["text"]
        if not isinstance(redacted, str):
            raise RuntimeError("surface notification redaction changed the text type")
        policy = self._template_policies.get(target.provider)
        if policy is not None and not policy.freeform_allowed(
            last_inbound_at=pairing.last_message_at,
            now=self._clock.now(),
        ):
            return _push_outcome(await policy.deliver_template(chat_ref))
        for chunk in chunk_surface_text(redacted, limit=self._chunk_size):
            outcome = await delivery(
                chat_ref,
                chunk,
                last_inbound_at=pairing.last_message_at,
                now=self._clock.now(),
            )
            if outcome.outcome is not SurfaceTransportOutcome.DELIVERED:
                return _push_outcome(outcome)
        return PushOutcome(outcome=DeliveryOutcome.DELIVERED)


def _push_outcome(result: SurfaceTransportResult) -> PushOutcome:
    outcome = {
        SurfaceTransportOutcome.DELIVERED: DeliveryOutcome.DELIVERED,
        SurfaceTransportOutcome.RETRY: DeliveryOutcome.RETRY,
        SurfaceTransportOutcome.REJECTED: DeliveryOutcome.REJECTED,
    }[result.outcome]
    return PushOutcome(
        outcome=outcome,
        provider_reason=result.reason_code,
        provider_id=result.external_message_id,
    )


class SurfaceWorker(WorkerService):
    """Bounded outbound drain loop; the role runner owns webhook serving."""

    def __init__(
        self,
        *,
        dispatch_once: Callable[[], Awaitable[int]],
        clock: Clock,
        fallback_poll_seconds: float,
        additional_services: tuple[SurfaceLoopService, ...] = (),
    ) -> None:
        if fallback_poll_seconds <= 0:
            raise ValueError("surface fallback poll must be positive")
        self._dispatch_once = dispatch_once
        self._clock = clock
        self._fallback_poll = fallback_poll_seconds
        self._additional_services = additional_services
        self._stopping = False
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        for service in self._additional_services:
            service.stop()

    async def run_once(self) -> int:
        counts = [await self._dispatch_once()]
        for service in self._additional_services:
            counts.append(await service.run_once())
        return sum(counts)

    async def run_forever(self) -> None:
        if self._additional_services:
            tasks = [asyncio.create_task(self._run_dispatch_loop())]
            tasks.extend(
                asyncio.create_task(service.run_forever()) for service in self._additional_services
            )
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self.stop()
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    await task
            finally:
                self.stop()
            return
        await self._run_dispatch_loop()

    async def _run_dispatch_loop(self) -> None:
        while not self._stopping:
            try:
                await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("surface dispatcher scan failed")
            if self._stopping:
                break
            waiting = asyncio.create_task(self._clock.sleep(self._fallback_poll))
            stopping = asyncio.create_task(self._stop_event.wait())
            try:
                await asyncio.wait(
                    {waiting, stopping},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (waiting, stopping):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(waiting, stopping, return_exceptions=True)
