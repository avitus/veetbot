"""Fail-closed configuration for the isolated profile service process."""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from agent_core.browser_control_plane.models import ProfileStoreIntegrityError
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.domain.credentials import SecretValue

_KEY_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAXIMUM_SECRET_FILE_BYTES = 4096


@dataclass(frozen=True, slots=True)
class ProfileServiceSettings:
    authorization: SecretValue
    session_secret: SecretValue
    keyring: StaticProfileKeyring
    material_root: Path
    bind_host: str
    bind_port: int
    ceremony_base_url: str


def load_profile_service_settings(
    environ: Mapping[str, str] | None = None,
) -> ProfileServiceSettings:
    values = os.environ if environ is None else environ
    auth_path = _absolute_path(values, "BROWSER_PROFILE_SERVICE_AUTH_FILE")
    session_secret_path = _absolute_path(values, "BROWSER_PROFILE_SESSION_SECRET_FILE")
    key_dir = _absolute_path(values, "BROWSER_PROFILE_KEY_DIR")
    material_root = _absolute_path(values, "BROWSER_PROFILE_MATERIAL_ROOT")

    authorization = _read_private_text(auth_path, "service authorization")
    if not 32 <= len(authorization) <= 512 or any(
        character.isspace() for character in authorization
    ):
        raise ProfileStoreIntegrityError("service authorization is invalid")
    session_secret = _read_private_text(session_secret_path, "session process secret")
    if not 32 <= len(session_secret) <= 512 or any(
        character.isspace() for character in session_secret
    ):
        raise ProfileStoreIntegrityError("session process secret is invalid")

    keys, current_version = _load_keyring(key_dir)
    bind_host = values.get("BROWSER_PROFILE_BIND_HOST", "0.0.0.0")
    if not bind_host or any(character.isspace() for character in bind_host):
        raise ProfileStoreIntegrityError("profile service bind host is invalid")
    try:
        bind_port = int(values.get("BROWSER_PROFILE_BIND_PORT", "8080"))
    except ValueError as exc:
        raise ProfileStoreIntegrityError("profile service bind port is invalid") from exc
    if not 1 <= bind_port <= 65535:
        raise ProfileStoreIntegrityError("profile service bind port is invalid")
    ceremony_base_url = values.get("BROWSER_PROFILE_CEREMONY_BASE_URL", "")
    parsed_ceremony = urlsplit(ceremony_base_url)
    if (
        parsed_ceremony.scheme != "https"
        or parsed_ceremony.hostname is None
        or parsed_ceremony.username is not None
        or parsed_ceremony.password is not None
        or parsed_ceremony.path not in {"", "/"}
        or parsed_ceremony.query
        or parsed_ceremony.fragment
    ):
        raise ProfileStoreIntegrityError("authentication ceremony origin is invalid")
    return ProfileServiceSettings(
        authorization=SecretValue(authorization),
        session_secret=SecretValue(session_secret),
        keyring=StaticProfileKeyring(keys, current_version=current_version),
        material_root=material_root.resolve(strict=False),
        bind_host=bind_host,
        bind_port=bind_port,
        ceremony_base_url=ceremony_base_url.rstrip("/"),
    )


def _absolute_path(values: Mapping[str, str], name: str) -> Path:
    raw = values.get(name, "")
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise ProfileStoreIntegrityError("profile service mount path is invalid")
    return path


def _assert_owned_private(path: Path, *, directory: bool) -> os.stat_result:
    try:
        if path.is_symlink():
            raise ProfileStoreIntegrityError("profile service mount is invalid")
        metadata = path.stat()
    except OSError as exc:
        raise ProfileStoreIntegrityError("profile service mount is unavailable") from exc
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ProfileStoreIntegrityError("profile service mount is invalid")
    if metadata.st_mode & 0o077:
        raise ProfileStoreIntegrityError("profile service mount permissions are invalid")
    return metadata


def _read_private_text(path: Path, label: str) -> str:
    metadata = _assert_owned_private(path, directory=False)
    if metadata.st_size > _MAXIMUM_SECRET_FILE_BYTES:
        raise ProfileStoreIntegrityError(f"{label} is invalid")
    try:
        raw = path.read_bytes()
        text = raw.decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProfileStoreIntegrityError(f"{label} is invalid") from exc
    value = text.removesuffix("\n").removesuffix("\r")
    if not value or "\n" in value or "\r" in value:
        raise ProfileStoreIntegrityError(f"{label} is invalid")
    return value


def _load_keyring(path: Path) -> tuple[dict[str, bytes], str]:
    _assert_owned_private(path, directory=True)
    try:
        entries = tuple(path.iterdir())
    except OSError as exc:
        raise ProfileStoreIntegrityError("profile keyring is unavailable") from exc
    allowed = {"current"}
    keys: dict[str, bytes] = {}
    decoded_values: set[bytes] = set()
    for entry in entries:
        if entry.name == "current":
            continue
        if not entry.name.endswith(".key"):
            raise ProfileStoreIntegrityError("profile keyring contains an unknown entry")
        version = entry.name.removesuffix(".key")
        if not _KEY_VERSION.fullmatch(version):
            raise ProfileStoreIntegrityError("profile key version is invalid")
        allowed.add(entry.name)
        encoded = _read_private_text(entry, "profile encryption key")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProfileStoreIntegrityError("profile encryption key is invalid") from exc
        if len(decoded) != 32 or decoded in decoded_values:
            raise ProfileStoreIntegrityError("profile encryption key is invalid")
        keys[version] = decoded
        decoded_values.add(decoded)
    if not keys or {entry.name for entry in entries} - allowed:
        raise ProfileStoreIntegrityError("profile keyring is invalid")
    current = _read_private_text(path / "current", "current profile key version")
    if not _KEY_VERSION.fullmatch(current) or current not in keys:
        raise ProfileStoreIntegrityError("current profile key version is invalid")
    return keys, current
