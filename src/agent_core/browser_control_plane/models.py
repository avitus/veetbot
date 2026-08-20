"""Internal identities and failures for encrypted browser-profile material."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core.domain.browser import normalize_browser_origin
from agent_core.domain.errors import AgentCoreError


class ProfileMaterialIdentity(BaseModel):
    """Complete authenticated scope of one opaque material record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    provider_ref: str = Field(min_length=32, max_length=512, repr=False)
    allowed_origins: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("allowed_origins")
    @classmethod
    def origins_are_normalized_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_browser_origin(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("profile material origins must be unique")
        return normalized


class ProfileMaterialMetadata(ProfileMaterialIdentity):
    """Non-secret authenticated metadata stored with one ciphertext."""

    format_version: int = Field(default=1, ge=1, le=1)
    encryption_key_version: str = Field(min_length=1, max_length=128)
    revoked: bool = False

    def identity(self) -> ProfileMaterialIdentity:
        return ProfileMaterialIdentity.model_validate(
            self.model_dump(include=set(ProfileMaterialIdentity.model_fields))
        )


class ProfileStoreIntegrityError(AgentCoreError):
    """Encrypted profile storage failed closed without exposing material."""
