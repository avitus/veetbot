"""Bounded recovery of verified sign-in continuations from durable checkpoints."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAuthenticationStatus,
    BrowserAuthenticationView,
    BrowserAuthenticationWait,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory

logger = logging.getLogger(__name__)


class VerifiedBrowserRunResumer(Protocol):
    async def resume_verified_browser_authentication(
        self,
        principal: Principal,
        run_id: UUID,
        authentication_id: UUID,
    ) -> bool: ...


class AuthenticationReader(Protocol):
    async def authentication_status(
        self,
        principal: Principal,
        authentication_id: UUID,
    ) -> BrowserAuthenticationView: ...


class BrowserContinuationService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        runs: VerifiedBrowserRunResumer,
        profiles: AuthenticationReader,
        principal: Principal,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._runs = runs
        self._profiles = profiles
        self._principal = principal
        self._cursor: UUID | None = None

    async def resume_profile(self, principal: Principal, profile_id: UUID) -> int:
        """Fast path after a recorded service outcome; the sweep covers later pages."""
        return await self._scan(principal, profile_id=profile_id, refresh=False, paginate=False)

    async def sweep(self) -> int:
        return await self._scan(self._principal, profile_id=None, refresh=True, paginate=True)

    async def _scan(
        self, principal: Principal, *, profile_id: UUID | None, refresh: bool, paginate: bool
    ) -> int:
        async with self._uow_factory() as uow:
            runs = await uow.runs.waiting_for_user(
                principal, limit=32, after_id=self._cursor if paginate else None
            )
        resumed = 0
        # One unavailable provider cannot hold maintenance indefinitely. Advance
        # after each visited run so a failed first page cannot starve later waits.
        try:
            async with asyncio.timeout(45):
                for run in runs:
                    if paginate:
                        self._cursor = run.id
                    try:
                        async with self._uow_factory() as uow:
                            checkpoint = await uow.checkpoints.latest(run.id)
                            raw = (
                                None
                                if checkpoint is None
                                else checkpoint.working_state.get("browser_auth_wait")
                            )
                            if raw is None:
                                continue
                            wait = BrowserAuthenticationWait.model_validate(raw)
                            if wait.expires_at <= self._clock.now() or (
                                profile_id is not None and wait.profile_id != profile_id
                            ):
                                continue
                            records = await uow.browser_authentications.list(
                                principal, profile_id=wait.profile_id
                            )
                        if not records:
                            continue
                        newest = max(records, key=lambda record: (record.created_at, record.id.int))
                        if newest.created_at < wait.started_at:
                            continue
                        if refresh and newest.status in {
                            BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
                            BrowserAuthenticationStatus.NEEDS_USER,
                        }:
                            async with asyncio.timeout(35):
                                await self._profiles.authentication_status(principal, newest.id)
                        resumed += await self._runs.resume_verified_browser_authentication(
                            principal, run.id, newest.id
                        )
                    except Exception as error:
                        logger.warning("Browser continuation deferred (%s)", type(error).__name__)
        except TimeoutError:
            return resumed
        if paginate and len(runs) < 32:
            self._cursor = None
        return resumed
