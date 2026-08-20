"""Deployment configuration validation tests."""

import os
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

import agent_core.config as config_module
from agent_core.config import (
    PACKAGE_ROOT,
    SHIPPED_KNOB_PATHS,
    AuthMode,
    ConfigurationError,
    DeploymentMode,
    MemoryProviderExtractionMode,
    SandboxMechanism,
    WebProviderKind,
    load_config_document,
    load_schedule_worker_settings,
    load_settings,
    validate_runtime_identity,
    validate_settings,
)

PROFILE_ID = "00000000-0000-0000-0000-0000000000e7"
GRANT_ID = "00000000-0000-0000-0000-0000000000e8"


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
    assert settings.release_id is None
    assert settings.web_search_provider is WebProviderKind.DISABLED
    assert settings.web_fetch_provider is WebProviderKind.DISABLED
    assert settings.browser_provider.value == "disabled"
    assert settings.browser_allowed_origins == ()


def test_playwright_browser_provider_requires_explicit_origins() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "BROWSER_PROVIDER": "playwright",
            "BROWSER_ALLOWED_ORIGINS": "https://example.org,https://static.example.org",
        }
    )
    assert settings.browser_provider.value == "playwright"
    assert settings.browser_allowed_origins == (
        "https://example.org",
        "https://static.example.org",
    )

    with pytest.raises(ConfigurationError, match="BROWSER_ALLOWED_ORIGINS"):
        load_settings({**base_environment(), "BROWSER_PROVIDER": "playwright"})


def test_hosted_browser_provider_requires_trusted_profile_and_service_configuration() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "BROWSER_PROVIDER": "hosted",
            "BROWSER_ALLOWED_ORIGINS": "https://example.org",
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_ID": PROFILE_ID,
            "BROWSER_GRANT_ID": GRANT_ID,
            "BROWSER_RUN_PURPOSE": "daily-language-practice",
            "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
        }
    )

    assert settings.browser_provider.value == "hosted"
    assert str(settings.browser_profile_id) == PROFILE_ID
    assert str(settings.browser_grant_id) == GRANT_ID
    assert settings.browser_run_purpose == "daily-language-practice"
    assert settings.browser_profile_service_url == "https://browser.internal.example"
    assert "browser_profile_control_plane" in settings.credentials


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("BROWSER_PROFILE_SERVICE_URL", "BROWSER_PROFILE_SERVICE_URL"),
        ("BROWSER_PROFILE_ID", "BROWSER_PROFILE_ID"),
        ("BROWSER_PROFILE_CONTROL_PLANE_API_KEY", "control-plane credential"),
    ],
)
def test_hosted_browser_provider_refuses_incomplete_trusted_configuration(
    missing: str,
    message: str,
) -> None:
    values = {
        **base_environment(),
        "BROWSER_PROVIDER": "hosted",
        "BROWSER_ALLOWED_ORIGINS": "https://example.org",
        "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
        "BROWSER_PROFILE_ID": PROFILE_ID,
        "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
    }
    values.pop(missing)

    with pytest.raises(ConfigurationError, match=message):
        load_settings(values)


def test_browser_grant_pin_requires_hosted_provider_and_valid_uuid() -> None:
    with pytest.raises(ConfigurationError, match="BROWSER_GRANT_ID requires"):
        load_settings({**base_environment(), "BROWSER_GRANT_ID": GRANT_ID})

    with pytest.raises(ConfigurationError, match="BROWSER_GRANT_ID must be a UUID"):
        load_settings(
            {
                **base_environment(),
                "BROWSER_PROVIDER": "hosted",
                "BROWSER_ALLOWED_ORIGINS": "https://example.org",
                "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
                "BROWSER_PROFILE_ID": PROFILE_ID,
                "BROWSER_GRANT_ID": "not-a-uuid",
                "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
            }
        )


def test_browser_control_plane_credential_loads_from_one_private_file(tmp_path: Path) -> None:
    credential = tmp_path / "browser-control-plane"
    credential.write_text("opaque-control-plane-token-at-least-32-bytes\n", encoding="ascii")
    os.chmod(credential, 0o600)

    settings = load_settings(
        {
            **base_environment(),
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE": str(credential),
        }
    )

    assert (
        settings.credentials["browser_profile_control_plane"].get_secret_value()
        == "opaque-control-plane-token-at-least-32-bytes"
    )


def test_browser_control_plane_refuses_insecure_or_ambiguous_credentials(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "browser-control-plane"
    credential.write_text("opaque-control-plane-token-at-least-32-bytes\n", encoding="ascii")
    os.chmod(credential, 0o644)
    values = {
        **base_environment(),
        "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
        "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE": str(credential),
    }
    with pytest.raises(ConfigurationError, match="credential file"):
        load_settings(values)

    os.chmod(credential, 0o600)
    values["BROWSER_PROFILE_CONTROL_PLANE_API_KEY"] = "second-token"
    with pytest.raises(ConfigurationError, match="exactly one"):
        load_settings(values)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.org",
        "https://example.org/account",
        "https://127.0.0.1",
    ],
)
def test_browser_origins_must_be_exact_public_https_origins(origin: str) -> None:
    with pytest.raises(ConfigurationError, match="invalid origin"):
        load_settings(
            {
                **base_environment(),
                "BROWSER_PROVIDER": "playwright",
                "BROWSER_ALLOWED_ORIGINS": origin,
            }
        )


def test_browser_origins_reject_duplicates_after_normalization() -> None:
    with pytest.raises(ConfigurationError, match="duplicate origins"):
        load_settings(
            {
                **base_environment(),
                "BROWSER_PROVIDER": "playwright",
                "BROWSER_ALLOWED_ORIGINS": "https://example.org,https://EXAMPLE.org",
            }
        )


def test_schedule_roles_default_off() -> None:
    settings = load_settings(base_environment())
    assert settings.schedule_api_enabled is False
    assert settings.schedule_worker_enabled is False


def test_schedule_roles_require_independent_explicit_enablement() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "AGENT_SCHEDULE_API_ENABLED": "1",
            "AGENT_SCHEDULE_WORKER_ENABLED": "1",
        }
    )
    assert settings.schedule_api_enabled is True
    assert settings.schedule_worker_enabled is True


def test_web_provider_selection_is_per_capability() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "WEB_SEARCH_PROVIDER": "tavily",
            "WEB_FETCH_PROVIDER": "firecrawl",
        }
    )
    assert settings.web_search_provider is WebProviderKind.TAVILY
    assert settings.web_fetch_provider is WebProviderKind.FIRECRAWL


def test_unknown_web_provider_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="WEB_SEARCH_PROVIDER"):
        load_settings({**base_environment(), "WEB_SEARCH_PROVIDER": "surprise"})


def test_release_identity_is_validated() -> None:
    settings = load_settings(
        {**base_environment(), "VEETBOT_RELEASE_ID": "20260810-152233-abcdef0"}
    )
    assert settings.release_id == "20260810-152233-abcdef0"

    with pytest.raises(ConfigurationError, match="VEETBOT_RELEASE_ID"):
        load_settings({**base_environment(), "VEETBOT_RELEASE_ID": "main-latest"})


def test_prebuilt_settings_release_identity_is_validated() -> None:
    settings = replace(load_settings(base_environment()), release_id="main-latest")

    with pytest.raises(ConfigurationError, match="VEETBOT_RELEASE_ID"):
        validate_settings(settings)


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


def test_provider_memory_extraction_defaults_to_automatic_selection() -> None:
    settings = load_settings(base_environment())

    assert settings.memory_provider_extraction_mode.value == "auto"


def test_provider_memory_extraction_refuses_required_mode_without_evaluation_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_root = tmp_path / "release-evidence"
    release_root.mkdir()
    monkeypatch.setattr(
        config_module,
        "PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT",
        release_root,
    )
    values = {
        **base_environment(),
        "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE": "required",
    }

    with pytest.raises(ConfigurationError, match="evaluation evidence"):
        load_settings(values)


def test_provider_memory_extraction_refuses_failed_evaluation_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "provider-memory-evidence.json"
    evidence.write_text(
        '{"schema_version":1,"formation_recall_lift":0,"fabricated_candidates":1}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="evaluation evidence did not pass"):
        load_settings(
            {
                **base_environment(),
                "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE": "required",
                "AGENT_MEMORY_PROVIDER_EXTRACTION_EVIDENCE": str(evidence),
            }
        )


def test_provider_memory_extraction_normalizes_non_utf8_evidence_failure(tmp_path: Path) -> None:
    evidence = tmp_path / "provider-memory-evidence.json"
    evidence.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigurationError, match="evaluation evidence did not pass"):
        load_settings(
            {
                **base_environment(),
                "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE": "required",
                "AGENT_MEMORY_PROVIDER_EXTRACTION_EVIDENCE": str(evidence),
            }
        )


def test_legacy_provider_memory_enablement_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_root = tmp_path / "release-evidence"
    release_root.mkdir()
    monkeypatch.setattr(
        config_module,
        "PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT",
        release_root,
    )
    with pytest.raises(ConfigurationError, match="evaluation evidence"):
        load_settings(
            {
                **base_environment(),
                "AGENT_MEMORY_PROVIDER_EXTRACTION_ENABLED": "1",
            }
        )


def test_blank_legacy_provider_memory_enablement_is_unset() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE": "off",
            "AGENT_MEMORY_PROVIDER_EXTRACTION_ENABLED": "   ",
        }
    )

    assert settings.memory_provider_extraction_mode is MemoryProviderExtractionMode.OFF


def test_auto_provider_memory_mode_tolerates_unusable_operator_evidence(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "provider-memory-evidence.json"
    evidence.write_text("not-json", encoding="utf-8")

    settings = load_settings(
        {
            **base_environment(),
            "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE": "auto",
            "AGENT_MEMORY_PROVIDER_EXTRACTION_EVIDENCE": str(evidence),
        }
    )

    assert settings.memory_provider_extraction_mode.value == "auto"


def test_provider_memory_mode_and_legacy_flag_are_mutually_exclusive() -> None:
    with pytest.raises(ConfigurationError, match="mutually exclusive"):
        load_settings(
            {
                **base_environment(),
                "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE": "auto",
                "AGENT_MEMORY_PROVIDER_EXTRACTION_ENABLED": "1",
            }
        )


def test_required_database_url_fails_before_construction() -> None:
    values = base_environment()
    values.pop("DATABASE_URL")
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        load_settings(values)


def test_token_auth_requires_token() -> None:
    values = {**base_environment(), "AUTH_MODE": "token"}
    with pytest.raises(ConfigurationError, match="AUTH_TOKEN"):
        load_settings(values)


def test_schedule_worker_identity_does_not_require_the_api_bearer_token() -> None:
    values = {
        **base_environment(),
        "AUTH_MODE": "token",
        "AUTH_TENANT_ID": "tenant-a",
        "AUTH_PRINCIPAL_ID": "principal-a",
        "AUTH_SCOPES": "schedule.read",
        "SANDBOX_MECHANISM": "gvisor",
    }
    settings = load_schedule_worker_settings(values)
    assert settings.auth_token is None
    assert settings.auth_principal_id == "principal-a"


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
            "worker:\n  lease_seconds: 0.5\n",
            r"worker\.lease_seconds must be at least 1",
        ),
        (
            "runtime/limits.yaml",
            "model:\n  max_internal_attempts: -" + "9" * 400 + "\n",
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


def test_undeclared_interpolation_in_new_provider_profile_is_refused(tmp_path: Path) -> None:
    overlay = tmp_path / "models" / "providers" / "custom.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("model: ${UNDECLARED_PROVIDER_MODEL}\n", encoding="utf-8")
    values = {**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)}
    with pytest.raises(ConfigurationError, match="UNDECLARED_PROVIDER_MODEL"):
        load_settings(values)


def test_credentials_are_profile_keyed_and_repr_safe() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "VEETBOT_OPENAI_KEY": "synthetic-openai-credential",
            "ANTHROPIC_API_KEY": "synthetic-anthropic-credential",
        }
    )
    assert set(settings.credentials) == {"openai", "anthropic"}
    assert "synthetic-openai-credential" not in repr(settings)


def test_veetbot_openai_credential_precedes_compatibility_fallback() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "VEETBOT_OPENAI_KEY": "synthetic-canonical-credential",
            "OPENAI_API_KEY": "synthetic-compatibility-credential",
        }
    )

    assert settings.credentials["openai"].get_secret_value() == ("synthetic-canonical-credential")


def test_openai_compatibility_credential_remains_supported() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "OPENAI_API_KEY": "synthetic-compatibility-credential",
        }
    )

    assert settings.credentials["openai"].get_secret_value() == (
        "synthetic-compatibility-credential"
    )


def test_production_refuses_evaluation_identity() -> None:
    values = {
        **base_environment(),
        "DEPLOYMENT_MODE": "production",
        "AUTH_MODE": "token",
        "AUTH_TOKEN": "local-test-token-value",
        "AUTH_TENANT_ID": "tenant-production",
        "AUTH_PRINCIPAL_ID": "operator",
        "AUTH_SCOPES": "session.read,run.read",
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


def test_token_auth_requires_a_configured_principal() -> None:
    values = {
        **base_environment(),
        "AUTH_MODE": "token",
        "AUTH_TOKEN": "local-test-token-value",
        "SANDBOX_MECHANISM": "microvm",
    }
    with pytest.raises(ConfigurationError, match="configured principal"):
        load_settings(values)


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ("resources:\n  memory_bytes: 0\n", "positive integer"),
        ("egress:\n  mode: unrestricted\n", "deny or allowlist"),
        (
            "egress:\n  mode: allowlist\n  destinations:\n"
            "    - host: 127.0.0.1\n      ports: [80]\n",
            "destinations.0 is invalid",
        ),
        ("artifacts:\n  retention_days: -1\n", "positive integer"),
    ],
)
def test_sandbox_overlay_values_are_semantically_validated(
    tmp_path: Path, overlay: str, message: str
) -> None:
    path = tmp_path / "sandbox" / "limits.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(overlay, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_settings({**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)})


def test_all_121_versioned_knobs_are_present_and_non_null() -> None:
    qualified_paths = {
        f"{relative}:{path}" for relative, paths in SHIPPED_KNOB_PATHS.items() for path in paths
    }
    assert len(qualified_paths) == 121

    for relative, paths in SHIPPED_KNOB_PATHS.items():
        loaded: object = yaml.safe_load((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        for path in paths:
            value: object = loaded
            for component in path.split("."):
                assert isinstance(value, dict), f"{relative}:{path} is not a mapping path"
                value = cast(dict[str, object], value)[component]
            assert value is not None, f"{relative}:{path} is null"
