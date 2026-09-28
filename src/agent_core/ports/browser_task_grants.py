"""Persistence port for session-bound browser task grants (ADR-0129)."""

from __future__ import annotations

import builtins
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser_task_grants import (
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
    BrowserTaskGrantScope,
    BrowserTaskScopePolicy,
)


class BrowserTaskGrantRepository(Protocol):
    async def get_scopes(self, principal: Principal) -> BrowserTaskScopePolicy:
        """Read the latest policy; an uninitialized principal has no scopes."""
        ...

    def locked_scopes(
        self, principal: Principal, *, defaults: tuple[BrowserTaskGrantScope, ...] = ()
    ) -> AbstractAsyncContextManager[BrowserTaskScopePolicy]:
        """Initialize once and lock the owner's policy through the operation.

        PostgreSQL holds the row lock through the enclosing transaction;
        memory holds its mutex through the context. Defaults never overwrite.
        """
        ...

    async def replace_scopes(self, principal: Principal, policy: BrowserTaskScopePolicy) -> None:
        """Replace under locked_scopes; require the preceding revision."""
        ...

    async def create(self, grant: BrowserTaskGrant) -> BrowserTaskGrant:
        """Store a new grant. ``ConflictError`` on a reused id or while the
        session already has an unended grant."""
        ...

    async def get(self, grant_id: UUID, principal: Principal) -> BrowserTaskGrant:
        """``NotFoundError`` for a missing grant or another principal's."""
        ...

    async def active_for_session(
        self, session_id: UUID, principal: Principal, *, now: datetime
    ) -> BrowserTaskGrant | None:
        """The session's unended grant whose window is still open at ``now``."""
        ...

    async def list(
        self,
        principal: Principal,
        *,
        session_id: UUID | None = None,
        active_only: bool = False,
        now: datetime,
        limit: int | None = None,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[BrowserTaskGrant]:
        """Newest first by ``(created_at, id)``, strictly after one composite
        cursor; both cursor components are supplied together."""
        ...

    async def consume(
        self,
        grant_id: UUID,
        principal: Principal,
        *,
        session_id: UUID,
        typed: int,
        now: datetime,
    ) -> BrowserTaskGrant | None:
        """Use one action and ``typed`` characters in one guarded step.

        ``None`` when the grant is ended, revoked, expired at ``now``, already
        at its action cap, or would pass its typed-character budget. The use
        that reaches the cap ends the grant ``exhausted``.
        """
        ...

    async def end(
        self,
        grant_id: UUID,
        principal: Principal,
        *,
        reason: BrowserTaskGrantEndReason,
        now: datetime,
    ) -> tuple[BrowserTaskGrant, bool]:
        """End an unended grant once; the flag says whether this call ended it."""
        ...

    async def end_expired(
        self, now: datetime, limit: int, *, tenant_id: str
    ) -> builtins.list[BrowserTaskGrant]:
        """End up to ``limit`` of one tenant's unended grants whose window closed."""
        ...

    async def end_for_profile(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        reason: BrowserTaskGrantEndReason,
        now: datetime,
    ) -> builtins.list[BrowserTaskGrant]:
        """End every unended grant on one profile; returns those this call ended."""
        ...
