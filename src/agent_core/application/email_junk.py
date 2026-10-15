"""Durable pilot admission; callers hold the email owner lock through commit."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord, EmailThread
from agent_core.domain.email_junk import JUNK_PILOT_DAILY_LIMIT, JunkPilot
from agent_core.domain.errors import ConflictError
from agent_core.ports.email import EmailStore


class JunkPilotUnavailableError(ConflictError):
    """A pilot stopped or filled while a judgment was in flight; assess normally."""


class PilotDay(BaseModel):
    """Content-free daily counter, independent of evidence-file revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    count: int = Field(ge=0, le=JUNK_PILOT_DAILY_LIMIT)


class PilotAdmission(BaseModel):
    """One durable conversation admission makes refresh and recovery idempotent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    admitted_at: AwareDatetime


def _keys(thread: EmailThread, now: datetime) -> tuple[str, str]:
    """Hash provider identity and key the fixed daily allowance by account and UTC date."""
    key = hashlib.sha256(f"{thread.account_id}:{thread.provider_thread_id}".encode()).hexdigest()
    day = f"{thread.account_id}:{now.astimezone(UTC).date().isoformat()}"
    return key, day


async def pilot_has_capacity(
    store: EmailStore, principal: Principal, thread: EmailThread, now: datetime
) -> bool:
    """Avoid new paid calls after the cap while permitting already-admitted conversations."""
    key, day = _keys(thread, now)
    admitted = await store.get(principal, "junk_pilot_admission", key)
    if admitted is not None:
        PilotAdmission.model_validate(admitted.payload)
        return True
    counter = await store.get(principal, "junk_pilot_day", day)
    return (
        counter is None or PilotDay.model_validate(counter.payload).count < JUNK_PILOT_DAILY_LIMIT
    )


async def admit_pilot(
    store: EmailStore,
    principal: Principal,
    thread: EmailThread,
    pilot: JunkPilot,
    now: datetime,
) -> None:
    """Commit a new slot in the same locked transaction as the bypass assessment."""
    if not pilot.admitted(principal, thread.account_id, now):
        raise JunkPilotUnavailableError("email junk pilot is inactive")
    if not await pilot_has_capacity(store, principal, thread, now):
        raise JunkPilotUnavailableError("email junk pilot daily limit reached")
    key, day = _keys(thread, now)
    if await store.get(principal, "junk_pilot_admission", key) is not None:
        return
    previous = await store.get(principal, "junk_pilot_day", day)
    revision = 0 if previous is None else previous.revision
    count = 0 if previous is None else PilotDay.model_validate(previous.payload).count
    for kind, identifier, old, value in (
        ("junk_pilot_day", day, previous, PilotDay(count=count + 1)),
        ("junk_pilot_admission", key, None, PilotAdmission(admitted_at=now)),
    ):
        expected = revision if kind == "junk_pilot_day" else 0
        await store.put(
            EmailRecord(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kind=kind,
                key=identifier,
                revision=expected + 1,
                payload=value.model_dump(mode="json"),
                created_at=now if old is None else old.created_at,
                updated_at=now,
            ),
            expected_revision=expected,
        )
