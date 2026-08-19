"""Shared contract for isolated browser-profile control planes."""

from __future__ import annotations

import inspect
from uuid import UUID

import pytest

from agent_core.adapters.browser.profiles import InMemoryBrowserProfileControlPlane
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.ports.browser_profiles import BrowserProfileControlPlane
from tests.contract.support import principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000d0")


async def assert_control_plane_provisioning_is_scoped_and_recoverably_idempotent(
    control_plane: BrowserProfileControlPlane,
) -> None:
    first = await control_plane.provision(
        PROFILE_ID,
        principal(),
        ("https://example.org",),
    )
    replay = await control_plane.provision(
        PROFILE_ID,
        principal(),
        ("https://example.org",),
    )

    assert replay == first
    assert first.provider_ref
    assert first.encryption_key_version

    foreign = Principal(
        tenant_id=principal().tenant_id,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )
    with pytest.raises(ConflictError):
        await control_plane.provision(
            PROFILE_ID,
            foreign,
            ("https://example.org",),
        )


async def assert_control_plane_revocation_and_deletion_are_idempotent(
    control_plane: BrowserProfileControlPlane,
) -> None:
    provisioned = await control_plane.provision(
        PROFILE_ID,
        principal(),
        ("https://example.org",),
    )

    await control_plane.revoke(PROFILE_ID, principal(), provisioned.provider_ref)
    await control_plane.revoke(PROFILE_ID, principal(), provisioned.provider_ref)
    await control_plane.delete(PROFILE_ID, principal(), provisioned.provider_ref)
    await control_plane.delete(PROFILE_ID, principal(), provisioned.provider_ref)


async def assert_control_plane_lifecycle_rejects_scope_mismatch(
    control_plane: BrowserProfileControlPlane,
) -> None:
    provisioned = await control_plane.provision(
        PROFILE_ID,
        principal(),
        ("https://example.org",),
    )
    foreign = Principal(
        tenant_id=principal().tenant_id,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )

    with pytest.raises(ConflictError):
        await control_plane.revoke(PROFILE_ID, foreign, provisioned.provider_ref)
    with pytest.raises(ConflictError):
        await control_plane.delete(PROFILE_ID, foreign, provisioned.provider_ref)


async def test_control_plane_provisioning_is_scoped_and_recoverably_idempotent() -> None:
    await assert_control_plane_provisioning_is_scoped_and_recoverably_idempotent(
        InMemoryBrowserProfileControlPlane()
    )


async def test_control_plane_revocation_and_deletion_are_idempotent() -> None:
    await assert_control_plane_revocation_and_deletion_are_idempotent(
        InMemoryBrowserProfileControlPlane()
    )


async def test_control_plane_lifecycle_rejects_scope_mismatch() -> None:
    await assert_control_plane_lifecycle_rejects_scope_mismatch(
        InMemoryBrowserProfileControlPlane()
    )


def test_control_plane_has_no_material_read_surface() -> None:
    public_methods = {
        name
        for name, member in inspect.getmembers(
            InMemoryBrowserProfileControlPlane,
            predicate=inspect.isfunction,
        )
        if not name.startswith("_")
    }

    assert public_methods == {"provision", "revoke", "delete"}
