"""Principal-explicit lifecycle service for browser profile metadata."""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProfileView,
    normalize_browser_origin,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.browser_profiles import (
    BrowserProfileControlPlane,
    BrowserProfileRepository,
)
from agent_core.ports.determinism import Clock, IdFactory


class BrowserProfileService:
    def __init__(
        self,
        repository: BrowserProfileRepository,
        control_plane: BrowserProfileControlPlane,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._repository = repository
        self._control_plane = control_plane
        self._clock = clock
        self._ids = ids

    @staticmethod
    def _view(profile: BrowserProfile) -> BrowserProfileView:
        return BrowserProfileView(
            id=profile.id,
            allowed_origins=profile.allowed_origins,
            status=profile.status,
            generation=profile.generation,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
            last_used_at=profile.last_used_at,
        )

    async def create(
        self,
        principal: Principal,
        allowed_origins: tuple[str, ...],
    ) -> BrowserProfileView:
        normalized = tuple(normalize_browser_origin(origin) for origin in allowed_origins)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("browser profile requires unique allowed origins")
        profile_id = self._ids.new_id()
        now = self._clock.now()
        reservation = BrowserProfile(
            id=profile_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            allowed_origins=normalized,
            status=BrowserProfileStatus.PROVISIONING,
            generation=0,
            created_at=now,
            updated_at=now,
        )
        await self._repository.create(reservation)
        try:
            provisioning = await self._control_plane.provision(
                profile_id,
                principal,
                normalized,
            )
        except Exception as provision_error:
            try:
                await self._repository.transition(
                    profile_id,
                    principal,
                    expected_generation=0,
                    status=BrowserProfileStatus.REVOKED,
                    updated_at=self._clock.now(),
                )
            except Exception as cleanup_error:
                raise ExceptionGroup(
                    "browser profile provisioning and reservation cleanup failed",
                    [provision_error, cleanup_error],
                ) from provision_error
            raise
        try:
            bound = await self._repository.bind(
                profile_id,
                principal,
                expected_generation=0,
                provisioning=provisioning,
                updated_at=self._clock.now(),
            )
        except Exception as bind_error:
            cleanup_errors: list[Exception] = []
            try:
                await self._control_plane.delete(
                    profile_id,
                    principal,
                    provisioning.provider_ref,
                )
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
            try:
                await self._repository.transition(
                    profile_id,
                    principal,
                    expected_generation=0,
                    status=BrowserProfileStatus.REVOKED,
                    updated_at=self._clock.now(),
                )
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ExceptionGroup(
                    "browser profile bind and compensation failed",
                    [bind_error, *cleanup_errors],
                ) from bind_error
            raise
        return self._view(bound)

    async def get(self, principal: Principal, profile_id: UUID) -> BrowserProfileView:
        return self._view(await self._repository.get(profile_id, principal))

    async def list(self, principal: Principal) -> list[BrowserProfileView]:
        return [self._view(profile) for profile in await self._repository.list(principal)]

    async def revoke(self, principal: Principal, profile_id: UUID) -> BrowserProfileView:
        profile = await self._repository.get(profile_id, principal)
        if profile.status is BrowserProfileStatus.REVOKED:
            if profile.provider_ref is not None:
                await self._control_plane.revoke(
                    profile_id,
                    principal,
                    profile.provider_ref,
                )
            return self._view(profile)
        revoked = await self._repository.transition(
            profile_id,
            principal,
            expected_generation=profile.generation,
            status=BrowserProfileStatus.REVOKED,
            updated_at=self._clock.now(),
        )
        if profile.provider_ref is not None:
            await self._control_plane.revoke(
                profile_id,
                principal,
                profile.provider_ref,
            )
        return self._view(revoked)

    async def delete(self, principal: Principal, profile_id: UUID) -> None:
        try:
            profile = await self._repository.get(profile_id, principal)
        except NotFoundError:
            return
        if profile.status is not BrowserProfileStatus.REVOKED:
            raise ConflictError("browser profile must be revoked before deletion")
        if profile.provider_ref is not None:
            await self._control_plane.delete(
                profile_id,
                principal,
                profile.provider_ref,
            )
        await self._repository.delete(
            profile_id,
            principal,
            expected_generation=profile.generation,
        )
