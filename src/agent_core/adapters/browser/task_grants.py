"""In-memory browser task-grant repository (ADR-0129)."""

from __future__ import annotations

import asyncio
import builtins
from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser_task_grants import (
    TASK_GRANT_MAX_TYPED_CHARACTERS,
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
)
from agent_core.domain.errors import ConflictError, NotFoundError


class InMemoryBrowserTaskGrantRepository:
    """Serializes every guarded change under one lock, as the SQL statements do."""

    def __init__(self) -> None:
        self._grants: dict[UUID, BrowserTaskGrant] = {}
        self._lock = asyncio.Lock()

    async def create(self, grant: BrowserTaskGrant) -> BrowserTaskGrant:
        async with self._lock:
            if grant.id in self._grants:
                raise ConflictError("browser task grant already exists")
            if any(
                existing.session_id == grant.session_id and existing.ended_at is None
                for existing in self._grants.values()
            ):
                raise ConflictError("the session already has an active browser task grant")
            self._grants[grant.id] = grant.model_copy(deep=True)
            return grant.model_copy(deep=True)

    async def get(self, grant_id: UUID, principal: Principal) -> BrowserTaskGrant:
        grant = self._grants.get(grant_id)
        if grant is None or not _owned(grant, principal):
            raise NotFoundError("browser task grant not found")
        return grant.model_copy(deep=True)

    async def active_for_session(
        self, session_id: UUID, principal: Principal, *, now: datetime
    ) -> BrowserTaskGrant | None:
        return next(
            (
                grant.model_copy(deep=True)
                for grant in self._grants.values()
                if _owned(grant, principal)
                and grant.session_id == session_id
                and _active(grant, now)
            ),
            None,
        )

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
        if (after_created_at is None) != (after_id is None):
            raise ValueError("pagination cursor components must be provided together")
        grants = [
            grant.model_copy(deep=True)
            for grant in self._grants.values()
            if _owned(grant, principal)
            and (session_id is None or grant.session_id == session_id)
            and (not active_only or _active(grant, now))
        ]
        ordered = sorted(grants, key=lambda grant: (grant.created_at, grant.id.int), reverse=True)
        if after_created_at is not None and after_id is not None:
            ordered = [
                grant
                for grant in ordered
                if (grant.created_at, grant.id.int) < (after_created_at, after_id.int)
            ]
        return ordered if limit is None else ordered[:limit]

    async def consume(
        self,
        grant_id: UUID,
        principal: Principal,
        *,
        session_id: UUID,
        typed: int,
        now: datetime,
    ) -> BrowserTaskGrant | None:
        async with self._lock:
            grant = self._grants.get(grant_id)
            if (
                grant is None
                or not _owned(grant, principal)
                or grant.session_id != session_id
                or grant.ended_at is not None
                or grant.revoked_at is not None
                or now >= grant.expires_at
                or grant.actions_used >= grant.max_actions
                or grant.typed_characters + typed > TASK_GRANT_MAX_TYPED_CHARACTERS
            ):
                return None
            used = grant.actions_used + 1
            exhausted = used >= grant.max_actions
            updated = BrowserTaskGrant.model_validate(
                grant.model_dump()
                | {
                    "actions_used": used,
                    "typed_characters": grant.typed_characters + typed,
                    "last_used_at": now,
                    "ended_at": now if exhausted else None,
                    "end_reason": BrowserTaskGrantEndReason.EXHAUSTED if exhausted else None,
                }
            )
            self._grants[grant_id] = updated
            return updated.model_copy(deep=True)

    async def end(
        self,
        grant_id: UUID,
        principal: Principal,
        *,
        reason: BrowserTaskGrantEndReason,
        now: datetime,
    ) -> tuple[BrowserTaskGrant, bool]:
        async with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None or not _owned(grant, principal):
                raise NotFoundError("browser task grant not found")
            if grant.ended_at is not None:
                return grant.model_copy(deep=True), False
            ended = _ended(grant, reason, now)
            self._grants[grant_id] = ended
            return ended.model_copy(deep=True), True

    async def end_expired(
        self, now: datetime, limit: int, *, tenant_id: str
    ) -> builtins.list[BrowserTaskGrant]:
        async with self._lock:
            due = sorted(
                (
                    grant
                    for grant in self._grants.values()
                    if grant.tenant_id == tenant_id
                    and grant.ended_at is None
                    and grant.expires_at <= now
                ),
                key=lambda grant: (grant.expires_at, grant.id.int),
            )[:limit]
            ended = [_ended(grant, BrowserTaskGrantEndReason.EXPIRED, now) for grant in due]
            for grant in ended:
                self._grants[grant.id] = grant
            return [grant.model_copy(deep=True) for grant in ended]

    async def end_for_profile(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        reason: BrowserTaskGrantEndReason,
        now: datetime,
    ) -> builtins.list[BrowserTaskGrant]:
        async with self._lock:
            ended = [
                _ended(grant, reason, now)
                for grant in self._grants.values()
                if _owned(grant, principal)
                and grant.profile_id == profile_id
                and grant.ended_at is None
            ]
            for grant in ended:
                self._grants[grant.id] = grant
            return [grant.model_copy(deep=True) for grant in ended]


def _owned(grant: BrowserTaskGrant, principal: Principal) -> bool:
    return grant.tenant_id == principal.tenant_id and grant.principal_id == principal.principal_id


def _active(grant: BrowserTaskGrant, now: datetime) -> bool:
    return grant.ended_at is None and now < grant.expires_at


def _ended(
    grant: BrowserTaskGrant, reason: BrowserTaskGrantEndReason, now: datetime
) -> BrowserTaskGrant:
    return BrowserTaskGrant.model_validate(
        grant.model_dump()
        | {
            "ended_at": now,
            "end_reason": reason,
            "revoked_at": now if reason is BrowserTaskGrantEndReason.REVOKED else None,
        }
    )
