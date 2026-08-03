"""Deployment settings and validation for the composition boundary."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import SecretStr


class ConfigurationError(ValueError):
    """Raised when deployment configuration is incomplete or unsafe."""


class DeploymentMode(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    DEV = "dev"
    TOKEN = "token"


class SandboxMechanism(StrEnum):
    MICROVM = "microvm"
    GVISOR = "gvisor"
    DOCKER = "docker"
    FAKE = "fake"


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment-layer settings; tuning values remain in versioned YAML."""

    database_url: str
    deployment_mode: DeploymentMode
    auth_mode: AuthMode
    auth_token: SecretStr | None
    sandbox: SandboxMechanism
    config_dir: Path | None
    credentials: Mapping[str, SecretStr]
    interpolation: Mapping[str, str]


PACKAGE_ROOT = Path(__file__).resolve().parent
SHIPPED_CONFIGS = (
    "policy/hardline.yaml",
    "policy/default.yaml",
    "models/policies.yaml",
    "models/catalog.yaml",
    "context/plan.yaml",
    "tools/limits.yaml",
    "runtime/limits.yaml",
    "memory/profiles.yaml",
)
# The design corpus declares 106 operator-reviewable knobs. Metadata such as
# schema versions, rule identifiers, catalog records, and frozen hardline
# predicates are intentionally not counted as knobs.
SHIPPED_KNOB_PATHS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "policy/default.yaml": (
            "rules.none.decision",
            "rules.workspace_read.decision",
            "rules.workspace_write.decision",
            "rules.network_read.decision",
            "rules.code_execution.decision",
            "rules.package_install.decision",
            "rules.sandbox_network.decision",
            "rules.external_message.decision",
            "rules.external_write.decision",
            "rules.external_delete.decision",
            "rules.financial.decision",
            "rules.publication.decision",
            "rules.credential_access.decision",
            "rules.host_access.decision",
            "rules.privileged.decision",
            "unknown_tool.decision",
            "trust_overlay.external_untrusted_requires_approval",
            "approval_expiry_seconds.low",
            "approval_expiry_seconds.medium",
            "approval_expiry_seconds.high",
            "approval_expiry_seconds.critical",
            "self_approval.enabled",
            "advisory.enabled",
        ),
        "models/policies.yaml": (
            "request_defaults.timeout_seconds",
            "request_defaults.stream_idle_seconds",
            "request_defaults.max_internal_attempts",
            "cache.expected_hit_ratio_floor",
        ),
        "context/plan.yaml": (
            "prefix.ceiling_tokens",
            "classes.platform_policy.max_tokens",
            "classes.agent_instructions.max_tokens",
            "classes.tool_definitions.max_items",
            "classes.tool_definitions.max_tokens",
            "classes.skill_catalog.max_items",
            "classes.skill_catalog.max_tokens",
            "classes.memory_snapshot.max_items",
            "classes.memory_snapshot.max_tokens",
            "classes.memory_snapshot.max_window_ratio",
            "classes.skill_bodies.max_items",
            "classes.skill_bodies.max_tokens",
            "classes.working_state.max_tokens",
            "classes.knowledge_passages.max_items",
            "classes.knowledge_passages.max_tokens",
            "classes.in_turn_recall.max_tokens",
            "classes.tool_results.max_body_ratio",
            "classes.history.floor_tokens",
            "output.reserve_tokens",
            "estimator.safety_margin_ratio",
            "summary.max_depth",
            "working_state.max_constraints",
            "working_state.max_open_tasks",
            "working_state.max_established_facts",
            "working_state.max_open_questions",
            "working_state.block_ceiling_tokens",
        ),
        "tools/limits.yaml": (
            "output.global_maximum_bytes",
            "output.hard_ceiling_multiplier",
            "output.excerpt_head_ratio",
            "output.excerpt_tail_ratio",
            "circuit_breaker.identical_denied_threshold",
            "circuit_breaker.identical_call_threshold",
            "circuit_breaker.uncertain_threshold",
            "parallel.maximum_calls",
            "mcp.connect_timeout_seconds",
            "mcp.request_timeout_seconds",
            "mcp.idle_timeout_seconds",
            "mcp.description_maximum_bytes",
            "mcp.schema_maximum_depth",
            "mcp.schema_maximum_bytes",
            "mcp.default_side_effect",
            "mcp.default_risk",
            "mcp.default_idempotency",
            "mcp.allow_parallel",
            "bridge.approval_hold_seconds",
            "bridge.maximum_underlying_calls_per_turn",
        ),
        "runtime/limits.yaml": (
            "queue.poll_interval_seconds",
            "queue.max_attempts",
            "queue.priorities.interactive",
            "queue.priorities.async",
            "queue.priorities.maintenance",
            "worker.lease_seconds",
            "worker.heartbeat_divisor",
            "sweeps.reclaim_expired_seconds",
            "sweeps.approval_reaper_seconds",
            "sweeps.checkpoint_prune_seconds",
            "sweeps.projection_catch_up_seconds",
            "model.max_internal_attempts",
            "context.max_compactions_per_step",
            "run_defaults.max_steps",
            "run_defaults.max_model_calls",
            "run_defaults.max_tool_calls",
        ),
        "memory/profiles.yaml": (
            "formation.session_boundary_enabled",
            "formation.scheduled_enabled",
            "formation.scheduled_interval_seconds",
            "retrieval.semantic_enabled",
            "retrieval.reciprocal_rank_fusion_k",
            "retrieval.durable_item_share",
            "retrieval.lifecycle_weights.active",
            "retrieval.lifecycle_weights.provisional",
            "snapshots.interactive.max_items",
            "snapshots.interactive.max_tokens",
            "snapshots.interactive.max_window_ratio",
            "snapshots.async.max_items",
            "snapshots.async.max_tokens",
            "snapshots.async.max_window_ratio",
            "snapshots.child.max_items",
            "snapshots.child.max_tokens",
            "snapshots.child.max_window_ratio",
        ),
    }
)
FROZEN_CONFIG = "policy/hardline.yaml"
INTERPOLATION = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
MINIMUM_CONFIG_VALUES: Mapping[str, int] = MappingProxyType(
    {
        "runtime/limits.yaml:model.max_internal_attempts": 1,
        "runtime/limits.yaml:queue.max_attempts": 1,
        "runtime/limits.yaml:run_defaults.max_steps": 1,
        "runtime/limits.yaml:run_defaults.max_model_calls": 1,
        "runtime/limits.yaml:run_defaults.max_tool_calls": 1,
        "runtime/limits.yaml:worker.heartbeat_divisor": 2,
        "runtime/limits.yaml:worker.lease_seconds": 1,
        "tools/limits.yaml:circuit_breaker.identical_call_threshold": 2,
        "context/plan.yaml:classes.tool_definitions.max_items": 1,
    }
)


def _environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    if environ is not None:
        return dict(environ)
    from_file = {key: value or "" for key, value in dotenv_values(".env").items()}
    from_file.update(os.environ)
    return from_file


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"required environment variable {name} is missing")
    return value


def _parse_enum[T: StrEnum](enum_type: type[T], value: str, name: str) -> T:
    try:
        return enum_type(value)
    except ValueError as exc:
        choices = ", ".join(member.value for member in enum_type)
        raise ConfigurationError(f"{name} must be one of: {choices}") from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load configuration file {path}") from exc
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"configuration file {path} must contain a mapping")
    return {str(key): value for key, value in loaded.items()}


def _merge_mappings(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively overlay mappings while retaining untouched shipped siblings."""

    merged = {str(key): value for key, value in base.items()}
    for key, value in overlay.items():
        normalized_key = str(key)
        current = merged.get(normalized_key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[normalized_key] = _merge_mappings(current, value)
        else:
            merged[normalized_key] = value
    return merged


def _validate_document_value(
    relative: str,
    path: str,
    value: object,
    shipped: object,
) -> None:
    location = f"{relative}:{path}"
    if isinstance(shipped, Mapping):
        if not isinstance(value, Mapping):
            raise ConfigurationError(f"{location} must be a mapping")
        for key, shipped_value in shipped.items():
            normalized_key = str(key)
            if normalized_key not in value:
                raise ConfigurationError(f"{location}.{normalized_key} is required")
            child_path = f"{path}.{normalized_key}" if path else normalized_key
            _validate_document_value(
                relative,
                child_path,
                value[normalized_key],
                shipped_value,
            )
        return
    if isinstance(shipped, bool):
        valid_type = isinstance(value, bool)
        expected = "a boolean"
    elif isinstance(shipped, int):
        valid_type = isinstance(value, int) and not isinstance(value, bool)
        expected = "an integer"
    elif isinstance(shipped, float):
        valid_type = isinstance(value, (int, float)) and not isinstance(value, bool)
        expected = "a number"
    else:
        valid_type = isinstance(value, type(shipped))
        expected = f"a {type(shipped).__name__}"
    if not valid_type:
        raise ConfigurationError(f"{location} must be {expected}")
    minimum = MINIMUM_CONFIG_VALUES.get(location)
    if minimum is not None and isinstance(value, int) and value < minimum:
        raise ConfigurationError(f"{location} must be at least {minimum}")


def _validate_config_document(
    relative: str,
    merged: Mapping[str, Any],
    shipped: Mapping[str, Any],
    interpolation: Mapping[str, str],
) -> None:
    _validate_document_value(relative, "", merged, shipped)
    serialized = yaml.safe_dump(dict(merged), sort_keys=True)
    missing = sorted(set(INTERPOLATION.findall(serialized)) - interpolation.keys())
    if missing:
        names = ", ".join(missing)
        raise ConfigurationError(f"{relative} references unavailable interpolation: {names}")


def _validate_documents(config_dir: Path | None, interpolation: Mapping[str, str]) -> None:
    overlay_files: dict[str, Path] = {}
    if config_dir is not None:
        if not config_dir.is_dir():
            raise ConfigurationError(f"AGENT_CONFIG_DIR is not a directory: {config_dir}")
        for path in config_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(config_dir).as_posix()
            if relative not in SHIPPED_CONFIGS:
                raise ConfigurationError(f"overlay has no shipped counterpart: {relative}")
            if relative == FROZEN_CONFIG:
                raise ConfigurationError("policy/hardline.yaml cannot be overlaid")
            overlay_files[relative] = path

    for relative in SHIPPED_CONFIGS:
        shipped = _read_yaml(PACKAGE_ROOT / relative)
        overlay_path = overlay_files.get(relative)
        merged = shipped
        if overlay_path is not None:
            merged = _merge_mappings(shipped, _read_yaml(overlay_path))
        _validate_config_document(relative, merged, shipped, interpolation)


def load_config_document(settings: Settings, relative: str) -> dict[str, Any]:
    """Load one validated versioned document with its recursive operator overlay."""

    if relative not in SHIPPED_CONFIGS:
        raise ConfigurationError(f"unknown shipped configuration document: {relative}")
    shipped = _read_yaml(PACKAGE_ROOT / relative)
    merged = shipped
    if settings.config_dir is not None and relative != FROZEN_CONFIG:
        overlay = settings.config_dir / relative
        if overlay.is_file():
            merged = _merge_mappings(shipped, _read_yaml(overlay))
    _validate_config_document(relative, merged, shipped, settings.interpolation)
    return merged


def validate_settings(settings: Settings) -> None:
    """Refuse unsafe deployment identities before constructing resources."""

    if settings.auth_mode is AuthMode.TOKEN and settings.auth_token is None:
        raise ConfigurationError("AUTH_TOKEN is required when AUTH_MODE=token")
    if settings.sandbox in {SandboxMechanism.DOCKER, SandboxMechanism.FAKE} and (
        settings.deployment_mode is DeploymentMode.PRODUCTION
        or settings.auth_mode is not AuthMode.DEV
    ):
        raise ConfigurationError(
            "unsafe sandbox configuration: "
            f"DEPLOYMENT_MODE={settings.deployment_mode.value}, "
            f"AUTH_MODE={settings.auth_mode.value}, "
            f"SANDBOX_MECHANISM={settings.sandbox.value}; "
            "startup refuses docker and fake unless DEPLOYMENT_MODE=development "
            "and AUTH_MODE=dev"
        )
    if settings.deployment_mode is DeploymentMode.PRODUCTION and settings.auth_mode is AuthMode.DEV:
        raise ConfigurationError(
            "unsafe authentication configuration: DEPLOYMENT_MODE=production refuses AUTH_MODE=dev"
        )


def validate_runtime_identity(
    settings: Settings, *, tenant_id: str, principal_id: str, policy_profile: str
) -> None:
    """Keep evaluation identities and profiles out of production compositions."""

    if settings.deployment_mode is not DeploymentMode.PRODUCTION:
        return
    if (
        tenant_id.startswith("tenant_eval")
        or principal_id.startswith("eval.")
        or policy_profile.startswith("eval.")
    ):
        raise ConfigurationError(
            "production refuses evaluation identity: tenant_id, principal_id, and "
            "policy_profile must not use evaluation namespaces"
        )


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load and validate the environment layer before constructing resources."""

    values = _environment(environ)
    database_url = _required(values, "DATABASE_URL")
    deployment_mode = _parse_enum(
        DeploymentMode, _required(values, "DEPLOYMENT_MODE"), "DEPLOYMENT_MODE"
    )
    auth_mode = _parse_enum(AuthMode, values.get("AUTH_MODE", "dev"), "AUTH_MODE")
    sandbox = _parse_enum(
        SandboxMechanism, values.get("SANDBOX_MECHANISM", "docker"), "SANDBOX_MECHANISM"
    )
    raw_token = values.get("AUTH_TOKEN", "").strip()
    auth_token = SecretStr(raw_token) if raw_token else None

    if auth_mode is AuthMode.TOKEN and auth_token is None:
        raise ConfigurationError("AUTH_TOKEN is required when AUTH_MODE=token")
    raw_dir = values.get("AGENT_CONFIG_DIR", "").strip()
    config_dir = Path(raw_dir).expanduser().resolve() if raw_dir else None
    credentials = {
        name.removesuffix("_API_KEY").lower(): SecretStr(value)
        for name, value in values.items()
        if name.endswith("_API_KEY") and value.strip()
    }
    interpolation = {"OPENAI_MODEL": values.get("OPENAI_MODEL", "")}
    _validate_documents(config_dir, interpolation)

    settings = Settings(
        database_url=database_url,
        deployment_mode=deployment_mode,
        auth_mode=auth_mode,
        auth_token=auth_token,
        sandbox=sandbox,
        config_dir=config_dir,
        credentials=MappingProxyType(credentials),
        interpolation=MappingProxyType(interpolation),
    )
    validate_settings(settings)
    return settings
