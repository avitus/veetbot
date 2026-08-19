"""Lifecycle service core for the isolated browser-profile deployment."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from agent_core.browser_control_plane.models import (
    ProfileMaterialIdentity,
    ProfileMaterialMetadata,
)
from agent_core.browser_control_plane.ports import EncryptedProfileStore
from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserProfileProvisioning
from agent_core.domain.errors import ConflictError
from agent_core.ports.browser_profiles import BrowserProfileControlPlane

INITIAL_PROFILE_MATERIAL = b'{"format_version":1}'
PROVIDER_NAME = "hosted-isolated"


def _identity(metadata: ProfileMaterialMetadata) -> ProfileMaterialIdentity:
    return ProfileMaterialIdentity(
        profile_id=metadata.profile_id,
        tenant_id=metadata.tenant_id,
        principal_id=metadata.principal_id,
        provider_ref=metadata.provider_ref,
        allowed_origins=metadata.allowed_origins,
    )


class HostedProfileLifecycleService(BrowserProfileControlPlane):
    def __init__(
        self,
        store: EncryptedProfileStore,
        *,
        reference_factory: Callable[[], str],
        invalidate_profile: Callable[[UUID], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._reference_factory = reference_factory
        self._invalidate_profile = invalidate_profile

    @staticmethod
    def _provisioning(metadata: ProfileMaterialMetadata) -> BrowserProfileProvisioning:
        return BrowserProfileProvisioning(
            provider_name=PROVIDER_NAME,
            provider_ref=metadata.provider_ref,
            encryption_key_version=metadata.encryption_key_version,
        )

    async def _owned_metadata(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> ProfileMaterialMetadata | None:
        by_profile = await self._store.find_by_profile(profile_id)
        by_ref = await self._store.find_by_ref(provider_ref)
        if by_profile is None and by_ref is None:
            return None
        if by_profile is None or by_ref is None or by_profile != by_ref:
            raise ConflictError("browser profile lifecycle identity conflicts")
        if (
            by_profile.tenant_id != principal.tenant_id
            or by_profile.principal_id != principal.principal_id
        ):
            raise ConflictError("browser profile lifecycle scope mismatch")
        return by_profile

    async def provision(
        self,
        profile_id: UUID,
        principal: Principal,
        allowed_origins: tuple[str, ...],
    ) -> BrowserProfileProvisioning:
        existing = await self._store.find_by_profile(profile_id)
        if existing is not None:
            if (
                existing.tenant_id != principal.tenant_id
                or existing.principal_id != principal.principal_id
                or existing.allowed_origins != allowed_origins
                or existing.revoked
            ):
                raise ConflictError("browser profile provisioning scope conflicts")
            return self._provisioning(existing)
        identity = ProfileMaterialIdentity(
            profile_id=profile_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            provider_ref=self._reference_factory(),
            allowed_origins=allowed_origins,
        )
        metadata = await self._store.create(identity, INITIAL_PROFILE_MATERIAL)
        return self._provisioning(metadata)

    async def revoke(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> None:
        metadata = await self._owned_metadata(profile_id, principal, provider_ref)
        if metadata is not None:
            await self._store.revoke(_identity(metadata))
        if self._invalidate_profile is not None:
            await self._invalidate_profile(profile_id)

    async def delete(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> None:
        metadata = await self._owned_metadata(profile_id, principal, provider_ref)
        if metadata is not None:
            if self._invalidate_profile is not None:
                await self._invalidate_profile(profile_id)
            await self._store.delete(_identity(metadata))

    async def rotate_all(self) -> int:
        rotated = 0
        for metadata in await self._store.list_metadata():
            updated = await self._store.rotate(_identity(metadata))
            if updated.encryption_key_version != metadata.encryption_key_version:
                rotated += 1
        return rotated
