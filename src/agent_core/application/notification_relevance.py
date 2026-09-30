"""Server-authoritative relevance, shared by dispatch and delivered-alert cleanup."""

from datetime import datetime
from typing import Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.errors import NotFoundError
from agent_core.domain.notifications import Notification, NotificationKind
from agent_core.domain.runs import RunStatus
from agent_core.ports.notifications import NotificationOutbox
from agent_core.ports.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    RunRepository,
    SessionRepository,
)


class NotificationRelevanceUnitOfWork(Protocol):
    approvals: ApprovalRepository
    checkpoints: CheckpointRepository
    runs: RunRepository
    sessions: SessionRepository
    notification_outbox: NotificationOutbox


async def notification_obsolete(
    uow: NotificationRelevanceUnitOfWork,
    notification: Notification,
    now: datetime,
    *,
    include_expiry: bool = True,
) -> bool:
    if include_expiry and notification.expires_at is not None and notification.expires_at <= now:
        return True
    principal = Principal(
        tenant_id=notification.tenant_id,
        principal_id=notification.principal_id,
    )
    try:
        if notification.session_id is not None:
            await uow.sessions.get(notification.session_id, principal)
        if notification.kind is NotificationKind.APPROVAL_REQUESTED:
            assert notification.approval_id is not None
            approval = await uow.approvals.get(notification.approval_id, principal)
            return approval.status is not ApprovalStatus.PENDING or (
                approval.expires_at is not None and approval.expires_at <= now
            )
        if notification.run_id is None:
            return False
        run = await uow.runs.get(notification.run_id, principal)
        if notification.kind is NotificationKind.QUESTION_ASKED:
            if run.status is not RunStatus.WAITING_FOR_USER:
                return True
            checkpoint = await uow.checkpoints.latest(run.id)
            if checkpoint is None:
                return True
            return checkpoint.working_state.get("outstanding_question_id") != str(
                notification.question_id
            )
        if notification.kind in {
            NotificationKind.RUN_FAILED,
            NotificationKind.SCHEDULE_RUN_FINISHED,
        } and await uow.notification_outbox.run_seen(principal, run.id):
            return True
        if notification.kind is NotificationKind.RUN_FAILED:
            return run.status is not RunStatus.FAILED
        if notification.kind is NotificationKind.SCHEDULE_RUN_FINISHED:
            return run.status not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }
    except NotFoundError:
        return True
    return False
