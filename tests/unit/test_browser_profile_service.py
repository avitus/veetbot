"""Browser profile lifecycle orchestration and secret-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest

from agent_core.adapters.browser.profiles import InMemoryBrowserProfileRepository
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.application.browser_profiles import BrowserProfileService
from agent_core.domain.browser import BrowserProfileProvisioning, BrowserProfileStatus
from agent_core.domain.errors import ConflictError
from tests.contract.support import NOW, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000c0")


@dataclass
class FakeProfileControlPlane:
    provider_ref: str = "secret-provider-reference"
    operations: list[str] = field(default_factory=list)

    async def provision(
        self,
        profile_id: UUID,
        principal: object,
        allowed_origins: tuple[str, ...],
    ) -> BrowserProfileProvisioning:
        del principal
        self.operations.append(f"provision:{profile_id}:{','.join(allowed_origins)}")
        return BrowserProfileProvisioning(
            provider_name="isolated-hosted",
            provider_ref=self.provider_ref,
            encryption_key_version="key-v1",
        )

    async def revoke(self, profile_id: UUID, principal: object, provider_ref: str) -> None:
        assert profile_id == PROFILE_ID
        assert principal == globals()["principal"]()
        assert provider_ref == self.provider_ref
        self.operations.append("provider:revoke")

    async def delete(self, profile_id: UUID, principal: object, provider_ref: str) -> None:
        assert profile_id == PROFILE_ID
        assert principal == globals()["principal"]()
        assert provider_ref == self.provider_ref
        self.operations.append("provider:delete")


def service(
    repository: InMemoryBrowserProfileRepository,
    control_plane: FakeProfileControlPlane,
) -> BrowserProfileService:
    return BrowserProfileService(
        repository,
        control_plane,
        FixedClock(NOW),
        SequenceIdFactory([PROFILE_ID]),
    )


async def test_create_provisions_then_persists_secret_free_scoped_view() -> None:
    repository = InMemoryBrowserProfileRepository()
    control_plane = FakeProfileControlPlane()

    result = await service(repository, control_plane).create(
        principal(),
        ("https://Example.org/",),
    )

    assert result.id == PROFILE_ID
    assert result.status is BrowserProfileStatus.AUTHENTICATION_REQUIRED
    assert result.allowed_origins == ("https://example.org",)
    assert "provider_ref" not in result.model_dump()
    assert control_plane.provider_ref not in result.model_dump_json()
    stored = await repository.get(PROFILE_ID, principal())
    assert stored.provider_ref == control_plane.provider_ref
    assert control_plane.operations == [f"provision:{PROFILE_ID}:https://example.org"]


async def test_create_rejects_invalid_origin_before_control_plane() -> None:
    repository = InMemoryBrowserProfileRepository()
    control_plane = FakeProfileControlPlane()

    with pytest.raises(ValueError):
        await service(repository, control_plane).create(
            principal(),
            ("http://localhost",),
        )

    assert control_plane.operations == []


async def test_create_rejects_duplicate_reservation_before_provider_provisioning() -> None:
    repository = InMemoryBrowserProfileRepository()
    control_plane = FakeProfileControlPlane()
    profiles = service(repository, control_plane)
    await profiles.create(principal(), ("https://example.org",))

    operations_before_duplicate = list(control_plane.operations)
    with pytest.raises(ConflictError):
        await service(repository, control_plane).create(
            principal(),
            ("https://example.org",),
        )

    assert control_plane.operations == operations_before_duplicate


async def test_revoke_commits_fail_closed_metadata_before_provider_cleanup() -> None:
    operations: list[str] = []

    class OrderedRepository(InMemoryBrowserProfileRepository):
        async def transition(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            result = await super().transition(*args, **kwargs)  # type: ignore[arg-type]
            operations.append("metadata:revoked")
            return result

    class OrderedControlPlane(FakeProfileControlPlane):
        async def revoke(self, profile_id: UUID, principal: object, provider_ref: str) -> None:
            operations.append("provider:revoke")
            await super().revoke(profile_id, principal, provider_ref)

    repository = OrderedRepository()
    control_plane = OrderedControlPlane()
    profiles = service(repository, control_plane)
    created = await profiles.create(principal(), ("https://example.org",))

    revoked = await profiles.revoke(principal(), created.id)

    assert revoked.status is BrowserProfileStatus.REVOKED
    assert revoked.generation == created.generation + 1
    assert operations == ["metadata:revoked", "provider:revoke"]


async def test_delete_requires_revocation_and_is_idempotent() -> None:
    repository = InMemoryBrowserProfileRepository()
    control_plane = FakeProfileControlPlane()
    profiles = service(repository, control_plane)
    created = await profiles.create(principal(), ("https://example.org",))

    with pytest.raises(ConflictError):
        await profiles.delete(principal(), created.id)

    await profiles.revoke(principal(), created.id)
    await profiles.delete(principal(), created.id)
    await profiles.delete(principal(), created.id)

    assert control_plane.operations.count("provider:delete") == 1
