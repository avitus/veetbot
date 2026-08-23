"""Durable outbox and provider-neutral push transport ports."""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.devices import PushProvider, PushTarget
from agent_core.domain.notifications import (
    NewNotification,
    Notification,
    NotificationCursor,
    NotificationDelivery,
    NotificationStatus,
    PushMessage,
    PushOutcome,
)
from agent_core.domain.runs import Run, RunStatus

if TYPE_CHECKING:
    from agent_core.ports.persistence import RepositoryUnitOfWork


class RunNotificationProducer(Protocol):
    async def for_run_transition(
        self,
        uow: RepositoryUnitOfWork,
        *,
        run: Run,
        principal_id: str,
        status: RunStatus,
        approval_id: UUID | None = None,
        question_id: UUID | None = None,
        approval_expires_at: datetime | None = None,
    ) -> bool: ...


class NotificationOutbox(Protocol):
    async def enqueue(self, notification: NewNotification) -> Notification | None: ...

    async def claim_due(
        self,
        now: datetime,
        limit: int,
        claimant: str,
        lease_seconds: float,
        providers: frozenset[PushProvider],
    ) -> builtins.list[Notification]: ...

    async def list_pending_older_than(
        self,
        before: datetime,
        limit: int,
    ) -> builtins.list[Notification]: ...

    async def record_delivery(self, delivery: NotificationDelivery) -> None: ...

    async def list_deliveries(
        self, notification_id: UUID
    ) -> builtins.list[NotificationDelivery]: ...

    async def list_deliveries_for(
        self,
        notification_ids: tuple[UUID, ...],
    ) -> dict[UUID, builtins.list[NotificationDelivery]]: ...

    async def settle(
        self,
        notification_id: UUID,
        attempt: int,
        status: NotificationStatus,
        next_attempt_at: datetime | None,
    ) -> bool: ...

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: NotificationCursor | None = None,
    ) -> builtins.list[Notification]: ...


class PushTransport(Protocol):
    async def deliver(self, target: PushTarget, message: PushMessage) -> PushOutcome: ...
