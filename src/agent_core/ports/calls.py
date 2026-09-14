"""Atomic principal-scoped call ledger and correspondence storage."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.calls import CallRecord


class CallStore(Protocol):
    def lock(self, principal: Principal) -> AbstractAsyncContextManager[None]: ...

    async def get(self, principal: Principal, kind: str, key: str) -> CallRecord | None: ...

    async def list(
        self,
        principal: Principal,
        kind: str,
        *,
        after: str | None = None,
        limit: int = 100,
        active: bool | None = None,
        due_at: datetime | None = None,
    ) -> list[CallRecord]: ...

    async def put(self, record: CallRecord, *, expected_revision: int) -> CallRecord: ...
