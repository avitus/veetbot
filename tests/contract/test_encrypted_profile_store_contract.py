"""Shared security contract for encrypted browser-profile material stores."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.models import (
    ProfileMaterialIdentity,
    ProfileStoreIntegrityError,
)
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.domain.errors import ConflictError

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000f0")
OTHER_PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000f1")
PROVIDER_REF = "opaque-profile-reference-0000000000000001"
OTHER_PROVIDER_REF = "opaque-profile-reference-0000000000000002"
PLAINTEXT_MARKER = b"synthetic-cookie-material-must-stay-encrypted"


def synthetic_key(version: str) -> bytes:
    return hashlib.sha256(f"synthetic-profile-key:{version}".encode()).digest()


def keyring(current: str = "key-v1", *versions: str) -> StaticProfileKeyring:
    named = versions or (current,)
    return StaticProfileKeyring(
        {version: synthetic_key(version) for version in named},
        current_version=current,
    )


def identity(
    *,
    profile_id: UUID = PROFILE_ID,
    provider_ref: str = PROVIDER_REF,
    principal_id: str = "principal-a",
    allowed_origins: tuple[str, ...] = ("https://example.org",),
) -> ProfileMaterialIdentity:
    return ProfileMaterialIdentity(
        profile_id=profile_id,
        tenant_id="tenant-a",
        principal_id=principal_id,
        provider_ref=provider_ref,
        allowed_origins=allowed_origins,
    )


def store(root: Path, keys: StaticProfileKeyring | None = None) -> FilesystemEncryptedProfileStore:
    return FilesystemEncryptedProfileStore(root, keys or keyring())


async def test_encrypted_store_round_trips_across_restart_without_plaintext(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profiles"
    first = store(root)
    metadata = await first.create(identity(), PLAINTEXT_MARKER)

    files = list(root.glob("*.profile"))
    assert len(files) == 1
    durable = files[0].read_bytes()
    assert PLAINTEXT_MARKER not in durable
    assert metadata.encryption_key_version == "key-v1"
    assert await store(root).load(identity()) == PLAINTEXT_MARKER


async def test_encrypted_store_uses_fresh_nonce_and_hashed_filenames(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    adapter = store(root)
    await adapter.create(identity(), PLAINTEXT_MARKER)
    await adapter.create(
        identity(profile_id=OTHER_PROFILE_ID, provider_ref=OTHER_PROVIDER_REF),
        PLAINTEXT_MARKER,
    )

    envelopes = [json.loads(path.read_text()) for path in sorted(root.glob("*.profile"))]
    assert envelopes[0]["nonce"] != envelopes[1]["nonce"]
    assert all(PROVIDER_REF not in path.name for path in root.iterdir())
    assert all(OTHER_PROVIDER_REF not in path.name for path in root.iterdir())


async def test_encrypted_store_enforces_exact_scope_and_unique_identities(
    tmp_path: Path,
) -> None:
    adapter = store(tmp_path / "profiles")
    await adapter.create(identity(), PLAINTEXT_MARKER)

    with pytest.raises(ConflictError):
        await adapter.load(identity(principal_id="principal-b"))
    with pytest.raises(ConflictError):
        await adapter.create(identity(provider_ref=OTHER_PROVIDER_REF), b"different")
    with pytest.raises(ConflictError):
        await adapter.create(
            identity(profile_id=OTHER_PROFILE_ID),
            b"different",
        )


async def test_encrypted_store_rechecks_uniqueness_across_live_instances(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profiles"
    first = store(root)
    stale_second = store(root)
    await first.create(identity(), PLAINTEXT_MARKER)

    with pytest.raises(ConflictError):
        await stale_second.create(identity(provider_ref=OTHER_PROVIDER_REF), b"different")


async def test_encrypted_store_revocation_fences_load_and_survives_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profiles"
    adapter = store(root)
    await adapter.create(identity(), PLAINTEXT_MARKER)
    await adapter.revoke(identity())
    await adapter.revoke(identity())

    with pytest.raises(ConflictError):
        await adapter.load(identity())
    restarted = store(root)
    metadata = await restarted.find_by_profile(PROFILE_ID)
    assert metadata is not None and metadata.revoked is True
    with pytest.raises(ConflictError):
        await restarted.load(identity())


async def test_encrypted_store_delete_is_scoped_durable_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    adapter = store(root)
    await adapter.create(identity(), PLAINTEXT_MARKER)

    with pytest.raises(ConflictError):
        await adapter.delete(identity(principal_id="principal-b"))
    await adapter.delete(identity())
    await adapter.delete(identity())

    restarted = store(root)
    assert await restarted.find_by_profile(PROFILE_ID) is None
    assert list(root.glob("*.profile")) == []


@pytest.mark.parametrize("field", ["tenant_id", "ciphertext"])
async def test_encrypted_store_rejects_tampered_envelopes_on_restart(
    tmp_path: Path,
    field: str,
) -> None:
    root = tmp_path / "profiles"
    adapter = store(root)
    await adapter.create(identity(), PLAINTEXT_MARKER)
    path = next(root.glob("*.profile"))
    envelope = json.loads(path.read_text())
    envelope[field] = "tampered"
    path.write_text(json.dumps(envelope))

    with pytest.raises(ProfileStoreIntegrityError):
        store(root)


async def test_encrypted_store_atomic_failure_preserves_previous_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "profiles"
    adapter = store(root)
    await adapter.create(identity(), PLAINTEXT_MARKER)

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("agent_core.browser_control_plane.filesystem.os.replace", fail_replace)
    with pytest.raises(OSError):
        await adapter.write(identity(), b"replacement material")
    monkeypatch.undo()

    assert await store(root).load(identity()) == PLAINTEXT_MARKER


async def test_encrypted_store_rotation_is_restartable_and_drops_old_key_dependency(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profiles"
    first = store(root, keyring("key-v1"))
    await first.create(identity(), PLAINTEXT_MARKER)

    rotating = store(root, keyring("key-v2", "key-v1", "key-v2"))
    rotated = await rotating.rotate(identity())
    repeated = await rotating.rotate(identity())
    assert rotated.encryption_key_version == "key-v2"
    assert repeated == rotated

    current_only = store(root, keyring("key-v2"))
    assert await current_only.load(identity()) == PLAINTEXT_MARKER
    assert {item.encryption_key_version for item in await current_only.list_metadata()} == {
        "key-v2"
    }


def test_encrypted_store_fails_closed_for_permissions_key_and_schema(tmp_path: Path) -> None:
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    os.chmod(insecure, 0o755)
    with pytest.raises(ProfileStoreIntegrityError):
        store(insecure)

    root = tmp_path / "profiles"
    root.mkdir(mode=0o700)
    (root / "broken.profile").write_text("not-json")
    with pytest.raises(ProfileStoreIntegrityError):
        store(root)

    with pytest.raises(ValueError):
        StaticProfileKeyring({"key-v1": b"short"}, current_version="key-v1")


async def test_encrypted_store_enforces_material_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_core.browser_control_plane.filesystem.MAX_PROFILE_MATERIAL_BYTES",
        4,
    )
    with pytest.raises(ValueError):
        await store(tmp_path / "profiles").create(identity(), b"12345")
