"""Deployment configuration validation tests."""

from pathlib import Path

import pytest

from agent_core.config import (
    AuthMode,
    ConfigurationError,
    DeploymentMode,
    SandboxMechanism,
    load_settings,
)


def base_environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://" + "agent:agent@localhost:5432/agent",
        "DEPLOYMENT_MODE": "development",
        "AUTH_MODE": "dev",
        "SANDBOX_MECHANISM": "docker",
        "OPENAI_MODEL": "",
    }


def test_loads_frozen_settings() -> None:
    settings = load_settings(base_environment())
    assert settings.deployment_mode is DeploymentMode.DEVELOPMENT
    assert settings.auth_mode is AuthMode.DEV
    assert settings.sandbox is SandboxMechanism.DOCKER
    with pytest.raises(TypeError):
        settings.interpolation["OPENAI_MODEL"] = "changed"  # type: ignore[index]
    with pytest.raises(AttributeError):
        settings.auth_mode = AuthMode.TOKEN  # type: ignore[misc]


def test_required_database_url_fails_before_construction() -> None:
    values = base_environment()
    values.pop("DATABASE_URL")
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        load_settings(values)


def test_token_auth_requires_token() -> None:
    values = {**base_environment(), "AUTH_MODE": "token"}
    with pytest.raises(ConfigurationError, match="AUTH_TOKEN"):
        load_settings(values)


@pytest.mark.parametrize("mechanism", ["docker", "fake"])
def test_production_refuses_development_sandboxes(mechanism: str) -> None:
    values = {
        **base_environment(),
        "DEPLOYMENT_MODE": "production",
        "AUTH_MODE": "token",
        "AUTH_TOKEN": "local-test-token-value",
        "SANDBOX_MECHANISM": mechanism,
    }
    with pytest.raises(ConfigurationError, match="refuses"):
        load_settings(values)


def test_hardline_overlay_is_refused(tmp_path: Path) -> None:
    hardline = tmp_path / "policy" / "hardline.yaml"
    hardline.parent.mkdir(parents=True)
    hardline.write_text("rules: []\n", encoding="utf-8")
    values = {**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)}
    with pytest.raises(ConfigurationError, match="cannot be overlaid"):
        load_settings(values)


def test_unknown_overlay_file_is_refused(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text("value: true\n", encoding="utf-8")
    values = {**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)}
    with pytest.raises(ConfigurationError, match="no shipped counterpart"):
        load_settings(values)


def test_valid_top_level_overlay_is_accepted(tmp_path: Path) -> None:
    overlay = tmp_path / "tools" / "limits.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("maximum_parallel_calls: 4\n", encoding="utf-8")
    settings = load_settings({**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)})
    assert settings.config_dir == tmp_path.resolve()


def test_undeclared_interpolation_is_refused(tmp_path: Path) -> None:
    overlay = tmp_path / "models" / "policies.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("unexpected: ${UNDECLARED_VALUE}\n", encoding="utf-8")
    values = {**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)}
    with pytest.raises(ConfigurationError, match="UNDECLARED_VALUE"):
        load_settings(values)


def test_credentials_are_profile_keyed_and_repr_safe() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "OPENAI_API_KEY": "synthetic-openai-credential",
            "ANTHROPIC_API_KEY": "synthetic-anthropic-credential",
        }
    )
    assert set(settings.credentials) == {"openai", "anthropic"}
    assert "synthetic-openai-credential" not in repr(settings)
