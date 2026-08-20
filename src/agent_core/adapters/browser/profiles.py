"""In-memory browser-profile metadata adapter for contracts and evaluation."""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    ALLOWED_BROWSER_PROFILE_TRANSITIONS,
    BrowserProfile,
    BrowserProfileProvisioning,
    BrowserProfileStatus,
)
from agent_core.domain.errors import ConcurrencyConflict, ConflictError, NotFoundError


def _copy(profile: BrowserProfile) -> BrowserProfile:
    return profile.model_copy(deep=True)


def _owned(profile: BrowserProfile, principal: Principal) -> bool:
    return (
        profile.tenant_id == principal.tenant_id and profile.principal_id == principal.principal_id
    )


class InMemoryBrowserProfileRepository:
    def __init__(self) -> None:
        self._profiles: dict[UUID, BrowserProfile] = {}

    async def create(self, profile: BrowserProfile) -> BrowserProfile:
        if profile.id in self._profiles:
            raise ConflictError("browser profile already exists")
        self._profiles[profile.id] = _copy(profile)
        return _copy(profile)

    async def get(self, profile_id: UUID, principal: Principal) -> BrowserProfile:
        profile = self._profiles.get(profile_id)
        if profile is None or not _owned(profile, principal):
            raise NotFoundError("browser profile not found")
        return _copy(profile)

    async def list(
        self,
        principal: Principal,
        *,
        limit: int | None = None,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[BrowserProfile]:
        if (after_created_at is None) != (after_id is None):
            raise ValueError("pagination cursor components must be provided together")
        profiles = [
            _copy(profile) for profile in self._profiles.values() if _owned(profile, principal)
        ]
        ordered = sorted(profiles, key=lambda profile: (profile.created_at, str(profile.id)))
        if after_created_at is not None and after_id is not None:
            ordered = [
                profile
                for profile in ordered
                if (profile.created_at, str(profile.id)) > (after_created_at, str(after_id))
            ]
        return ordered if limit is None else ordered[:limit]

    async def bind(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
        provisioning: BrowserProfileProvisioning,
        updated_at: datetime,
    ) -> BrowserProfile:
        profile = await self.get(profile_id, principal)
        if profile.generation != expected_generation:
            raise ConcurrencyConflict("browser profile generation changed")
        if profile.status is not BrowserProfileStatus.PROVISIONING:
            raise ConflictError("browser profile is not awaiting a provider binding")
        if updated_at < profile.updated_at:
            raise ConflictError("browser profile update time moved backwards")
        if any(
            existing.id != profile_id
            and existing.tenant_id == principal.tenant_id
            and existing.provider_ref == provisioning.provider_ref
            for existing in self._profiles.values()
        ):
            raise ConflictError("browser provider reference is already bound")
        updated = profile.model_copy(
            update={
                "provider_name": provisioning.provider_name,
                "provider_ref": provisioning.provider_ref,
                "encryption_key_version": provisioning.encryption_key_version,
                "status": BrowserProfileStatus.AUTHENTICATION_REQUIRED,
                "generation": profile.generation + 1,
                "updated_at": updated_at,
            },
            deep=True,
        )
        self._profiles[profile_id] = updated
        return _copy(updated)

    async def transition(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
        status: BrowserProfileStatus,
        updated_at: datetime,
    ) -> BrowserProfile:
        profile = await self.get(profile_id, principal)
        if profile.generation != expected_generation:
            raise ConcurrencyConflict("browser profile generation changed")
        if profile.status is status:
            return profile
        if status not in ALLOWED_BROWSER_PROFILE_TRANSITIONS[profile.status]:
            raise ConflictError("browser profile transition is not allowed")
        if updated_at < profile.updated_at:
            raise ConflictError("browser profile update time moved backwards")
        updated = profile.model_copy(
            update={
                "status": status,
                "generation": profile.generation + 1,
                "updated_at": updated_at,
            },
            deep=True,
        )
        self._profiles[profile_id] = updated
        return _copy(updated)

    async def delete(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
    ) -> None:
        profile = self._profiles.get(profile_id)
        if profile is None or not _owned(profile, principal):
            return
        if profile.generation != expected_generation:
            raise ConcurrencyConflict("browser profile generation changed")
        if profile.status is not BrowserProfileStatus.REVOKED:
            raise ConflictError("browser profile must be revoked before deletion")
        del self._profiles[profile_id]


class InMemoryBrowserProfileControlPlane:
    """Secret-free simulation of the isolated provider control plane."""

    def __init__(self) -> None:
        self._by_profile: dict[
            UUID,
            tuple[str, str, tuple[str, ...], BrowserProfileProvisioning, bool, bool],
        ] = {}
        self._by_ref: dict[str, UUID] = {}

    async def provision(
        self,
        profile_id: UUID,
        principal: Principal,
        allowed_origins: tuple[str, ...],
    ) -> BrowserProfileProvisioning:
        existing = self._by_profile.get(profile_id)
        identity = (principal.tenant_id, principal.principal_id, allowed_origins)
        if existing is not None:
            if existing[:3] != identity or existing[5]:
                raise ConflictError("browser profile provisioning conflicts with existing state")
            return existing[3].model_copy(deep=True)
        provider_ref = secrets.token_urlsafe(32)
        provisioning = BrowserProfileProvisioning(
            provider_name="in-memory-profile-control-plane",
            provider_ref=provider_ref,
            encryption_key_version="in-memory-no-material-v1",
        )
        self._by_profile[profile_id] = (*identity, provisioning, False, False)
        self._by_ref[provider_ref] = profile_id
        return provisioning.model_copy(deep=True)

    def _scoped_profile(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> tuple[str, str, tuple[str, ...], BrowserProfileProvisioning, bool, bool] | None:
        resolved_profile_id = self._by_ref.get(provider_ref)
        if resolved_profile_id is None:
            return None
        profile = self._by_profile[resolved_profile_id]
        if (
            resolved_profile_id != profile_id
            or profile[0] != principal.tenant_id
            or profile[1] != principal.principal_id
        ):
            raise ConflictError("browser profile control-plane scope mismatch")
        return profile

    async def revoke(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> None:
        profile = self._scoped_profile(profile_id, principal, provider_ref)
        if profile is None:
            return
        tenant_id, principal_id, origins, provisioning, _revoked, deleted = profile
        self._by_profile[profile_id] = (
            tenant_id,
            principal_id,
            origins,
            provisioning,
            True,
            deleted,
        )

    async def delete(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> None:
        profile = self._scoped_profile(profile_id, principal, provider_ref)
        if profile is None:
            return
        tenant_id, principal_id, origins, provisioning, revoked, _deleted = profile
        self._by_profile[profile_id] = (
            tenant_id,
            principal_id,
            origins,
            provisioning,
            revoked,
            True,
        )
        del self._by_ref[provider_ref]
