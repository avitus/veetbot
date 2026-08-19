"""Internal ports owned by the isolated browser-profile service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from agent_core.browser_control_plane.models import (
    ProfileMaterialIdentity,
    ProfileMaterialMetadata,
    ProfileStoreIntegrityError,
)


class ProfileKeyring(Protocol):
    @property
    def current_version(self) -> str: ...

    def resolve(self, version: str) -> bytes: ...


class EncryptedProfileStore(Protocol):
    async def create(
        self,
        identity: ProfileMaterialIdentity,
        material: bytes,
    ) -> ProfileMaterialMetadata: ...

    async def find_by_profile(self, profile_id: UUID) -> ProfileMaterialMetadata | None: ...

    async def find_by_ref(self, provider_ref: str) -> ProfileMaterialMetadata | None: ...

    async def load(self, identity: ProfileMaterialIdentity) -> bytes: ...

    async def write(
        self,
        identity: ProfileMaterialIdentity,
        material: bytes,
    ) -> ProfileMaterialMetadata: ...

    async def revoke(self, identity: ProfileMaterialIdentity) -> None: ...

    async def delete(self, identity: ProfileMaterialIdentity) -> None: ...

    async def rotate(self, identity: ProfileMaterialIdentity) -> ProfileMaterialMetadata: ...

    async def list_metadata(self) -> Sequence[ProfileMaterialMetadata]: ...


class StaticProfileKeyring:
    """Immutable service-local key mapping with exact version lookup."""

    def __init__(self, keys: Mapping[str, bytes], *, current_version: str) -> None:
        self._keys = {version: bytes(value) for version, value in keys.items()}
        self._current_version = current_version
        if not self._keys or not all(
            version and len(value) == 32 for version, value in self._keys.items()
        ):
            raise ValueError("profile keyring requires named 256-bit keys")
        if current_version not in self._keys:
            raise ValueError("profile keyring current version is unavailable")

    @property
    def current_version(self) -> str:
        return self._current_version

    def resolve(self, version: str) -> bytes:
        try:
            return bytes(self._keys[version])
        except KeyError as exc:
            raise ProfileStoreIntegrityError(
                "encrypted profile names an unavailable key version"
            ) from exc
