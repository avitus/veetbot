"""Durable pilot admission; callers hold the email owner lock through commit."""

from __future__ import annotations

from datetime import datetime

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord, EmailThread
from agent_core.domain.email_junk import (
    JunkAuthorization,
    JunkPilotUnavailableError,
    PilotAdmission,
    PilotDay,
    junk_pilot_capacity,
    junk_pilot_keys,
)
from agent_core.ports.email import EmailStore


async def admit_pilot(
    store: EmailStore,
    principal: Principal,
    thread: EmailThread,
    pilot: JunkAuthorization,
    now: datetime,
) -> None:
    """Commit a new slot in the same locked transaction as the bypass assessment."""
    if not pilot.admitted(principal, thread.account_id, now):
        raise JunkPilotUnavailableError("email junk pilot is inactive")
    key, day = junk_pilot_keys(thread, now)
    admission = await store.get(principal, "junk_pilot_admission", key)
    previous = await store.get(principal, "junk_pilot_day", day)
    if not junk_pilot_capacity(
        None if admission is None else admission.payload,
        None if previous is None else previous.payload,
    ):
        raise JunkPilotUnavailableError("email junk pilot daily limit reached")
    if admission is not None:
        return
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
