"""Deployment configuration validation tests."""

import os
from collections.abc import Mapping
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
    PushProviderKind,
    SandboxMechanism,
    WebProviderKind,
    load_config_document,
    load_notification_worker_settings,
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

    with pytest.raises(
        ConfigurationError,
        match="BROWSER_ALLOWED_ORIGINS is required when BROWSER_PROVIDER=playwright",
    ):
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


def test_hosted_browser_provider_supports_principal_selected_session_profiles() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "BROWSER_PROVIDER": "hosted",
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
        }
    )

    assert settings.browser_provider.value == "hosted"
    assert settings.browser_profile_id is None
    assert settings.browser_allowed_origins == ()


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


def test_browser_grant_pin_requires_a_hosted_profile_id() -> None:
    with pytest.raises(
        ConfigurationError,
        match="BROWSER_GRANT_ID requires BROWSER_PROFILE_ID",
    ):
        load_settings(
            {
                **base_environment(),
                "BROWSER_PROVIDER": "hosted",
                "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
                "BROWSER_GRANT_ID": GRANT_ID,
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


def test_notification_roles_and_provider_default_off() -> None:
    settings = load_settings(base_environment())
    assert settings.notification_api_enabled is False
    assert settings.notification_dispatch_enabled is False
    assert settings.push_provider is PushProviderKind.DISABLED
    assert settings.apns_key_file is None


@pytest.mark.parametrize(
    ("enabled", "disabled"),
    [
        ("AGENT_NOTIFICATION_API_ENABLED", "AGENT_NOTIFICATION_DISPATCH_ENABLED"),
        ("AGENT_NOTIFICATION_DISPATCH_ENABLED", "AGENT_NOTIFICATION_API_ENABLED"),
    ],
)
def test_notification_roles_must_change_together(enabled: str, disabled: str) -> None:
    with pytest.raises(ConfigurationError, match="notification API and dispatch"):
        load_settings(
            {
                **base_environment(),
                enabled: "1",
                disabled: "0",
            }
        )


def test_notification_worker_settings_load_without_api_bearer(tmp_path: Path) -> None:
    key_file = tmp_path / "AuthKey_TEST.p8"
    key_file.write_text("test APNs private key material", encoding="ascii")
    key_file.chmod(0o600)
    values = {
        **base_environment(),
        "DEPLOYMENT_MODE": "production",
        "AUTH_MODE": "token",
        "AUTH_TENANT_ID": "tenant-a",
        "AUTH_PRINCIPAL_ID": "notify-a",
        "AUTH_SCOPES": "notification.read",
        "AGENT_NOTIFICATION_API_ENABLED": "1",
        "AGENT_NOTIFICATION_DISPATCH_ENABLED": "1",
        "PUSH_PROVIDER": "apns",
        "APNS_KEY_FILE": str(key_file),
        "APNS_KEY_ID": "KEY123",
        "APNS_TEAM_ID": "TEAM123",
        "APNS_TOPIC": "com.veetbot.app",
    }

    settings = load_notification_worker_settings(values)

    assert settings.auth_token is None
    assert settings.notification_api_enabled is True
    assert settings.notification_dispatch_enabled is True
    assert settings.push_provider is PushProviderKind.APNS
    assert settings.apns_key_file == key_file
    assert settings.apns_key_id == "KEY123"


@pytest.mark.parametrize(
    "missing",
    ["APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_TOPIC"],
)
def test_apns_provider_requires_complete_private_configuration(
    tmp_path: Path,
    missing: str,
) -> None:
    key_file = tmp_path / "AuthKey_TEST.p8"
    key_file.write_text("test APNs private key material", encoding="ascii")
    key_file.chmod(0o600)
    values = {
        **base_environment(),
        "AGENT_NOTIFICATION_API_ENABLED": "1",
        "AGENT_NOTIFICATION_DISPATCH_ENABLED": "1",
        "PUSH_PROVIDER": "apns",
        "APNS_KEY_FILE": str(key_file),
        "APNS_KEY_ID": "KEY123",
        "APNS_TEAM_ID": "TEAM123",
        "APNS_TOPIC": "com.veetbot.app",
    }
    values.pop(missing)

    with pytest.raises(ConfigurationError, match=missing):
        load_settings(values)


def test_disabled_push_provider_rejects_apns_configuration(tmp_path: Path) -> None:
    key_file = tmp_path / "AuthKey_TEST.p8"
    key_file.write_text("test APNs private key material", encoding="ascii")
    key_file.chmod(0o600)

    with pytest.raises(ConfigurationError, match=r"APNS_.*PUSH_PROVIDER=apns"):
        load_settings({**base_environment(), "APNS_KEY_FILE": str(key_file)})


@pytest.mark.parametrize("unsafe", ["relative", "symlink", "permissive"])
def test_apns_key_file_must_be_absolute_regular_and_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    key_file = tmp_path / "AuthKey_TEST.p8"
    key_file.write_text("test APNs private key material", encoding="ascii")
    key_file.chmod(0o600)
    configured = key_file
    if unsafe == "relative":
        monkeypatch.chdir(tmp_path)
        configured = Path(key_file.name)
    elif unsafe == "symlink":
        configured = tmp_path / "AuthKey_LINK.p8"
        configured.symlink_to(key_file)
    else:
        key_file.chmod(0o640)
    values = {
        **base_environment(),
        "AGENT_NOTIFICATION_API_ENABLED": "1",
        "AGENT_NOTIFICATION_DISPATCH_ENABLED": "1",
        "PUSH_PROVIDER": "apns",
        "APNS_KEY_FILE": str(configured),
        "APNS_KEY_ID": "KEY123",
        "APNS_TEAM_ID": "TEAM123",
        "APNS_TOPIC": "com.veetbot.app",
    }

    with pytest.raises(ConfigurationError, match="APNS_KEY_FILE"):
        load_settings(values)


def test_unknown_push_provider_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="PUSH_PROVIDER"):
        load_settings({**base_environment(), "PUSH_PROVIDER": "surprise"})


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


def test_weighted_web_provider_selection_is_per_capability() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "WEB_SEARCH_PROVIDERS": "tavily:50,keenable:50",
            "WEB_FETCH_PROVIDERS": "firecrawl:50,keenable:50",
        }
    )

    assert [(entry.provider, entry.weight) for entry in settings.web_search_providers] == [
        (WebProviderKind.TAVILY, 50),
        (WebProviderKind.KEENABLE, 50),
    ]
    assert [(entry.provider, entry.weight) for entry in settings.web_fetch_providers] == [
        (WebProviderKind.FIRECRAWL, 50),
        (WebProviderKind.KEENABLE, 50),
    ]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("WEB_SEARCH_PROVIDERS", "tavily:60,keenable:30"),
        ("WEB_SEARCH_PROVIDERS", "tavily:50,tavily:50"),
        ("WEB_SEARCH_PROVIDERS", "disabled:50,keenable:50"),
        ("WEB_SEARCH_PROVIDERS", "tavily:zero,keenable:100"),
        ("WEB_FETCH_PROVIDERS", "firecrawl:0,keenable:100"),
        ("WEB_FETCH_PROVIDERS", "surprise:50,keenable:50"),
    ],
)
def test_invalid_weighted_web_provider_selection_is_refused(
    variable: str,
    value: str,
) -> None:
    with pytest.raises(ConfigurationError, match=variable):
        load_settings({**base_environment(), variable: value})


def test_plural_and_enabled_legacy_web_provider_selection_are_ambiguous() -> None:
    with pytest.raises(ConfigurationError, match="WEB_SEARCH_PROVIDERS"):
        load_settings(
            {
                **base_environment(),
                "WEB_SEARCH_PROVIDER": "tavily",
                "WEB_SEARCH_PROVIDERS": "tavily:50,keenable:50",
            }
        )


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
        "DEPLOYMENT_MODE": "production",
        "AUTH_MODE": "token",
        "AUTH_TENANT_ID": "tenant-a",
        "AUTH_PRINCIPAL_ID": "principal-a",
        "AUTH_SCOPES": "schedule.read",
    }
    settings = load_schedule_worker_settings(values)
    assert settings.auth_token is None
    assert settings.auth_principal_id == "principal-a"
    assert settings.execution_service_socket is None


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


def test_production_requires_the_credential_free_execution_service() -> None:
    values = {
        **base_environment(),
        "DEPLOYMENT_MODE": "production",
        "AUTH_MODE": "token",
        "AUTH_TOKEN": "local-test-token-value",
        "AUTH_TENANT_ID": "tenant-production",
        "AUTH_PRINCIPAL_ID": "operator",
        "AUTH_SCOPES": "session.read,run.read",
        "SANDBOX_MECHANISM": "gvisor",
    }

    with pytest.raises(ConfigurationError, match="AGENT_EXECUTION_SERVICE_SOCKET"):
        load_settings(values)
    with pytest.raises(ConfigurationError, match="must be an absolute path"):
        load_settings({**values, "AGENT_EXECUTION_SERVICE_SOCKET": "execution.sock"})

    settings = load_settings(
        {**values, "AGENT_EXECUTION_SERVICE_SOCKET": "/run/veetbot/execution.sock"}
    )
    assert settings.execution_service_socket == Path("/run/veetbot/execution.sock")


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
            "runtime/limits.yaml",
            "notifications:\n  retry_delays_seconds: []\n",
            r"notifications\.retry_delays_seconds must contain positive numbers",
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


def test_policy_semantic_interpolation_is_refused_even_when_declared(tmp_path: Path) -> None:
    overlay = tmp_path / "policy" / "default.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("name: ${OPENAI_MODEL}\n", encoding="utf-8")
    values = {
        **base_environment(),
        "AGENT_CONFIG_DIR": str(tmp_path),
        "OPENAI_MODEL": "deployment-specific-policy-name",
    }

    with pytest.raises(ConfigurationError, match="policy-semantic interpolation"):
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
        "AGENT_EXECUTION_SERVICE_SOCKET": "/run/veetbot/execution.sock",
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
    """Sandbox overlays reject values that violate execution invariants."""

    path = tmp_path / "sandbox" / "limits.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(overlay, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_settings({**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)})


def test_all_161_versioned_knobs_are_present_and_non_null() -> None:
    """Keep the declared configuration inventory exact and fully populated."""

    qualified_paths = {
        f"{relative}:{path}" for relative, paths in SHIPPED_KNOB_PATHS.items() for path in paths
    }
    assert len(qualified_paths) == 161
    assert {
        "runtime/limits.yaml:delegation.max_children_per_call",
        "runtime/limits.yaml:delegation.max_live_children_per_parent",
        "runtime/limits.yaml:delegation.max_depth",
        "runtime/limits.yaml:delegation.max_live_delegated_runs_per_tenant",
        "runtime/limits.yaml:delegation.child_max_steps",
        "runtime/limits.yaml:delegation.child_max_model_calls",
        "runtime/limits.yaml:delegation.child_max_tool_calls",
        "runtime/limits.yaml:delegation.child_max_cost",
        "runtime/limits.yaml:delegation.child_wall_seconds",
        "runtime/limits.yaml:delegation.synthesis_reserve_steps",
        "runtime/limits.yaml:delegation.synthesis_reserve_model_calls",
        "runtime/limits.yaml:delegation.synthesis_reserve_cost",
        "runtime/limits.yaml:delegation.summary_max_bytes",
    } <= qualified_paths

    for relative, paths in SHIPPED_KNOB_PATHS.items():
        loaded: object = yaml.safe_load((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        for path in paths:
            value: object = loaded
            for component in path.split("."):
                assert isinstance(value, dict), f"{relative}:{path} is not a mapping path"
                value = cast(dict[str, object], value)[component]
            assert value is not None, f"{relative}:{path} is null"


def test_delegated_research_defaults_leave_room_for_tool_use_and_synthesis() -> None:
    """Pin the governed child research limits and final-synthesis reserves."""

    loaded = yaml.safe_load((PACKAGE_ROOT / "runtime/limits.yaml").read_text(encoding="utf-8"))

    assert loaded["delegation"] == {
        "max_children_per_call": 3,
        "max_live_children_per_parent": 8,
        "max_depth": 1,
        "max_live_delegated_runs_per_tenant": 16,
        "child_max_steps": 12,
        "child_max_model_calls": 12,
        "child_max_tool_calls": 48,
        "child_max_cost": 2,
        "child_wall_seconds": 900,
        "synthesis_reserve_steps": 1,
        "synthesis_reserve_model_calls": 1,
        "synthesis_reserve_cost": 0.25,
        "summary_max_bytes": 16384,
    }


def _leaf_paths(document: Mapping[str, object], prefix: str = "") -> set[str]:
    """Return dotted paths for every non-mapping value in a document."""

    leaves: set[str] = set()
    for key, value in document.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            leaves |= _leaf_paths(cast(Mapping[str, object], value), path)
        else:
            leaves.add(path)
    return leaves


def test_memory_profiles_knob_paths_match_document() -> None:
    """Keep the memory profile registry synchronized with its YAML leaves."""

    loaded: object = yaml.safe_load(
        (PACKAGE_ROOT / "memory/profiles.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    document = cast(Mapping[str, object], loaded)

    declared = set(SHIPPED_KNOB_PATHS["memory/profiles.yaml"])

    assert declared == _leaf_paths(document) - {"schema_version"}
    assert len(declared) == 37
