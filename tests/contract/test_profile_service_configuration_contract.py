"""Fail-closed mounted-secret configuration contract for the profile service."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest
import uvicorn

import agent_core.browser_control_plane.main as service_main
from agent_core.browser_control_plane.configuration import load_profile_service_settings
from agent_core.browser_control_plane.models import ProfileStoreIntegrityError

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
    settings = load_profile_service_settings(environment(tmp_path))
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
        lambda app, *, host, port, access_log: observed.update(
            app=app,
            host=host,
            port=port,
            access_log=access_log,
        ),
    )

    service_main.main()

    assert observed["authorization"] is settings.authorization
    assert observed["sessions"] is not None
    assert observed["app"] == "synthetic-app"
    assert observed["host"] == "0.0.0.0"  # noqa: S104 - boundary fixture
    assert observed["port"] == 8080
    assert observed["access_log"] is False
