"""Deployment settings and validation for the composition boundary."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError

from agent_core.domain.browser import normalize_browser_origin
from agent_core.domain.memory import (
    MemoryDistillationEvidence,
    ProviderExtractionEvaluationEvidence,
)
from agent_core.policy.scopes import PLATFORM_SCOPES


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


class WebProviderKind(StrEnum):
    DISABLED = "disabled"
    TAVILY = "tavily"
    FIRECRAWL = "firecrawl"
    KEENABLE = "keenable"


@dataclass(frozen=True, slots=True)
class WebProviderAllocation:
    provider: WebProviderKind
    weight: int


class MemoryProviderExtractionMode(StrEnum):
    AUTO = "auto"
    OFF = "off"
    REQUIRED = "required"


class BrowserProviderKind(StrEnum):
    DISABLED = "disabled"
    PLAYWRIGHT = "playwright"
    HOSTED = "hosted"


class PushProviderKind(StrEnum):
    DISABLED = "disabled"
    APNS = "apns"


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
    trajectory_export_enabled: bool = False
    skill_authoring_enabled: bool = False
    skill_background_review_enabled: bool = False
    memory_provider_extraction_mode: MemoryProviderExtractionMode = (
        MemoryProviderExtractionMode.AUTO
    )
    memory_provider_extraction_evidence: Path | None = None
    schedule_api_enabled: bool = False
    schedule_worker_enabled: bool = False
    notification_api_enabled: bool = False
    notification_dispatch_enabled: bool = False
    memory_api_enabled: bool = False
    persona_api_enabled: bool = False
    delegation_enabled: bool = False
    email_enabled: bool = False
    push_provider: PushProviderKind = PushProviderKind.DISABLED
    apns_key_file: Path | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_topic: str | None = None
    artifact_root: Path = Path(".agent/artifacts")
    auth_tenant_id: str = ""
    auth_principal_id: str = ""
    auth_roles: frozenset[str] = frozenset()
    auth_scopes: frozenset[str] = frozenset()
    sandbox_image: str = "agent-core-sandbox:dev"
    sandbox_passthrough: tuple[str, ...] = ()
    execution_service_socket: Path | None = None
    release_id: str | None = None
    web_search_providers: tuple[WebProviderAllocation, ...] = ()
    web_fetch_providers: tuple[WebProviderAllocation, ...] = ()
    browser_provider: BrowserProviderKind = BrowserProviderKind.DISABLED
    browser_allowed_origins: tuple[str, ...] = ()
    browser_profile_service_url: str | None = None
    browser_profile_id: UUID | None = None
    browser_grant_id: UUID | None = None
    browser_run_purpose: str | None = None

    @property
    def web_search_provider(self) -> WebProviderKind:
        return _legacy_web_provider(self.web_search_providers)

    @property
    def web_fetch_provider(self) -> WebProviderKind:
        return _legacy_web_provider(self.web_fetch_providers)


PACKAGE_ROOT = Path(__file__).resolve().parent
PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT = PACKAGE_ROOT / "memory" / "release_evidence"
SHIPPED_CONFIGS = (
    "policy/hardline.yaml",
    "policy/default.yaml",
    "models/policies.yaml",
    "models/catalog.yaml",
    "models/providers/openai.yaml",
    "models/providers/anthropic.yaml",
    "models/providers/ollama.yaml",
    "context/plan.yaml",
    "tools/limits.yaml",
    "runtime/limits.yaml",
    "sandbox/limits.yaml",
    "memory/profiles.yaml",
)
# The design corpus declares 161 operator-reviewable knobs. Metadata such as
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
            "classes.persona.max_items",
            "classes.persona.max_tokens",
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
            "worker.reserved_interactive_slots",
            "worker.reserved_async_slots",
            "sweeps.reclaim_expired_seconds",
            "sweeps.approval_reaper_seconds",
            "sweeps.checkpoint_prune_seconds",
            "sweeps.projection_catch_up_seconds",
            "model.max_internal_attempts",
            "context.max_compactions_per_step",
            "run_defaults.max_steps",
            "run_defaults.max_model_calls",
            "run_defaults.max_tool_calls",
            "scheduling.scan_batch",
            "scheduling.fallback_poll_seconds",
            "scheduling.admission_backoff_seconds",
            "scheduling.max_run_timeout_seconds",
            "scheduling.max_misfire_grace_seconds",
            "scheduling.max_steps_per_run",
            "scheduling.max_model_calls_per_run",
            "scheduling.max_tool_calls_per_run",
            "scheduling.max_cost_per_run",
            "scheduling.max_active_runs_per_tenant",
            "scheduling.max_materializations_per_minute",
            "scheduling.daily_cost",
            "scheduling.monthly_cost",
            "notifications.claim_batch",
            "notifications.lease_seconds",
            "notifications.fallback_poll_seconds",
            "notifications.retry_delays_seconds",
            "notifications.terminal_expiry_seconds",
            "delegation.max_children_per_call",
            "delegation.max_live_children_per_parent",
            "delegation.max_depth",
            "delegation.max_live_delegated_runs_per_tenant",
            "delegation.child_max_steps",
            "delegation.child_max_model_calls",
            "delegation.child_max_tool_calls",
            "delegation.child_max_cost",
            "delegation.child_wall_seconds",
            "delegation.synthesis_reserve_steps",
            "delegation.synthesis_reserve_model_calls",
            "delegation.synthesis_reserve_cost",
            "delegation.summary_max_bytes",
        ),
        "memory/profiles.yaml": (
            "formation.session_boundary_enabled",
            "formation.scheduled_enabled",
            "formation.scheduled_interval_seconds",
            "formation.established_facts_enabled",
            "formation.decay.floor_confidence",
            "formation.decay.step",
            "formation.decay.max_per_sweep",
            "formation.persona_nomination.min_confidence",
            "formation.persona_nomination.min_corroboration",
            "formation.persona_nomination.max_open",
            "retrieval.semantic_enabled",
            "retrieval.reciprocal_rank_fusion_k",
            "retrieval.durable_item_share",
            "retrieval.ranking_weights.match",
            "retrieval.ranking_weights.confidence",
            "retrieval.ranking_weights.reinforce",
            "retrieval.ranking_weights.authority",
            "retrieval.ranking_weights.scope",
            "retrieval.ranking_weights.utility",
            "retrieval.lifecycle_weights.active",
            "retrieval.lifecycle_weights.provisional",
            "retrieval.decay_tau_days.fact",
            "retrieval.decay_tau_days.preference",
            "retrieval.decay_tau_days.relationship",
            "retrieval.decay_tau_days.user_model_attr",
            "retrieval.decay_tau_days.procedure_pointer",
            "retrieval.stale_penalty",
            "retrieval.near_duplicate_penalty",
            "retrieval.usage.cited_utility_delta",
            "retrieval.usage.uncited_utility_delta",
            "snapshots.async.max_items",
            "snapshots.async.max_tokens",
            "snapshots.async.max_window_ratio",
            "snapshots.child.max_items",
            "snapshots.child.max_tokens",
            "snapshots.child.max_window_ratio",
            "traces.operator_retention_days",
        ),
    }
)
FROZEN_CONFIG = "policy/hardline.yaml"
INTERPOLATION = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
RELEASE_ID_PATTERN = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{7,40}")
MINIMUM_CONFIG_VALUES: Mapping[str, float] = MappingProxyType(
    {
        "runtime/limits.yaml:model.max_internal_attempts": 1,
        "runtime/limits.yaml:queue.max_attempts": 1,
        "runtime/limits.yaml:run_defaults.max_steps": 1,
        "runtime/limits.yaml:run_defaults.max_model_calls": 1,
        "runtime/limits.yaml:run_defaults.max_tool_calls": 1,
        "runtime/limits.yaml:worker.heartbeat_divisor": 2,
        "runtime/limits.yaml:worker.lease_seconds": 1,
        "runtime/limits.yaml:worker.reserved_interactive_slots": 1,
        "runtime/limits.yaml:worker.reserved_async_slots": 1,
        "runtime/limits.yaml:scheduling.scan_batch": 1,
        "runtime/limits.yaml:scheduling.fallback_poll_seconds": 1,
        "runtime/limits.yaml:scheduling.admission_backoff_seconds": 1,
        "runtime/limits.yaml:scheduling.max_run_timeout_seconds": 1,
        "runtime/limits.yaml:scheduling.max_misfire_grace_seconds": 1,
        "runtime/limits.yaml:scheduling.max_steps_per_run": 1,
        "runtime/limits.yaml:scheduling.max_model_calls_per_run": 1,
        "runtime/limits.yaml:scheduling.max_tool_calls_per_run": 1,
        "runtime/limits.yaml:scheduling.max_cost_per_run": 0.01,
        "runtime/limits.yaml:scheduling.max_active_runs_per_tenant": 1,
        "runtime/limits.yaml:scheduling.max_materializations_per_minute": 1,
        "runtime/limits.yaml:scheduling.daily_cost": 0.01,
        "runtime/limits.yaml:scheduling.monthly_cost": 0.01,
        "runtime/limits.yaml:notifications.claim_batch": 1,
        "runtime/limits.yaml:notifications.lease_seconds": 1,
        "runtime/limits.yaml:notifications.fallback_poll_seconds": 1,
        "runtime/limits.yaml:notifications.terminal_expiry_seconds": 1,
        "runtime/limits.yaml:delegation.max_children_per_call": 1,
        "runtime/limits.yaml:delegation.max_live_children_per_parent": 1,
        "runtime/limits.yaml:delegation.max_depth": 1,
        "runtime/limits.yaml:delegation.max_live_delegated_runs_per_tenant": 1,
        "runtime/limits.yaml:delegation.child_max_steps": 1,
        "runtime/limits.yaml:delegation.child_max_model_calls": 1,
        "runtime/limits.yaml:delegation.child_max_tool_calls": 1,
        "runtime/limits.yaml:delegation.child_max_cost": 0.01,
        "runtime/limits.yaml:delegation.child_wall_seconds": 1,
        "runtime/limits.yaml:delegation.synthesis_reserve_steps": 1,
        "runtime/limits.yaml:delegation.synthesis_reserve_model_calls": 1,
        "runtime/limits.yaml:delegation.synthesis_reserve_cost": 0.01,
        "runtime/limits.yaml:delegation.summary_max_bytes": 1,
        "memory/profiles.yaml:formation.scheduled_interval_seconds": 1,
        "memory/profiles.yaml:formation.decay.max_per_sweep": 1,
        "memory/profiles.yaml:retrieval.reciprocal_rank_fusion_k": 1,
        "memory/profiles.yaml:retrieval.decay_tau_days.fact": 1,
        "memory/profiles.yaml:retrieval.decay_tau_days.preference": 1,
        "memory/profiles.yaml:retrieval.decay_tau_days.relationship": 1,
        "memory/profiles.yaml:retrieval.decay_tau_days.user_model_attr": 1,
        "memory/profiles.yaml:retrieval.decay_tau_days.procedure_pointer": 1,
        "memory/profiles.yaml:snapshots.async.max_items": 1,
        "memory/profiles.yaml:snapshots.async.max_tokens": 1,
        "memory/profiles.yaml:snapshots.child.max_items": 1,
        "memory/profiles.yaml:snapshots.child.max_tokens": 1,
        "memory/profiles.yaml:traces.operator_retention_days": 1,
        "tools/limits.yaml:circuit_breaker.identical_call_threshold": 2,
        "tools/limits.yaml:circuit_breaker.identical_denied_threshold": 1,
        "tools/limits.yaml:circuit_breaker.uncertain_threshold": 1,
        "tools/limits.yaml:parallel.maximum_calls": 1,
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


def _legacy_web_provider(
    allocations: tuple[WebProviderAllocation, ...],
) -> WebProviderKind:
    if not allocations:
        return WebProviderKind.DISABLED
    if len(allocations) == 1 and allocations[0].weight == 100:
        return allocations[0].provider
    raise ConfigurationError("weighted web-provider selection has no singular provider")


def _parse_web_provider_allocations(
    values: Mapping[str, str],
    *,
    singular_name: str,
    plural_name: str,
) -> tuple[WebProviderAllocation, ...]:
    plural = values.get(plural_name, "").strip()
    singular = values.get(singular_name, "disabled").strip()
    if not plural:
        provider = _parse_enum(WebProviderKind, singular, singular_name)
        return (
            () if provider is WebProviderKind.DISABLED else (WebProviderAllocation(provider, 100),)
        )
    if singular not in {"", WebProviderKind.DISABLED.value}:
        raise ConfigurationError(f"{plural_name} cannot be combined with enabled {singular_name}")
    if plural == WebProviderKind.DISABLED.value:
        return ()

    allocations: list[WebProviderAllocation] = []
    for entry in plural.split(","):
        provider_name, separator, raw_weight = entry.strip().partition(":")
        if not separator or not provider_name or not raw_weight:
            raise ConfigurationError(f"{plural_name} entries must use provider:percentage")
        provider = _parse_enum(WebProviderKind, provider_name, plural_name)
        if provider is WebProviderKind.DISABLED:
            raise ConfigurationError(f"{plural_name} cannot weight the disabled provider")
        try:
            weight = int(raw_weight)
        except ValueError as exc:
            raise ConfigurationError(f"{plural_name} percentages must be integers") from exc
        if weight <= 0:
            raise ConfigurationError(f"{plural_name} percentages must be positive")
        allocations.append(WebProviderAllocation(provider=provider, weight=weight))
    provider_kinds = [allocation.provider for allocation in allocations]
    if len(set(provider_kinds)) != len(provider_kinds):
        raise ConfigurationError(f"{plural_name} cannot contain duplicate providers")
    if sum(allocation.weight for allocation in allocations) != 100:
        raise ConfigurationError(f"{plural_name} percentages must sum to 100")
    return tuple(allocations)


def _parse_flag(values: Mapping[str, str], name: str) -> bool:
    raw = values.get(name, "0").strip()
    if raw not in {"0", "1"}:
        raise ConfigurationError(f"{name} must be 0 or 1")
    return raw == "1"


def _optional_uuid(values: Mapping[str, str], name: str) -> UUID | None:
    raw = values.get(name, "").strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a UUID") from exc


def _read_private_credential_file(raw_path: str) -> str:
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError("browser control-plane credential file is invalid")
    try:
        metadata = path.stat()
        payload = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError("browser control-plane credential file is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 4096:
        raise ConfigurationError("browser control-plane credential file is invalid")
    try:
        value = payload.decode("ascii").removesuffix("\n").removesuffix("\r")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("browser control-plane credential file is invalid") from exc
    if not 32 <= len(value) <= 512 or any(character.isspace() for character in value):
        raise ConfigurationError("browser control-plane credential file is invalid")
    return value


_GMAIL_CREDENTIAL_FILES = {
    "gmail_read": (
        "GMAIL_READ_CREDENTIAL_FILE",
        "https://www.googleapis.com/auth/gmail.readonly",
    ),
    "gmail_write": (
        "GMAIL_WRITE_CREDENTIAL_FILE",
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    "gmail_send": (
        "GMAIL_SEND_CREDENTIAL_FILE",
        "https://www.googleapis.com/auth/gmail.send",
    ),
}


def _read_gmail_credential_file(raw_path: str, name: str, expected_scope: str) -> str:
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError(f"{name} must be an absolute private regular file")
    try:
        metadata = path.stat()
        payload = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"{name} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 1 <= metadata.st_size <= 16_384
    ):
        raise ConfigurationError(f"{name} must be a 0600 regular file under 16 KiB")
    try:
        loaded: object = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"{name} is not a valid credential document") from exc
    if not isinstance(loaded, dict) or set(loaded) != {
        "client_id",
        "client_secret",
        "refresh_token",
        "scope",
    }:
        raise ConfigurationError(f"{name} has an invalid credential shape")
    for field in ("client_id", "client_secret", "refresh_token"):
        value = loaded.get(field)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ConfigurationError(f"{name} has an invalid credential shape")
    if loaded.get("scope") != expected_scope:
        raise ConfigurationError(f"{name} does not carry its exact Google scope")
    return json.dumps(loaded, sort_keys=True, separators=(",", ":"))


def _validate_private_regular_file(path: Path, name: str) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError(f"{name} must be an absolute private regular file")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ConfigurationError(f"{name} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConfigurationError(f"{name} must be a regular file with mode 0600")


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
    if (
        minimum is not None
        and isinstance(value, (int, float))
        and ((isinstance(value, float) and not isfinite(value)) or value < minimum)
    ):
        raise ConfigurationError(f"{location} must be at least {minimum}")


def _validate_config_document(
    relative: str,
    merged: Mapping[str, Any],
    shipped: Mapping[str, Any],
    interpolation: Mapping[str, str],
) -> None:
    _validate_document_value(relative, "", merged, shipped)
    if relative == "runtime/limits.yaml":
        retry_delays = merged["notifications"]["retry_delays_seconds"]
        if (
            not isinstance(retry_delays, list)
            or not retry_delays
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or value <= 0
                for value in retry_delays
            )
        ):
            raise ConfigurationError(
                "runtime/limits.yaml:notifications.retry_delays_seconds "
                "must contain positive numbers"
            )
    if relative == "sandbox/limits.yaml":
        resources = merged["resources"]
        for name, value in resources.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigurationError(
                    f"sandbox/limits.yaml:resources.{name} must be a positive integer"
                )
        artifacts = merged["artifacts"]
        for name, value in artifacts.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigurationError(
                    f"sandbox/limits.yaml:artifacts.{name} must be a positive integer"
                )
        egress = merged["egress"]
        if egress["mode"] not in {"deny", "allowlist"}:
            raise ConfigurationError("sandbox/limits.yaml:egress.mode must be deny or allowlist")
        destinations = egress["destinations"]
        if not isinstance(destinations, list):
            raise ConfigurationError("sandbox/limits.yaml:egress.destinations must be a list")
        from agent_core.execution.egress_core import validate_host_and_ports

        for index, destination in enumerate(destinations):
            if not isinstance(destination, Mapping):
                raise ConfigurationError(
                    f"sandbox/limits.yaml:egress.destinations.{index} must be a mapping"
                )
            try:
                host = destination["host"]
                ports = destination["ports"]
                if (
                    not isinstance(host, str)
                    or not isinstance(ports, list)
                    or any(not isinstance(port, int) or isinstance(port, bool) for port in ports)
                ):
                    raise TypeError
                validate_host_and_ports(host, frozenset(ports))
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"sandbox/limits.yaml:egress.destinations.{index} is invalid"
                ) from exc
    _validate_interpolation(relative, merged, interpolation)


def _validate_interpolation(
    relative: str,
    document: Mapping[str, Any],
    interpolation: Mapping[str, str],
) -> None:
    serialized = yaml.safe_dump(dict(document), sort_keys=True)
    referenced = set(INTERPOLATION.findall(serialized))
    if relative.startswith("policy/") and referenced:
        raise ConfigurationError(
            f"{relative} cannot use policy-semantic interpolation; policy hashes must cover "
            "the effective rules"
        )
    missing = sorted(referenced - interpolation.keys())
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
            is_provider_profile = relative.startswith("models/providers/") and relative.endswith(
                ".yaml"
            )
            if relative not in SHIPPED_CONFIGS and not is_provider_profile:
                raise ConfigurationError(f"overlay has no shipped counterpart: {relative}")
            if relative == FROZEN_CONFIG:
                raise ConfigurationError("policy/hardline.yaml cannot be overlaid")
            if relative in SHIPPED_CONFIGS:
                overlay_files[relative] = path
            elif is_provider_profile:
                _validate_interpolation(relative, _read_yaml(path), interpolation)

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


def _validate_release_id(release_id: str | None) -> None:
    if release_id is not None and RELEASE_ID_PATTERN.fullmatch(release_id) is None:
        raise ConfigurationError(
            "VEETBOT_RELEASE_ID must be YYYYMMDD-HHMMSS followed by a 7-40 character "
            "lowercase hexadecimal revision"
        )


def load_provider_extraction_evidence(
    path: Path,
) -> ProviderExtractionEvaluationEvidence:
    """Load a reviewable activation artifact and reject incomplete or failed evidence."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ProviderExtractionEvaluationEvidence.model_validate(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ConfigurationError(
            "provider-backed memory extraction evaluation evidence did not pass"
        ) from exc


def load_memory_distillation_evidence(path: Path) -> MemoryDistillationEvidence:
    """Load a passing formation@9 comparative-evaluation artifact."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return MemoryDistillationEvidence.model_validate(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ConfigurationError(
            "adaptive memory distillation evaluation evidence did not pass"
        ) from exc


def provider_extraction_evidence_paths(settings: Settings) -> tuple[Path, ...]:
    """Return operator evidence first, followed by immutable release-bundled evidence."""

    paths: list[Path] = []
    if settings.memory_provider_extraction_evidence is not None:
        paths.append(settings.memory_provider_extraction_evidence)
    if PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT.is_dir():
        paths.extend(sorted(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT.glob("*.json")))
    return tuple(dict.fromkeys(paths))


def _provider_extraction_evidence_is_valid(path: Path) -> bool:
    try:
        load_provider_extraction_evidence(path)
    except ConfigurationError:
        try:
            load_memory_distillation_evidence(path)
        except ConfigurationError:
            return False
    return True


def validate_settings(
    settings: Settings,
    *,
    require_auth_token: bool = True,
    require_execution_environment: bool = True,
) -> None:
    """Refuse unsafe deployment identities before constructing resources."""

    _validate_release_id(settings.release_id)
    if settings.memory_provider_extraction_mode is MemoryProviderExtractionMode.REQUIRED:
        evidence_paths = provider_extraction_evidence_paths(settings)
        if not evidence_paths:
            raise ConfigurationError(
                "provider-backed memory extraction requires evaluation evidence"
            )
        required_paths = (
            (settings.memory_provider_extraction_evidence,)
            if settings.memory_provider_extraction_evidence is not None
            else evidence_paths
        )
        if not any(_provider_extraction_evidence_is_valid(path) for path in required_paths):
            raise ConfigurationError(
                "provider-backed memory extraction evaluation evidence did not pass"
            )
    if settings.skill_background_review_enabled and not settings.skill_authoring_enabled:
        raise ConfigurationError("skill background review requires skill authoring to be enabled")
    if settings.notification_api_enabled != settings.notification_dispatch_enabled:
        raise ConfigurationError(
            "notification API and dispatch flags must be enabled or disabled together"
        )
    gmail_credential_names = set(_GMAIL_CREDENTIAL_FILES)
    configured_gmail_credentials = gmail_credential_names & set(settings.credentials)
    if settings.email_enabled and configured_gmail_credentials != gmail_credential_names:
        raise ConfigurationError("email enablement requires all three Gmail credential files")
    if not settings.email_enabled and configured_gmail_credentials:
        raise ConfigurationError("Gmail credentials require AGENT_EMAIL_ENABLED=1")
    apns_values = {
        "APNS_KEY_FILE": settings.apns_key_file,
        "APNS_KEY_ID": settings.apns_key_id,
        "APNS_TEAM_ID": settings.apns_team_id,
        "APNS_TOPIC": settings.apns_topic,
    }
    configured_apns = [name for name, value in apns_values.items() if value is not None]
    if settings.push_provider is PushProviderKind.APNS:
        missing_apns = [name for name, value in apns_values.items() if value is None]
        if missing_apns:
            raise ConfigurationError("PUSH_PROVIDER=apns requires " + ", ".join(missing_apns))
        if not settings.notification_dispatch_enabled:
            raise ConfigurationError(
                "PUSH_PROVIDER=apns requires notification dispatch to be enabled"
            )
        assert settings.apns_key_file is not None
        _validate_private_regular_file(settings.apns_key_file, "APNS_KEY_FILE")
    elif configured_apns:
        raise ConfigurationError(", ".join(configured_apns) + " require PUSH_PROVIDER=apns")
    if require_auth_token and settings.auth_mode is AuthMode.TOKEN and settings.auth_token is None:
        raise ConfigurationError("AUTH_TOKEN is required when AUTH_MODE=token")
    if (
        require_execution_environment
        and settings.sandbox in {SandboxMechanism.DOCKER, SandboxMechanism.FAKE}
        and (
            settings.deployment_mode is DeploymentMode.PRODUCTION
            or settings.auth_mode is not AuthMode.DEV
        )
    ):
        raise ConfigurationError(
            "unsafe sandbox configuration: "
            f"DEPLOYMENT_MODE={settings.deployment_mode.value}, "
            f"AUTH_MODE={settings.auth_mode.value}, "
            f"SANDBOX_MECHANISM={settings.sandbox.value}; "
            "startup refuses docker and fake unless DEPLOYMENT_MODE=development "
            "and AUTH_MODE=dev"
        )
    if (
        require_execution_environment
        and settings.deployment_mode is DeploymentMode.PRODUCTION
        and settings.sandbox in {SandboxMechanism.GVISOR, SandboxMechanism.MICROVM}
        and settings.execution_service_socket is None
    ):
        raise ConfigurationError(
            "production sandboxing requires AGENT_EXECUTION_SERVICE_SOCKET so application "
            "processes never access the container runtime"
        )
    if settings.deployment_mode is DeploymentMode.PRODUCTION and settings.auth_mode is AuthMode.DEV:
        raise ConfigurationError(
            "unsafe authentication configuration: DEPLOYMENT_MODE=production refuses AUTH_MODE=dev"
        )
    if settings.auth_mode is AuthMode.TOKEN:
        required_identity = {
            "AUTH_TENANT_ID": settings.auth_tenant_id,
            "AUTH_PRINCIPAL_ID": settings.auth_principal_id,
            "AUTH_SCOPES": settings.auth_scopes,
        }
        missing = [name for name, value in required_identity.items() if not value]
        if missing:
            raise ConfigurationError(
                "token authentication requires a configured principal: " + ", ".join(missing)
            )
    if (
        settings.browser_provider is BrowserProviderKind.PLAYWRIGHT
        and not settings.browser_allowed_origins
    ):
        raise ConfigurationError(
            "BROWSER_ALLOWED_ORIGINS is required when BROWSER_PROVIDER=playwright"
        )
    if settings.browser_provider is BrowserProviderKind.HOSTED:
        if settings.browser_profile_service_url is None:
            raise ConfigurationError(
                "BROWSER_PROFILE_SERVICE_URL is required when BROWSER_PROVIDER=hosted"
            )
        if settings.browser_profile_id is None and settings.browser_allowed_origins:
            raise ConfigurationError(
                "BROWSER_PROFILE_ID is required when hosted origins are configured"
            )
        if settings.browser_profile_id is not None and not settings.browser_allowed_origins:
            raise ConfigurationError(
                "BROWSER_ALLOWED_ORIGINS is required for a pinned hosted browser profile"
            )
        if "browser_profile_control_plane" not in settings.credentials:
            raise ConfigurationError(
                "a browser profile control-plane credential is required when "
                "BROWSER_PROVIDER=hosted"
            )
    elif settings.browser_profile_id is not None:
        raise ConfigurationError("BROWSER_PROFILE_ID requires BROWSER_PROVIDER=hosted")
    if (
        settings.browser_grant_id is not None
        and settings.browser_provider is not BrowserProviderKind.HOSTED
    ):
        raise ConfigurationError("BROWSER_GRANT_ID requires BROWSER_PROVIDER=hosted")
    if settings.browser_grant_id is not None and settings.browser_profile_id is None:
        raise ConfigurationError("BROWSER_GRANT_ID requires BROWSER_PROFILE_ID")
    if settings.browser_run_purpose is not None and settings.browser_grant_id is None:
        raise ConfigurationError("BROWSER_RUN_PURPOSE requires BROWSER_GRANT_ID")


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

    return _load_settings(
        environ,
        require_auth_token=True,
        require_execution_environment=True,
    )


def load_schedule_worker_settings(
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load the credential-minimized environment for the scheduler-only role."""

    return _load_settings(
        environ,
        require_auth_token=False,
        require_execution_environment=False,
    )


def load_notification_worker_settings(
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load the credential-minimized environment for the notify-only role."""

    return _load_settings(
        environ,
        require_auth_token=False,
        require_execution_environment=False,
    )


def _load_settings(
    environ: Mapping[str, str] | None,
    *,
    require_auth_token: bool,
    require_execution_environment: bool,
) -> Settings:

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

    if require_auth_token and auth_mode is AuthMode.TOKEN and auth_token is None:
        raise ConfigurationError("AUTH_TOKEN is required when AUTH_MODE=token")
    raw_dir = values.get("AGENT_CONFIG_DIR", "").strip()
    config_dir = Path(raw_dir).expanduser().resolve() if raw_dir else None
    credentials = {
        name.removesuffix("_API_KEY").lower(): SecretStr(value)
        for name, value in values.items()
        if name.endswith("_API_KEY") and name != "VEETBOT_OPENAI_KEY" and value.strip()
    }
    browser_credential_file = values.get(
        "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE",
        "",
    ).strip()
    if browser_credential_file:
        if "browser_profile_control_plane" in credentials:
            raise ConfigurationError(
                "configure exactly one browser control-plane credential source"
            )
        credentials["browser_profile_control_plane"] = SecretStr(
            _read_private_credential_file(browser_credential_file)
        )
    veetbot_openai_key = values.get("VEETBOT_OPENAI_KEY", "").strip()
    if veetbot_openai_key:
        credentials["openai"] = SecretStr(veetbot_openai_key)
    interpolation = {"OPENAI_MODEL": values.get("OPENAI_MODEL", "")}
    trajectory_export_enabled = _parse_flag(values, "AGENT_TRAJECTORY_EXPORT_ENABLED")
    skill_authoring_enabled = _parse_flag(values, "AGENT_SKILL_AUTHORING_ENABLED")
    skill_background_review_enabled = _parse_flag(values, "AGENT_SKILL_BACKGROUND_REVIEW_ENABLED")
    raw_memory_mode = values.get("AGENT_MEMORY_PROVIDER_EXTRACTION_MODE", "").strip()
    legacy_memory_enablement = values.get("AGENT_MEMORY_PROVIDER_EXTRACTION_ENABLED", "").strip()
    if raw_memory_mode and legacy_memory_enablement:
        raise ConfigurationError(
            "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE and the legacy enablement flag "
            "are mutually exclusive"
        )
    if legacy_memory_enablement:
        memory_provider_extraction_mode = (
            MemoryProviderExtractionMode.REQUIRED
            if _parse_flag(values, "AGENT_MEMORY_PROVIDER_EXTRACTION_ENABLED")
            else MemoryProviderExtractionMode.OFF
        )
    else:
        memory_provider_extraction_mode = _parse_enum(
            MemoryProviderExtractionMode,
            raw_memory_mode or MemoryProviderExtractionMode.AUTO.value,
            "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE",
        )
    raw_memory_evidence = values.get("AGENT_MEMORY_PROVIDER_EXTRACTION_EVIDENCE", "").strip()
    memory_provider_extraction_evidence = (
        Path(raw_memory_evidence).expanduser().resolve() if raw_memory_evidence else None
    )
    schedule_api_enabled = _parse_flag(values, "AGENT_SCHEDULE_API_ENABLED")
    schedule_worker_enabled = _parse_flag(values, "AGENT_SCHEDULE_WORKER_ENABLED")
    notification_api_enabled = _parse_flag(values, "AGENT_NOTIFICATION_API_ENABLED")
    notification_dispatch_enabled = _parse_flag(values, "AGENT_NOTIFICATION_DISPATCH_ENABLED")
    memory_api_enabled = _parse_flag(values, "AGENT_MEMORY_API_ENABLED")
    persona_api_enabled = _parse_flag(values, "AGENT_PERSONA_API_ENABLED")
    delegation_enabled = _parse_flag(values, "AGENT_DELEGATION_ENABLED")
    email_enabled = _parse_flag(values, "AGENT_EMAIL_ENABLED")
    configured_gmail_files = {
        credential_name: (variable, values.get(variable, "").strip(), scope)
        for credential_name, (variable, scope) in _GMAIL_CREDENTIAL_FILES.items()
        if values.get(variable, "").strip()
    }
    if configured_gmail_files and not email_enabled:
        raise ConfigurationError("Gmail credential files require AGENT_EMAIL_ENABLED=1")
    if email_enabled:
        missing_gmail_files = [
            variable
            for credential_name, (variable, _scope) in _GMAIL_CREDENTIAL_FILES.items()
            if credential_name not in configured_gmail_files
        ]
        if missing_gmail_files:
            raise ConfigurationError(
                "AGENT_EMAIL_ENABLED=1 requires " + ", ".join(missing_gmail_files)
            )
        for credential_name, (variable, raw_path, scope) in configured_gmail_files.items():
            if credential_name in credentials:
                raise ConfigurationError(f"duplicate credential source for {credential_name}")
            credentials[credential_name] = SecretStr(
                _read_gmail_credential_file(raw_path, variable, scope)
            )
    push_provider = _parse_enum(
        PushProviderKind,
        values.get("PUSH_PROVIDER", PushProviderKind.DISABLED.value).strip(),
        "PUSH_PROVIDER",
    )
    raw_apns_key_file = values.get("APNS_KEY_FILE", "").strip()
    apns_key_file = Path(raw_apns_key_file).expanduser() if raw_apns_key_file else None
    apns_key_id = values.get("APNS_KEY_ID", "").strip() or None
    apns_team_id = values.get("APNS_TEAM_ID", "").strip() or None
    apns_topic = values.get("APNS_TOPIC", "").strip() or None
    artifact_root = Path(values.get("AGENT_ARTIFACT_ROOT", ".agent/artifacts")).expanduser()
    auth_tenant_id = values.get("AUTH_TENANT_ID", "").strip()
    auth_principal_id = values.get("AUTH_PRINCIPAL_ID", "").strip()
    auth_roles = frozenset(
        value.strip() for value in values.get("AUTH_ROLES", "").split(",") if value.strip()
    )
    raw_auth_scopes = values.get("AUTH_SCOPES", "").strip()
    auth_scopes = (
        frozenset(value.strip() for value in raw_auth_scopes.split(",") if value.strip())
        if raw_auth_scopes
        else frozenset()
    )
    sandbox_image = values.get("AGENT_SANDBOX_IMAGE", "agent-core-sandbox:dev").strip()
    if not sandbox_image:
        raise ConfigurationError("AGENT_SANDBOX_IMAGE must not be empty")
    sandbox_passthrough = tuple(
        name.strip()
        for name in values.get("AGENT_SANDBOX_PASSTHROUGH", "").split(",")
        if name.strip()
    )
    raw_execution_socket = values.get("AGENT_EXECUTION_SERVICE_SOCKET", "").strip()
    execution_service_socket = Path(raw_execution_socket) if raw_execution_socket else None
    if execution_service_socket is not None and not execution_service_socket.is_absolute():
        raise ConfigurationError("AGENT_EXECUTION_SERVICE_SOCKET must be an absolute path")
    release_id = values.get("VEETBOT_RELEASE_ID", "").strip() or None
    web_search_providers = _parse_web_provider_allocations(
        values,
        singular_name="WEB_SEARCH_PROVIDER",
        plural_name="WEB_SEARCH_PROVIDERS",
    )
    web_fetch_providers = _parse_web_provider_allocations(
        values,
        singular_name="WEB_FETCH_PROVIDER",
        plural_name="WEB_FETCH_PROVIDERS",
    )
    browser_provider = _parse_enum(
        BrowserProviderKind,
        values.get("BROWSER_PROVIDER", "disabled").strip(),
        "BROWSER_PROVIDER",
    )
    raw_browser_origins = tuple(
        value.strip()
        for value in values.get("BROWSER_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    )
    try:
        browser_allowed_origins = tuple(
            normalize_browser_origin(value) for value in raw_browser_origins
        )
    except ValueError as exc:
        raise ConfigurationError("BROWSER_ALLOWED_ORIGINS contains an invalid origin") from exc
    if len(set(browser_allowed_origins)) != len(browser_allowed_origins):
        raise ConfigurationError("BROWSER_ALLOWED_ORIGINS contains duplicate origins")
    browser_profile_service_url = values.get("BROWSER_PROFILE_SERVICE_URL", "").strip() or None
    if browser_profile_service_url is not None:
        parsed_profile_service = urlsplit(browser_profile_service_url)
        if (
            parsed_profile_service.scheme != "https"
            or parsed_profile_service.hostname is None
            or parsed_profile_service.username is not None
            or parsed_profile_service.password is not None
            or parsed_profile_service.path not in {"", "/"}
            or parsed_profile_service.query
            or parsed_profile_service.fragment
        ):
            raise ConfigurationError("BROWSER_PROFILE_SERVICE_URL must be one HTTPS origin")
        browser_profile_service_url = browser_profile_service_url.rstrip("/")
        if "browser_profile_control_plane" not in credentials:
            raise ConfigurationError(
                "a browser profile control-plane credential is required when "
                "BROWSER_PROFILE_SERVICE_URL is configured"
            )
    browser_profile_id = _optional_uuid(values, "BROWSER_PROFILE_ID")
    browser_grant_id = _optional_uuid(values, "BROWSER_GRANT_ID")
    browser_run_purpose = values.get("BROWSER_RUN_PURPOSE", "").strip() or None
    if browser_run_purpose is not None and len(browser_run_purpose) > 255:
        raise ConfigurationError("BROWSER_RUN_PURPOSE must not exceed 255 characters")
    _validate_release_id(release_id)
    if auth_mode is AuthMode.DEV:
        auth_tenant_id = "local"
        auth_principal_id = "local-user"
        auth_roles = frozenset({"user"})
        auth_scopes = PLATFORM_SCOPES
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
        trajectory_export_enabled=trajectory_export_enabled,
        skill_authoring_enabled=skill_authoring_enabled,
        skill_background_review_enabled=skill_background_review_enabled,
        memory_provider_extraction_mode=memory_provider_extraction_mode,
        memory_provider_extraction_evidence=memory_provider_extraction_evidence,
        schedule_api_enabled=schedule_api_enabled,
        schedule_worker_enabled=schedule_worker_enabled,
        notification_api_enabled=notification_api_enabled,
        notification_dispatch_enabled=notification_dispatch_enabled,
        memory_api_enabled=memory_api_enabled,
        persona_api_enabled=persona_api_enabled,
        delegation_enabled=delegation_enabled,
        email_enabled=email_enabled,
        push_provider=push_provider,
        apns_key_file=apns_key_file,
        apns_key_id=apns_key_id,
        apns_team_id=apns_team_id,
        apns_topic=apns_topic,
        artifact_root=artifact_root,
        auth_tenant_id=auth_tenant_id,
        auth_principal_id=auth_principal_id,
        auth_roles=auth_roles,
        auth_scopes=auth_scopes,
        sandbox_image=sandbox_image,
        sandbox_passthrough=sandbox_passthrough,
        execution_service_socket=execution_service_socket,
        release_id=release_id,
        web_search_providers=web_search_providers,
        web_fetch_providers=web_fetch_providers,
        browser_provider=browser_provider,
        browser_allowed_origins=browser_allowed_origins,
        browser_profile_service_url=browser_profile_service_url,
        browser_profile_id=browser_profile_id,
        browser_grant_id=browser_grant_id,
        browser_run_purpose=browser_run_purpose,
    )
    validate_settings(
        settings,
        require_auth_token=require_auth_token,
        require_execution_environment=require_execution_environment,
    )
    return settings
