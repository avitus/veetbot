"""Contract for the isolated hosted-profile lifecycle service core."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from tests.contract.support import principal
from tests.contract.test_browser_profile_control_plane_contract import (
    assert_control_plane_lifecycle_rejects_scope_mismatch,
    assert_control_plane_provisioning_is_scoped_and_recoverably_idempotent,
    assert_control_plane_revocation_and_deletion_are_idempotent,
)

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000f2")
PROVIDER_REF = "opaque-service-reference-0000000000000001"


def service(root: Path, *, current_version: str = "key-v1") -> HostedProfileLifecycleService:
    versions = ("key-v1", "key-v2")
    keyring = StaticProfileKeyring(
        {
            version: hashlib.sha256(f"synthetic-service-key:{version}".encode()).digest()
            for version in versions
        },
        current_version=current_version,
    )
    return HostedProfileLifecycleService(
        FilesystemEncryptedProfileStore(root, keyring),
        reference_factory=lambda: PROVIDER_REF,
    )


async def test_hosted_lifecycle_service_passes_shared_control_plane_contract(
    tmp_path: Path,
) -> None:
    await assert_control_plane_provisioning_is_scoped_and_recoverably_idempotent(
        service(tmp_path / "provisioning")
    )
    await assert_control_plane_revocation_and_deletion_are_idempotent(
        service(tmp_path / "lifecycle")
    )
    await assert_control_plane_lifecycle_rejects_scope_mismatch(service(tmp_path / "scope"))


async def test_lifecycle_service_allows_scoped_provisioning_compensation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profiles"
    lifecycle = service(root)
    provisioned = await lifecycle.provision(
        PROFILE_ID,
        principal(),
        ("https://example.org",),
    )
    await lifecycle.delete(PROFILE_ID, principal(), provisioned.provider_ref)

    assert list(root.glob("*.profile")) == []


async def test_lifecycle_service_provision_is_durable_and_scope_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profiles"
    first = await service(root).provision(
        PROFILE_ID,
        principal(),
        ("https://example.org",),
    )
    replay = await service(root).provision(
        PROFILE_ID,
        principal(),
        ("https://example.org",),
    )

    assert replay == first
    assert first.provider_name == "hosted-isolated"
    assert first.provider_ref == PROVIDER_REF
    assert first.encryption_key_version == "key-v1"

    foreign = Principal(
        tenant_id=principal().tenant_id,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )
    with pytest.raises(ConflictError):
        await service(root).provision(
            PROFILE_ID,
            foreign,
            ("https://example.org",),
        )
    with pytest.raises(ConflictError):
        await service(root).provision(
            PROFILE_ID,
            principal(),
            ("https://example.net",),
        )


async def test_lifecycle_service_normalizes_origins_before_idempotency_comparison(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profiles"

    first = await service(root).provision(
        PROFILE_ID,
        principal(),
        ("https://Example.ORG",),
    )
    replay = await service(root).provision(
        PROFILE_ID,
        principal(),
        ("https://example.org/",),
    )

    assert replay == first


async def test_lifecycle_service_revoke_delete_and_rotation_are_restart_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profiles"
    lifecycle = service(root)
    provisioned = await lifecycle.provision(
        PROFILE_ID,
        principal(),
        ("https://example.org",),
    )
    await lifecycle.revoke(PROFILE_ID, principal(), provisioned.provider_ref)
    await service(root).revoke(PROFILE_ID, principal(), provisioned.provider_ref)

    assert await service(root, current_version="key-v2").rotate_all() == 1
    assert await service(root, current_version="key-v2").rotate_all() == 0

    await service(root, current_version="key-v2").delete(
        PROFILE_ID,
        principal(),
        provisioned.provider_ref,
    )
    await service(root, current_version="key-v2").delete(
        PROFILE_ID,
        principal(),
        provisioned.provider_ref,
    )
    assert list(root.glob("*.profile")) == []
