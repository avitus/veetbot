"""ADR-0129 section 9: the maintenance pass ends expired task grants.

A grant whose thirty minutes have passed reads as expired at once; the sweep
records the end and its event, exactly once, so a client watching the
session sees the permission stop.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.application.browser_task_grants import sweep_expired_task_grants
from agent_core.domain.browser import BrowserProfile, BrowserProfileStatus
from agent_core.domain.browser_task_grants import (
    TASK_GRANT_DURATION,
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
)
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.support import NOW, memory_uow_factory, principal

SESSION = UUID("00000000-0000-0000-0000-0000000000d1")
PROFILE = UUID("00000000-0000-0000-0000-0000000000d2")


def grant(number: int, *, session_id: UUID, created_at: datetime = NOW) -> BrowserTaskGrant:
    return BrowserTaskGrant(
        id=UUID(int=0xD100 + number),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        session_id=session_id,
        profile_id=PROFILE,
        profile_generation=1,
        agent_version="agent-v1",
        policy_version="policy-v1",
        origin="https://www.example.org",
        path_prefix="/lesson",
        max_actions=200,
        actions_used=7,
        typed_characters=12,
        approval_id=UUID(int=0xD200 + number),
        approved_by=principal().principal_id,
        created_at=created_at,
        expires_at=created_at + TASK_GRANT_DURATION,
    )


async def test_an_expired_task_grant_is_ended_once_with_an_event() -> None:
    _clock, uow_factory = await memory_uow_factory()
    other_session = UUID("00000000-0000-0000-0000-0000000000d3")
    async with uow_factory() as uow:
        for session_id in (SESSION, other_session):
            await uow.sessions.create(
                Session(
                    id=session_id,
                    tenant_id=principal().tenant_id,
                    principal_id=principal().principal_id,
                    agent_id=UUID(int=0xA6),
                    agent_version="agent-v1",
                    status=SessionStatus.ACTIVE,
                    metadata={"browser_profile_id": str(PROFILE)},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        await uow.browser_profiles.create(
            BrowserProfile(
                id=PROFILE,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                provider_name="hosted-isolated",
                provider_ref="opaque-provider-reference",
                allowed_origins=("https://www.example.org",),
                status=BrowserProfileStatus.READY,
                generation=1,
                encryption_key_version="key-v1",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        expiring = await uow.browser_task_grants.create(grant(1, session_id=SESSION))
        current = await uow.browser_task_grants.create(
            grant(2, session_id=other_session, created_at=NOW + timedelta(minutes=20))
        )
    clock = FixedClock(NOW + TASK_GRANT_DURATION + timedelta(seconds=1))
    worker = MaintenanceWorker(
        uow_factory=uow_factory,
        clock=clock,
        sweep_browser_task_grants=partial(
            sweep_expired_task_grants,
            uow_factory,
            clock,
            tenant_id=principal().tenant_id,
        ),
    )

    await worker.run_once()
    await worker.run_once()
    async with uow_factory() as uow:
        ended = await uow.browser_task_grants.get(expiring.id, principal())
        kept = await uow.browser_task_grants.get(current.id, principal())
        events = await uow.events.list_after(SESSION, 0, principal())

    assert ended.end_reason is BrowserTaskGrantEndReason.EXPIRED
    assert kept.ended_at is None
    assert [
        event.payload for event in events if event.event_type == "browser.task_grant.ended"
    ] == [
        {
            "grant_id": str(expiring.id),
            "reason": "expired",
            "actions_used": 7,
            "typed_characters": 12,
        }
    ]
