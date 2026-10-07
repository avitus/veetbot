"""Fail-closed mounted-secret configuration contract for the profile service."""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest
import uvicorn

import agent_core.browser_control_plane.main as service_main
from agent_core.browser_control_plane.configuration import load_profile_service_settings
from agent_core.browser_control_plane.log_redaction import profile_service_log_config
from agent_core.browser_control_plane.models import ProfileStoreIntegrityError
from agent_core.domain.browser import require_service_origin

OPAQUE_AUTH_VALUE = "synthetic-profile-service-auth-value"


def private_file(path: Path, content: str) -> None:
    path.write_text(content)
    os.chmod(path, 0o600)


def environment(tmp_path: Path) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    token_file = tmp_path / "service-auth"
    private_file(token_file, OPAQUE_AUTH_VALUE + "\n")
    session_secret_file = tmp_path / "session-secret"
    private_file(session_secret_file, "synthetic-session-process-secret-32-bytes\n")
    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    private_file(key_dir / "current", "key-v1\n")
    private_file(
        key_dir / "key-v1.key",
        base64.b64encode(hashlib.sha256(b"synthetic-mounted-key").digest()).decode() + "\n",
    )
    return {
        "BROWSER_PROFILE_SERVICE_AUTH_FILE": str(token_file),
        "BROWSER_PROFILE_SESSION_SECRET_FILE": str(session_secret_file),
        "BROWSER_PROFILE_KEY_DIR": str(key_dir),
        "BROWSER_PROFILE_MATERIAL_ROOT": str(tmp_path / "materials"),
        "BROWSER_PROFILE_BIND_HOST": "0.0.0.0",  # noqa: S104 - boundary fixture
        "BROWSER_PROFILE_BIND_PORT": "8080",
        "BROWSER_PROFILE_CEREMONY_BASE_URL": "https://login.example.test",
    }


def test_profile_service_loads_only_private_file_mounted_material(tmp_path: Path) -> None:
    settings = load_profile_service_settings(environment(tmp_path))

    assert settings.authorization.reveal() == OPAQUE_AUTH_VALUE
    assert settings.session_secret.reveal() == "synthetic-session-process-secret-32-bytes"
    assert settings.keyring.current_version == "key-v1"
    assert len(settings.keyring.resolve("key-v1")) == 32
    assert settings.material_root == (tmp_path / "materials").resolve()
    assert settings.bind_host == "0.0.0.0"  # noqa: S104 - boundary fixture
    assert settings.bind_port == 8080
    assert settings.ceremony_base_url == "https://login.example.test"


def test_site_verification_refuses_executable_definitions(tmp_path: Path) -> None:
    values = environment(tmp_path)
    definition = tmp_path / "verification.json"
    private_file(definition, '{"sites":[{"script":"return true"}]}')
    values["BROWSER_PROFILE_VERIFICATION_FILE"] = str(definition)
    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, True), ("true", True), ("false", False)],
)
def test_device_sign_in_switch_parses_strictly(
    tmp_path: Path, raw: str | None, expected: bool
) -> None:
    """Unset or ``true`` enables device sign-in; ``false`` turns it off (ADR-0128 D13)."""

    values = environment(tmp_path)
    if raw is not None:
        values["BROWSER_PROFILE_DEVICE_SIGN_IN_ENABLED"] = raw

    assert load_profile_service_settings(values).device_sign_in_enabled is expected


@pytest.mark.parametrize("raw", ["yes", "1", "TRUE", "", " false"])
def test_device_sign_in_switch_refuses_any_other_value(tmp_path: Path, raw: str) -> None:
    values = environment(tmp_path)
    values["BROWSER_PROFILE_DEVICE_SIGN_IN_ENABLED"] = raw

    with pytest.raises(ProfileStoreIntegrityError, match="device sign-in switch is invalid"):
        load_profile_service_settings(values)


@pytest.mark.parametrize(
    "key",
    [
        "BROWSER_PROFILE_SERVICE_AUTH_FILE",
        "BROWSER_PROFILE_SESSION_SECRET_FILE",
        "BROWSER_PROFILE_KEY_DIR",
        "BROWSER_PROFILE_MATERIAL_ROOT",
    ],
)
def test_profile_service_rejects_missing_or_relative_mount_paths(
    tmp_path: Path,
    key: str,
) -> None:
    values = environment(tmp_path)
    values[key] = "relative/path"
    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)


def test_profile_service_rejects_insecure_or_symlinked_secret_files(tmp_path: Path) -> None:
    values = environment(tmp_path)
    auth_file = Path(values["BROWSER_PROFILE_SERVICE_AUTH_FILE"])
    os.chmod(auth_file, 0o644)
    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)


def test_profile_service_fails_closed_without_atomic_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = environment(tmp_path)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)


@pytest.mark.parametrize(
    "origin",
    (
        "https://login.example.test:not-a-port",
        "https://login.example.test:0",
        "https://login.example.test:65536",
    ),
)
def test_service_origins_reject_invalid_ports(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ValueError):
        require_service_origin(origin, message="invalid service origin")

    values = environment(tmp_path)
    values["BROWSER_PROFILE_CEREMONY_BASE_URL"] = origin
    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)

    values = environment(tmp_path / "second")
    auth_file = Path(values["BROWSER_PROFILE_SERVICE_AUTH_FILE"])
    target = auth_file.with_name("target-auth")
    auth_file.rename(target)
    auth_file.symlink_to(target)
    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)


def test_profile_service_rejects_unknown_duplicate_or_invalid_keys(tmp_path: Path) -> None:
    values = environment(tmp_path)
    key_dir = Path(values["BROWSER_PROFILE_KEY_DIR"])
    private_file(key_dir / "unexpected.txt", "value")
    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)

    values = environment(tmp_path / "second")
    key_dir = Path(values["BROWSER_PROFILE_KEY_DIR"])
    private_file(key_dir / "key-v2.key", (key_dir / "key-v1.key").read_text())
    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)


def test_profile_service_does_not_accept_secret_bytes_from_environment(tmp_path: Path) -> None:
    values = environment(tmp_path)
    values["BROWSER_PROFILE_SERVICE_AUTH_TOKEN"] = "different-environment-authorization-value"
    values["BROWSER_PROFILE_ENCRYPTION_KEY"] = "synthetic-environment-key-material"

    settings = load_profile_service_settings(values)

    assert settings.authorization.reveal() == OPAQUE_AUTH_VALUE
    assert settings.keyring.resolve("key-v1") == hashlib.sha256(b"synthetic-mounted-key").digest()


def test_profile_service_entrypoint_uses_only_mounted_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = environment(tmp_path)
    values["BROWSER_PROFILE_DEVICE_SIGN_IN_ENABLED"] = "false"
    settings = load_profile_service_settings(values)
    observed: dict[str, object] = {}
    monkeypatch.setattr(service_main, "load_profile_service_settings", lambda: settings)
    monkeypatch.setattr(
        service_main,
        "create_profile_service_app",
        lambda lifecycle, authorization, *, sessions: (
            observed.update(
                lifecycle=lifecycle,
                authorization=authorization,
                sessions=sessions,
            )
            or "synthetic-app"
        ),
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, *, host, port, access_log, log_config: observed.update(
            app=app,
            host=host,
            port=port,
            access_log=access_log,
            log_config=log_config,
        ),
    )

    service_main.main()

    assert observed["authorization"] is settings.authorization
    assert observed["sessions"] is not None
    assert observed["sessions"]._device_sign_in_enabled is False  # type: ignore[attr-defined]
    assert observed["app"] == "synthetic-app"
    assert observed["host"] == "0.0.0.0"  # noqa: S104 - boundary fixture
    assert observed["port"] == 8080
    assert observed["access_log"] is False
    assert observed["log_config"] == profile_service_log_config()


def _replace_secret(content: str) -> Callable[[dict[str, str]], None]:
    def mutate(values: dict[str, str]) -> None:
        private_file(Path(values["BROWSER_PROFILE_SERVICE_AUTH_FILE"]), content)

    return mutate


def _key_dir(values: dict[str, str]) -> Path:
    return Path(values["BROWSER_PROFILE_KEY_DIR"])


def _empty_keyring(values: dict[str, str]) -> None:
    for name in ("key-v1.key", "current"):
        (_key_dir(values) / name).unlink()


def _set(name: str, value: str) -> Callable[[dict[str, str]], None]:
    def mutate(values: dict[str, str]) -> None:
        values[name] = value

    return mutate


MOUNT_REFUSALS: dict[str, Callable[[dict[str, str]], None]] = {
    "short_authorization": _replace_secret("too-short\n"),
    "authorization_with_space": _replace_secret("a" * 20 + " " + "b" * 20 + "\n"),
    "two_line_authorization": _replace_secret(OPAQUE_AUTH_VALUE + "\n" + OPAQUE_AUTH_VALUE),
    "empty_authorization": _replace_secret(""),
    "non_ascii_authorization": _replace_secret("é" * 40),
    "short_session_secret": lambda values: private_file(
        Path(values["BROWSER_PROFILE_SESSION_SECRET_FILE"]), "short\n"
    ),
    "missing_secret_file": _set("BROWSER_PROFILE_SERVICE_AUTH_FILE", "/nonexistent/service-auth"),
    "shared_key_directory": lambda values: os.chmod(_key_dir(values), 0o755),
    "key_readable_by_group": lambda values: os.chmod(_key_dir(values) / "key-v1.key", 0o640),
    "current_names_absent_key": lambda values: private_file(
        _key_dir(values) / "current", "key-v9\n"
    ),
    "current_invalid_version": lambda values: private_file(
        _key_dir(values) / "current", "../key-v1\n"
    ),
    "missing_current": lambda values: (_key_dir(values) / "current").unlink(),
    "short_key": lambda values: private_file(
        _key_dir(values) / "key-v1.key", base64.b64encode(b"k" * 16).decode()
    ),
    "invalid_base64_key": lambda values: private_file(
        _key_dir(values) / "key-v1.key", "not base64!"
    ),
    "empty_keyring": _empty_keyring,
    "invalid_key_version_name": lambda values: private_file(
        _key_dir(values) / "key v2.key", base64.b64encode(b"k" * 32).decode()
    ),
    "empty_bind_host": _set("BROWSER_PROFILE_BIND_HOST", ""),
    "bind_host_with_space": _set("BROWSER_PROFILE_BIND_HOST", "0.0.0.0 evil"),
    "non_numeric_port": _set("BROWSER_PROFILE_BIND_PORT", "http"),
    "port_zero": _set("BROWSER_PROFILE_BIND_PORT", "0"),
    "port_too_large": _set("BROWSER_PROFILE_BIND_PORT", "65536"),
    "http_ceremony_origin": _set("BROWSER_PROFILE_CEREMONY_BASE_URL", "http://login.example.test"),
    "missing_ceremony_origin": _set("BROWSER_PROFILE_CEREMONY_BASE_URL", ""),
    "ceremony_origin_with_path": _set(
        "BROWSER_PROFILE_CEREMONY_BASE_URL", "https://login.example.test/app"
    ),
    "ceremony_origin_with_query": _set(
        "BROWSER_PROFILE_CEREMONY_BASE_URL", "https://login.example.test/?next=/"
    ),
    "ceremony_origin_with_userinfo": _set(
        "BROWSER_PROFILE_CEREMONY_BASE_URL", "https://operator@login.example.test"
    ),
    "ceremony_origin_with_fragment": _set(
        "BROWSER_PROFILE_CEREMONY_BASE_URL", "https://login.example.test/#x"
    ),
}


@pytest.mark.parametrize("mutation", list(MOUNT_REFUSALS.values()), ids=list(MOUNT_REFUSALS))
def test_profile_service_refuses_invalid_mounted_values(
    tmp_path: Path, mutation: Callable[[dict[str, str]], None]
) -> None:
    values = environment(tmp_path)
    mutation(values)

    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)


def test_profile_service_keyring_rotates_to_the_named_current_key(tmp_path: Path) -> None:
    values = environment(tmp_path)
    key_dir = _key_dir(values)
    second = hashlib.sha256(b"synthetic-second-key").digest()
    private_file(key_dir / "key-v2.key", base64.b64encode(second).decode() + "\n")
    private_file(key_dir / "current", "key-v2\n")

    settings = load_profile_service_settings(values)

    assert settings.keyring.current_version == "key-v2"
    assert settings.keyring.resolve("key-v2") == second
    assert settings.keyring.resolve("key-v1") == hashlib.sha256(b"synthetic-mounted-key").digest()
