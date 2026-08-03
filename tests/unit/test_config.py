"""Deployment configuration validation tests."""

from pathlib import Path
from typing import cast

import pytest
import yaml

from agent_core.config import (
    PACKAGE_ROOT,
    SHIPPED_KNOB_PATHS,
    AuthMode,
    ConfigurationError,
    DeploymentMode,
    SandboxMechanism,
    load_config_document,
    load_settings,
    validate_runtime_identity,
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
    assert settings.trajectory_export_enabled is False
    assert settings.artifact_root == Path(".agent/artifacts")


def test_trajectory_export_requires_explicit_operator_enablement() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "AGENT_TRAJECTORY_EXPORT_ENABLED": "1",
            "AGENT_ARTIFACT_ROOT": "/var/lib/agent/trajectory-artifacts",
        }
    )
    assert settings.trajectory_export_enabled is True
    assert settings.artifact_root == Path("/var/lib/agent/trajectory-artifacts")


def test_trajectory_export_enablement_is_strictly_boolean() -> None:
    values = {**base_environment(), "AGENT_TRAJECTORY_EXPORT_ENABLED": "true"}
    with pytest.raises(ConfigurationError, match="must be 0 or 1"):
        load_settings(values)


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
    overlay.write_text("parallel:\n  maximum_calls: 4\n", encoding="utf-8")
    settings = load_settings({**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)})
    assert settings.config_dir == tmp_path.resolve()
    merged = load_config_document(settings, "tools/limits.yaml")
    assert merged["parallel"]["maximum_calls"] == 4
    assert merged["output"]["global_maximum_bytes"] == 4_194_304


@pytest.mark.parametrize(
    ("relative", "document", "message"),
    [
        (
            "runtime/limits.yaml",
            "model:\n  max_internal_attempts: 0\n",
            r"model\.max_internal_attempts must be at least 1",
        ),
        (
            "runtime/limits.yaml",
            "run_defaults: disabled\n",
            r"run_defaults must be a mapping",
        ),
        (
            "tools/limits.yaml",
            "circuit_breaker:\n  identical_call_threshold: 1\n",
            r"identical_call_threshold must be at least 2",
        ),
        (
            "context/plan.yaml",
            "classes:\n  tool_definitions:\n    max_items: many\n",
            r"tool_definitions\.max_items must be an integer",
        ),
    ],
)
def test_operator_overlay_values_are_validated(
    tmp_path: Path,
    relative: str,
    document: str,
    message: str,
) -> None:
    overlay = tmp_path / relative
    overlay.parent.mkdir(parents=True)
    overlay.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_settings({**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)})


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


def test_production_refuses_evaluation_identity() -> None:
    values = {
        **base_environment(),
        "DEPLOYMENT_MODE": "production",
        "AUTH_MODE": "token",
        "AUTH_TOKEN": "local-test-token-value",
        "SANDBOX_MECHANISM": "microvm",
    }
    settings = load_settings(values)
    with pytest.raises(ConfigurationError, match="evaluation identity"):
        validate_runtime_identity(
            settings,
            tenant_id="tenant_eval",
            principal_id="user",
            policy_profile="default",
        )

    development = load_settings(base_environment())
    validate_runtime_identity(
        development,
        tenant_id="tenant_eval",
        principal_id="eval.user",
        policy_profile="eval.default",
    )


def test_all_106_versioned_knobs_are_present_and_non_null() -> None:
    qualified_paths = {
        f"{relative}:{path}" for relative, paths in SHIPPED_KNOB_PATHS.items() for path in paths
    }
    assert len(qualified_paths) == 106

    for relative, paths in SHIPPED_KNOB_PATHS.items():
        loaded: object = yaml.safe_load((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        for path in paths:
            value: object = loaded
            for component in path.split("."):
                assert isinstance(value, dict), f"{relative}:{path} is not a mapping path"
                value = cast(dict[str, object], value)[component]
            assert value is not None, f"{relative}:{path} is null"
