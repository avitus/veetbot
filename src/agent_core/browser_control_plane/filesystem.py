"""AES-GCM filesystem adapter for isolated browser-profile material."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError

from agent_core.browser_control_plane.models import (
    ProfileMaterialIdentity,
    ProfileMaterialMetadata,
    ProfileStoreIntegrityError,
)
from agent_core.browser_control_plane.ports import ProfileKeyring
from agent_core.domain.errors import ConflictError

MAX_PROFILE_MATERIAL_BYTES = 64 * 1024 * 1024
MAX_PROFILE_ENVELOPE_BYTES = 90 * 1024 * 1024
NONCE_BYTES = 12
LOCK_FILENAME = ".profile-store.lock"


class _ProfileEnvelope(ProfileMaterialMetadata):
    nonce: str
    ciphertext: str


def _identity(metadata: ProfileMaterialMetadata) -> ProfileMaterialIdentity:
    return ProfileMaterialIdentity(
        profile_id=metadata.profile_id,
        tenant_id=metadata.tenant_id,
        principal_id=metadata.principal_id,
        provider_ref=metadata.provider_ref,
        allowed_origins=metadata.allowed_origins,
    )


def _aad(record: ProfileMaterialMetadata) -> bytes:
    return json.dumps(
        record.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _filename(provider_ref: str) -> str:
    return hashlib.sha256(provider_ref.encode("utf-8")).hexdigest() + ".profile"


class FilesystemEncryptedProfileStore:
    def __init__(self, root: Path, keyring: ProfileKeyring) -> None:
        if root.exists() and root.is_symlink():
            raise ProfileStoreIntegrityError("encrypted profile root cannot be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root = root.resolve()
        self._keyring = keyring
        self._lock_path = self._root / LOCK_FILENAME
        lock_descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(lock_descriptor)
        self._by_profile: dict[UUID, ProfileMaterialMetadata] = {}
        self._by_ref: dict[str, ProfileMaterialMetadata] = {}
        self._validate_root()
        with self._locked():
            self._refresh_index()

    def _validate_root(self) -> None:
        try:
            mode = stat.S_IMODE(self._root.stat().st_mode)
        except OSError as exc:
            raise ProfileStoreIntegrityError("encrypted profile root is inaccessible") from exc
        if not self._root.is_dir() or mode & 0o077:
            raise ProfileStoreIntegrityError("encrypted profile root permissions are not private")

    def _load_index(self) -> None:
        try:
            entries = tuple(self._root.iterdir())
        except OSError as exc:
            raise ProfileStoreIntegrityError("encrypted profile root is inaccessible") from exc
        removed_stage = False
        for path in entries:
            if path.name == LOCK_FILENAME and path.is_file():
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise ProfileStoreIntegrityError(
                        "encrypted profile lock permissions are not private"
                    )
                continue
            if path.name.startswith(".profile-stage-") and path.is_file():
                path.unlink()
                removed_stage = True
                continue
            if not path.is_file() or path.suffix != ".profile":
                raise ProfileStoreIntegrityError("encrypted profile root contains unknown data")
            envelope, _material = self._decode(path)
            metadata = ProfileMaterialMetadata.model_validate(
                envelope.model_dump(exclude={"nonce", "ciphertext"})
            )
            if metadata.profile_id in self._by_profile or metadata.provider_ref in self._by_ref:
                raise ProfileStoreIntegrityError(
                    "encrypted profile store contains duplicate identity"
                )
            self._by_profile[metadata.profile_id] = metadata
            self._by_ref[metadata.provider_ref] = metadata
        if removed_stage:
            self._fsync_directory()

    def _refresh_index(self) -> None:
        self._by_profile.clear()
        self._by_ref.clear()
        self._load_index()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(self._lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _path(self, provider_ref: str) -> Path:
        return self._root / _filename(provider_ref)

    def _decode(self, path: Path) -> tuple[_ProfileEnvelope, bytes]:
        try:
            if path.stat().st_size > MAX_PROFILE_ENVELOPE_BYTES:
                raise ProfileStoreIntegrityError("encrypted profile envelope exceeds its bound")
            raw = path.read_bytes()
            decoded = json.loads(raw)
            envelope = _ProfileEnvelope.model_validate(decoded)
            if path.name != _filename(envelope.provider_ref):
                raise ProfileStoreIntegrityError("encrypted profile filename does not match scope")
            nonce = base64.b64decode(envelope.nonce, validate=True)
            ciphertext = base64.b64decode(envelope.ciphertext, validate=True)
            if len(nonce) != NONCE_BYTES or len(ciphertext) > MAX_PROFILE_MATERIAL_BYTES + 16:
                raise ProfileStoreIntegrityError("encrypted profile envelope has invalid bounds")
            metadata = ProfileMaterialMetadata.model_validate(
                envelope.model_dump(exclude={"nonce", "ciphertext"})
            )
            material = AESGCM(self._keyring.resolve(metadata.encryption_key_version)).decrypt(
                nonce,
                ciphertext,
                _aad(metadata),
            )
            if len(material) > MAX_PROFILE_MATERIAL_BYTES:
                raise ProfileStoreIntegrityError("decrypted profile material exceeds its bound")
            return envelope, material
        except ProfileStoreIntegrityError:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            TypeError,
            ValidationError,
            InvalidTag,
            binascii.Error,
        ) as exc:
            raise ProfileStoreIntegrityError(
                "encrypted profile envelope failed validation"
            ) from exc

    def _metadata_for(
        self,
        identity: ProfileMaterialIdentity,
    ) -> ProfileMaterialMetadata | None:
        by_profile = self._by_profile.get(identity.profile_id)
        by_ref = self._by_ref.get(identity.provider_ref)
        if by_profile is None and by_ref is None:
            return None
        if by_profile is None or by_ref is None or by_profile != by_ref:
            raise ConflictError("browser profile material identity conflicts with existing state")
        if _identity(by_profile) != identity:
            raise ConflictError("browser profile material scope mismatch")
        return by_profile

    def _seal(self, record: ProfileMaterialMetadata, material: bytes) -> bytes:
        if len(material) > MAX_PROFILE_MATERIAL_BYTES:
            raise ValueError("browser profile material exceeds its byte bound")
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self._keyring.resolve(record.encryption_key_version)).encrypt(
            nonce,
            material,
            _aad(record),
        )
        envelope = _ProfileEnvelope(
            **record.model_dump(),
            nonce=base64.b64encode(nonce).decode("ascii"),
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        )
        return (
            json.dumps(
                envelope.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def _fsync_directory(self) -> None:
        descriptor = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _persist(
        self,
        metadata: ProfileMaterialMetadata,
        material: bytes,
        *,
        exclusive: bool,
    ) -> None:
        content = self._seal(metadata, material)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".profile-stage-",
            dir=self._root,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as staged:
                staged.write(content)
                staged.flush()
                os.fsync(staged.fileno())
            destination = self._path(metadata.provider_ref)
            if exclusive:
                try:
                    os.link(temporary, destination)
                except FileExistsError as exc:
                    raise ConflictError(
                        "browser profile provider reference already exists"
                    ) from exc
            else:
                os.replace(temporary, destination)
            self._fsync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _replace_index(
        self,
        previous: ProfileMaterialMetadata | None,
        metadata: ProfileMaterialMetadata,
    ) -> None:
        if previous is not None:
            self._by_profile.pop(previous.profile_id, None)
            self._by_ref.pop(previous.provider_ref, None)
        self._by_profile[metadata.profile_id] = metadata
        self._by_ref[metadata.provider_ref] = metadata

    async def create(
        self,
        identity: ProfileMaterialIdentity,
        material: bytes,
    ) -> ProfileMaterialMetadata:
        with self._locked():
            self._refresh_index()
            if identity.profile_id in self._by_profile or identity.provider_ref in self._by_ref:
                raise ConflictError("browser profile material identity already exists")
            metadata = ProfileMaterialMetadata(
                **identity.model_dump(),
                encryption_key_version=self._keyring.current_version,
            )
            self._persist(metadata, bytes(material), exclusive=True)
            self._replace_index(None, metadata)
            return metadata.model_copy(deep=True)

    async def find_by_profile(self, profile_id: UUID) -> ProfileMaterialMetadata | None:
        with self._locked():
            self._refresh_index()
            metadata = self._by_profile.get(profile_id)
            return None if metadata is None else metadata.model_copy(deep=True)

    async def find_by_ref(self, provider_ref: str) -> ProfileMaterialMetadata | None:
        with self._locked():
            self._refresh_index()
            metadata = self._by_ref.get(provider_ref)
            return None if metadata is None else metadata.model_copy(deep=True)

    async def load(self, identity: ProfileMaterialIdentity) -> bytes:
        with self._locked():
            self._refresh_index()
            metadata = self._metadata_for(identity)
            if metadata is None:
                raise ConflictError("browser profile material does not exist")
            if metadata.revoked:
                raise ConflictError("browser profile material is revoked")
            _envelope, material = self._decode(self._path(metadata.provider_ref))
            return bytes(material)

    async def write(
        self,
        identity: ProfileMaterialIdentity,
        material: bytes,
    ) -> ProfileMaterialMetadata:
        with self._locked():
            self._refresh_index()
            previous = self._metadata_for(identity)
            if previous is None:
                raise ConflictError("browser profile material does not exist")
            if previous.revoked:
                raise ConflictError("browser profile material is revoked")
            metadata = previous.model_copy(
                update={"encryption_key_version": self._keyring.current_version}
            )
            self._persist(metadata, bytes(material), exclusive=False)
            self._replace_index(previous, metadata)
            return metadata.model_copy(deep=True)

    async def revoke(self, identity: ProfileMaterialIdentity) -> None:
        with self._locked():
            self._refresh_index()
            previous = self._metadata_for(identity)
            if previous is None:
                return
            if previous.revoked:
                return
            _envelope, material = self._decode(self._path(previous.provider_ref))
            metadata = previous.model_copy(update={"revoked": True})
            self._persist(metadata, material, exclusive=False)
            self._replace_index(previous, metadata)

    async def delete(self, identity: ProfileMaterialIdentity) -> None:
        with self._locked():
            self._refresh_index()
            metadata = self._metadata_for(identity)
            if metadata is None:
                return
            path = self._path(metadata.provider_ref)
            path.unlink(missing_ok=True)
            self._fsync_directory()
            self._by_profile.pop(metadata.profile_id, None)
            self._by_ref.pop(metadata.provider_ref, None)

    async def rotate(self, identity: ProfileMaterialIdentity) -> ProfileMaterialMetadata:
        with self._locked():
            self._refresh_index()
            previous = self._metadata_for(identity)
            if previous is None:
                raise ConflictError("browser profile material does not exist")
            if previous.encryption_key_version == self._keyring.current_version:
                return previous.model_copy(deep=True)
            _envelope, material = self._decode(self._path(previous.provider_ref))
            metadata = previous.model_copy(
                update={"encryption_key_version": self._keyring.current_version}
            )
            self._persist(metadata, material, exclusive=False)
            self._replace_index(previous, metadata)
            return metadata.model_copy(deep=True)

    async def list_metadata(self) -> tuple[ProfileMaterialMetadata, ...]:
        with self._locked():
            self._refresh_index()
            return tuple(
                metadata.model_copy(deep=True)
                for _profile_id, metadata in sorted(
                    self._by_profile.items(),
                    key=lambda item: str(item[0]),
                )
            )
