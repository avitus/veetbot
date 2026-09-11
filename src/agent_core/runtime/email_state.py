"""Typed email projection mechanics over the persistence port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord, EmailValue
from agent_core.ports.email import EmailStore


async def records(store: EmailStore, principal: Principal, kind: str) -> AsyncIterator[EmailRecord]:
    after: str | None = None
    while page := await store.list(principal, kind, after=after):
        for row in page:
            yield row
        after = page[-1].key


async def read_value[Value: EmailValue](
    store: EmailStore, principal: Principal, kind: str, key: str, model: type[Value]
) -> Value | None:
    row = await store.get(principal, kind, key)
    return None if row is None else model.model_validate(row.payload)


async def save_value(
    store: EmailStore,
    principal: Principal,
    kind: str,
    key: str,
    value: EmailValue,
    now: datetime,
) -> EmailRecord:
    """Caller holds the principal lock for read-modify-write sequences."""
    previous = await store.get(principal, kind, key)
    revision = 0 if previous is None else previous.revision
    return await store.put(
        EmailRecord(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            kind=kind,
            key=key,
            revision=revision + 1,
            payload=value.model_dump(mode="json"),
            created_at=now if previous is None else previous.created_at,
            updated_at=now,
        ),
        expected_revision=revision,
    )
