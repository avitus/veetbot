"""The lean worker roles refuse to start with more authority or less topology than designed.

notifications-and-devices.md, scheduling.md and inbound-surfaces.md each give a
role its own environment: production PostgreSQL, a configured token identity,
and only that role's own secret. The API bearer and provider keys never reach
it, and a missing enablement flag stops it before any resource is built.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_core.bootstrap import (
    _validate_notification_role,
    _validate_schedule_role,
    _validate_surface_role,
)
from agent_core.config import (
    AuthMode,
    ConfigurationError,
    DeploymentMode,
    PushProviderKind,
    SandboxMechanism,
    Settings,
)
from agent_core.domain.agents import Principal

Validator = Callable[[Settings], Principal]


def _production(**flags: object) -> Settings:
    base = Settings(
        database_url="postgresql+asyncpg://role@127.0.0.1:5432/agent",
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        auth_token=None,
        sandbox=SandboxMechanism.GVISOR,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
        auth_tenant_id="tenant-a",
        auth_principal_id="operator",
        auth_roles=frozenset({"user"}),
        auth_scopes=frozenset({"run.write", "surface.read", "surface.write", "schedule.read"}),
    )
    return replace(base, **flags)  # type: ignore[arg-type]


def _notify(tmp_path: Path) -> Settings:
    key = tmp_path / "AuthKey.p8"
    key.write_text("fixture", encoding="ascii")
    key.chmod(0o600)
    return _production(
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
        push_provider=PushProviderKind.APNS,
        apns_key_file=key,
        apns_key_id="KEY123",
        apns_team_id="TEAM123",
        apns_topic="com.veetbot.app",
    )


def _schedule(_tmp_path: Path) -> Settings:
    return _production(schedule_api_enabled=True, schedule_worker_enabled=True)


def _surface(_tmp_path: Path) -> Settings:
    return _production(
        surface_api_enabled=True,
        surface_worker_enabled=True,
        surface_telegram_token=SecretStr("fixture"),
    )


ROLES: dict[str, tuple[Callable[[Path], Settings], Validator]] = {
    "notify": (_notify, _validate_notification_role),
    "schedule": (_schedule, _validate_schedule_role),
    "surface": (_surface, _validate_surface_role),
}

TOPOLOGY: list[tuple[dict[str, object], str]] = [
    ({"deployment_mode": DeploymentMode.DEVELOPMENT}, "production process topology"),
    # The shared validation refuses a development identity in production first.
    ({"auth_mode": AuthMode.DEV}, "non-development identity|refuses AUTH_MODE=dev"),
    ({"database_url": "sqlite:///agent.db"}, "PostgreSQL storage"),
    ({"credentials": {"openai": SecretStr("fixture")}}, "must not contain provider keys"),
    ({"auth_scopes": frozenset({"run.write", "not.a.scope"})}, "unknown platform scopes"),
]


@pytest.mark.parametrize("role", sorted(ROLES))
def test_a_complete_lean_role_environment_is_accepted(role: str, tmp_path: Path) -> None:
    settings, validate = ROLES[role]
    principal = validate(settings(tmp_path))
    assert (principal.tenant_id, principal.principal_id) == ("tenant-a", "operator")


@pytest.mark.parametrize(("override", "message"), TOPOLOGY)
@pytest.mark.parametrize("role", sorted(ROLES))
def test_every_lean_role_refuses_the_wrong_topology_or_foreign_credentials(
    role: str, override: dict[str, object], message: str, tmp_path: Path
) -> None:
    settings, validate = ROLES[role]
    with pytest.raises(ConfigurationError, match=message):
        validate(replace(settings(tmp_path), **override))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("role", "override", "message"),
    [
        ("notify", {"auth_token": SecretStr("fixture")}, "must not contain an API bearer"),
        ("surface", {"auth_token": SecretStr("fixture")}, "must not contain an API bearer"),
        (
            "notify",
            {
                "notification_api_enabled": False,
                "notification_dispatch_enabled": False,
                "push_provider": PushProviderKind.DISABLED,
                "apns_key_file": None,
                "apns_key_id": None,
                "apns_team_id": None,
                "apns_topic": None,
            },
            "notification dispatch is disabled",
        ),
        (
            "schedule",
            {"schedule_api_enabled": False, "schedule_worker_enabled": False},
            "schedule worker is disabled",
        ),
        ("schedule", {"schedule_api_enabled": False}, "schedule API is disabled"),
        (
            "surface",
            {"surface_api_enabled": False, "surface_worker_enabled": False},
            "must both be enabled",
        ),
        (
            "surface",
            {"auth_scopes": frozenset({"run.write", "surface.read"})},
            "requires run.write, surface.read, and surface.write",
        ),
    ],
)
def test_each_lean_role_refuses_its_own_missing_enablement_or_authority(
    role: str, override: dict[str, object], message: str, tmp_path: Path
) -> None:
    settings, validate = ROLES[role]
    with pytest.raises(ConfigurationError, match=message):
        validate(replace(settings(tmp_path), **override))  # type: ignore[arg-type]


def test_the_notify_role_requires_the_apns_provider(tmp_path: Path) -> None:
    unconfigured = replace(
        _notify(tmp_path),
        push_provider=PushProviderKind.DISABLED,
        apns_key_file=None,
        apns_key_id=None,
        apns_team_id=None,
        apns_topic=None,
    )
    with pytest.raises(ConfigurationError, match="requires PUSH_PROVIDER=apns"):
        _validate_notification_role(unconfigured)


def test_the_notify_role_refuses_a_relative_apns_key_file(tmp_path: Path) -> None:
    relative = replace(_notify(tmp_path), apns_key_file=Path("relative/AuthKey.p8"))
    with pytest.raises(ConfigurationError, match="APNS_KEY_FILE must be an absolute"):
        _validate_notification_role(relative)
