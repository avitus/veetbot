"""Strict declarative provider-profile registry and router."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Never
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_core.config import ConfigurationError
from agent_core.domain.messages import (
    CapabilitySet,
    CostSource,
    ModelCapabilities,
    ModelLimits,
    ModelPricing,
    ProviderPin,
    ReasoningSupport,
    ResolvedModel,
)
from agent_core.ports.determinism import Clock

PROFILE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
CREDENTIAL_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
CAPABILITY_FIELDS = frozenset(ModelCapabilities.model_fields)
PROFILE_LIMIT_FIELDS = frozenset(
    {"max_cache_breakpoints", "default_output_reserve", "max_tool_count"}
)
PRICING_FIELDS = frozenset(
    {
        "input_per_mtok",
        "cached_input_per_mtok",
        "cache_write_per_mtok",
        "output_per_mtok",
        "reasoning_per_mtok",
        "reasoning_priced_separately",
        "source",
        "effective_at",
    }
)


class ProfileValidationError(ConfigurationError):
    """A profile names the strict loader rule it violated."""


class InBandReasoning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: str = Field(min_length=1)
    close: str = Field(min_length=1)


class ProfileLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_cache_breakpoints: int = Field(ge=0)
    default_output_reserve: int = Field(gt=0)
    max_tool_count: int | None = Field(default=None, ge=1)


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    catalog: str | None = None
    limits: dict[str, Any] | None = None
    pricing: dict[str, Any] | None = None


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    profile: str
    adapter: str
    api: Literal["responses", "messages", "chat_completions"]
    base_url: str
    credential_ref: str | None
    enabled: bool
    in_band_reasoning: InBandReasoning | None = None
    capabilities: ModelCapabilities
    limits: ProfileLimits
    models: list[ModelEntry] = Field(min_length=1, max_length=200)


class PolicyTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str


class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    enabled_profiles: list[str]
    model_policies: dict[str, PolicyTarget]
    request_defaults: dict[str, int | float]
    cache: dict[str, float]


@dataclass(frozen=True, slots=True)
class AdapterDefinition:
    apis: frozenset[str]
    ceiling: ModelCapabilities


@dataclass(frozen=True, slots=True)
class RegistryModel:
    profile: str
    model_id: str
    aliases: tuple[str, ...]
    limits: ModelLimits
    pricing: ModelPricing


@dataclass(frozen=True, slots=True)
class LoadedProfile:
    document: ProviderProfile
    profile_hash: str
    registry_version: str
    models: tuple[RegistryModel, ...]


def _canonical_hash(value: object) -> str:
    def yaml_scalar(nested: object) -> str:
        if isinstance(nested, (date, datetime)):
            return nested.isoformat()
        raise TypeError(f"unsupported YAML scalar {type(nested).__name__}")

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=yaml_scalar,
        )
    except (TypeError, ValueError) as exc:
        raise ProfileValidationError(
            "provider registry: any level: unsupported value in canonical hash"
        ) from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        value: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileValidationError(f"{path}: any level: invalid YAML mapping") from exc
    if not isinstance(value, dict):
        raise ProfileValidationError(f"{path}: any level: document must be a mapping")
    return {str(key): nested for key, nested in value.items()}


def _merged_mapping(shipped: Path, overlay: Path | None) -> dict[str, Any]:
    base = _load_mapping(shipped)
    if overlay is None or not overlay.exists():
        return base
    return {**base, **_load_mapping(overlay)}


def _fail(path: Path, field: str, rule: str) -> Never:
    raise ProfileValidationError(f"{path}: {field}: {rule}")


def _validate_url(path: Path, value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return
    _fail(path, "base_url", "must be absolute https or http on a loopback host")


def _pricing(path: Path, raw: dict[str, Any]) -> ModelPricing:
    if set(raw) != PRICING_FIELDS:
        _fail(path, "models[].pricing", "all pricing fields are required and unknown keys fail")
    for field_name in ("input_per_mtok", "cached_input_per_mtok", "output_per_mtok"):
        if not isinstance(raw[field_name], str):
            _fail(path, f"pricing.{field_name}", "pricing amounts must be decimal strings")
    for field_name in ("cache_write_per_mtok", "reasoning_per_mtok"):
        value = raw[field_name]
        if value is not None and not isinstance(value, str):
            _fail(
                path,
                f"pricing.{field_name}",
                "pricing amounts must be decimal strings or null",
            )
    effective = raw["effective_at"]
    if not isinstance(effective, str):
        _fail(path, "pricing.effective_at", "must be RFC 3339 with an explicit offset")
    try:
        parsed_effective = datetime.fromisoformat(effective.replace("Z", "+00:00"))
    except ValueError:
        _fail(path, "pricing.effective_at", "must be RFC 3339 with an explicit offset")
    if parsed_effective.tzinfo is None:
        _fail(path, "pricing.effective_at", "must be RFC 3339 with an explicit offset")
    source_value = raw["source"]
    source_map = {
        "provider_cost_api": CostSource.PROVIDER_COST_API,
        "generation_usage": CostSource.GENERATION_USAGE,
        "model_catalog": CostSource.MODEL_CATALOG,
        "published": CostSource.DOCS_SNAPSHOT,
        "docs_snapshot": CostSource.DOCS_SNAPSHOT,
        "config_override": CostSource.CONFIG_OVERRIDE,
    }
    if source_value not in source_map:
        _fail(path, "pricing.source", "must name a declared cost source")
    try:
        return ModelPricing(
            input_per_mtok=Decimal(raw["input_per_mtok"]),
            cached_input_per_mtok=Decimal(raw["cached_input_per_mtok"]),
            cache_write_per_mtok=(
                None
                if raw["cache_write_per_mtok"] is None
                else Decimal(raw["cache_write_per_mtok"])
            ),
            output_per_mtok=Decimal(raw["output_per_mtok"]),
            reasoning_per_mtok=(
                None if raw["reasoning_per_mtok"] is None else Decimal(raw["reasoning_per_mtok"])
            ),
            reasoning_priced_separately=bool(raw["reasoning_priced_separately"]),
            source=source_map[str(source_value)],
            effective_at=parsed_effective,
        )
    except (ArithmeticError, TypeError, ValidationError) as exc:
        raise ProfileValidationError(f"{path}: models[].pricing: invalid amount") from exc


class ProviderRegistry:
    """A complete, hashed registry; partial profile loading is never exposed."""

    def __init__(
        self,
        *,
        policies: PolicyDocument,
        profiles: dict[str, LoadedProfile],
        aliases: dict[str, RegistryModel],
    ) -> None:
        self.policies = policies
        self.profiles = profiles
        self.aliases = aliases

    @classmethod
    def load(
        cls,
        models_root: Path,
        *,
        adapters: dict[str, AdapterDefinition],
        overlay_root: Path | None = None,
    ) -> ProviderRegistry:
        policy_path = models_root / "policies.yaml"
        policy_overlay = None if overlay_root is None else overlay_root / "models/policies.yaml"
        policy_raw = _merged_mapping(policy_path, policy_overlay)
        try:
            policies = PolicyDocument.model_validate(policy_raw)
        except ValidationError as exc:
            raise ProfileValidationError(
                f"{policy_path}: any level: unknown or invalid key"
            ) from exc
        if len(policies.enabled_profiles) != len(set(policies.enabled_profiles)):
            _fail(
                policy_path,
                "enabled_profiles",
                "profile names must be unique across the merged registry",
            )

        catalog_path = models_root / "catalog.yaml"
        catalog_overlay = None if overlay_root is None else overlay_root / "models/catalog.yaml"
        catalog = _merged_mapping(catalog_path, catalog_overlay)
        entries = catalog.get("entries")
        if not isinstance(entries, dict):
            _fail(catalog_path, "entries", "must be a mapping")

        profile_dir = models_root / "providers"
        sources = {path.stem: path for path in profile_dir.glob("*.yaml")}
        if overlay_root is not None:
            overlay_dir = overlay_root / "models/providers"
            if overlay_dir.exists():
                for path in overlay_dir.glob("*.yaml"):
                    sources.setdefault(path.stem, profile_dir / path.name)

        raw_profiles: dict[str, tuple[Path, dict[str, Any]]] = {}
        profile_hashes: dict[str, str] = {}
        for name in policies.enabled_profiles:
            shipped = sources.get(name)
            if shipped is None:
                _fail(policy_path, "enabled_profiles", f"profile {name!r} has no document")
            overlay = (
                None if overlay_root is None else overlay_root / "models/providers" / shipped.name
            )
            if shipped.exists():
                raw = _merged_mapping(shipped, overlay)
            elif overlay is not None and overlay.exists():
                raw = _load_mapping(overlay)
            else:
                _fail(policy_path, "enabled_profiles", f"profile {name!r} has no document")
            raw_profiles[name] = (shipped, raw)
            profile_hashes[name] = _canonical_hash(raw)

        registry_hash = _canonical_hash(
            {"policies": _canonical_hash(policy_raw), "profiles": profile_hashes}
        )
        loaded: dict[str, LoadedProfile] = {}
        aliases: dict[str, RegistryModel] = {}
        adapter_models: set[tuple[str, str]] = set()
        for expected_name, (path, raw) in raw_profiles.items():
            profile, models = cls._validate_profile(
                path,
                raw,
                expected_name=expected_name,
                adapters=adapters,
                catalog_entries={str(key): value for key, value in entries.items()},
            )
            version = f"{profile.profile}@{profile_hashes[expected_name][:12]}+r{registry_hash[:8]}"
            loaded[expected_name] = LoadedProfile(
                document=profile,
                profile_hash=profile_hashes[expected_name],
                registry_version=version,
                models=tuple(models),
            )
            for model in models:
                adapter_model = (profile.adapter, model.model_id)
                if adapter_model in adapter_models:
                    _fail(
                        path,
                        "models[].id",
                        "adapter and model id pair must be globally unique",
                    )
                adapter_models.add(adapter_model)
                for lookup_name in (model.model_id, *model.aliases):
                    if lookup_name in aliases:
                        _fail(
                            path,
                            "models[].aliases",
                            f"model id or alias {lookup_name!r} is not globally unique",
                        )
                    aliases[lookup_name] = model
        return cls(policies=policies, profiles=loaded, aliases=aliases)

    @classmethod
    def _validate_profile(
        cls,
        path: Path,
        raw: dict[str, Any],
        *,
        expected_name: str,
        adapters: dict[str, AdapterDefinition],
        catalog_entries: dict[str, Any],
    ) -> tuple[ProviderProfile, list[RegistryModel]]:
        if raw.get("schema_version") != 1:
            _fail(path, "schema_version", "must equal 1")
        name = raw.get("profile")
        if not isinstance(name, str) or PROFILE_NAME.fullmatch(name) is None:
            _fail(path, "profile", "must match the provider-profile name grammar")
        if name != expected_name or name != path.stem:
            _fail(path, "profile", "must equal the file stem")
        adapter_name = raw.get("adapter")
        if not isinstance(adapter_name, str) or adapter_name not in adapters:
            _fail(path, "adapter", "must name a registered adapter class")
        api = raw.get("api")
        if api not in {"responses", "messages", "chat_completions"}:
            _fail(path, "api", "must be responses, messages, or chat_completions")
        if api not in adapters[adapter_name].apis:
            _fail(path, "api", "is not permitted by the selected adapter")
        base_url = raw.get("base_url")
        if not isinstance(base_url, str):
            _fail(path, "base_url", "must be a URL string")
        _validate_url(path, base_url)
        if "credential_ref" not in raw:
            _fail(path, "credential_ref", "must be present as a name or null")
        credential_ref = raw["credential_ref"]
        if credential_ref is not None and (
            not isinstance(credential_ref, str) or CREDENTIAL_NAME.fullmatch(credential_ref) is None
        ):
            _fail(path, "credential_ref", "must be an environment-variable name or null")
        capabilities_raw = raw.get("capabilities")
        if not isinstance(capabilities_raw, dict) or set(capabilities_raw) != CAPABILITY_FIELDS:
            _fail(path, "capabilities", "all ten fields must be present with no extras")
        limits_raw = raw.get("limits")
        if not isinstance(limits_raw, dict) or set(limits_raw) != PROFILE_LIMIT_FIELDS:
            _fail(path, "limits", "all three fields must be present with no extras")
        try:
            profile = ProviderProfile.model_validate(raw)
        except ValidationError as exc:
            raise ProfileValidationError(f"{path}: any level: unknown or invalid key") from exc
        ceiling = adapters[adapter_name].ceiling
        for field_name in CAPABILITY_FIELDS - {"reasoning"}:
            if bool(getattr(profile.capabilities, field_name)) and not bool(
                getattr(ceiling, field_name)
            ):
                _fail(path, f"capabilities.{field_name}", "exceeds the adapter capability ceiling")
        if (
            profile.capabilities.reasoning is not ReasoningSupport.NONE
            and ceiling.reasoning is ReasoningSupport.NONE
        ):
            _fail(path, "capabilities.reasoning", "exceeds the adapter capability ceiling")
        has_in_band = profile.in_band_reasoning is not None
        needs_in_band = profile.capabilities.reasoning is ReasoningSupport.IN_BAND
        if has_in_band != needs_in_band:
            _fail(path, "in_band_reasoning", "must be present if and only if reasoning is in_band")

        models: list[RegistryModel] = []
        seen_ids: set[str] = set()
        for entry in profile.models:
            if entry.id in seen_ids:
                _fail(path, "models[].id", "must be unique within a profile")
            seen_ids.add(entry.id)
            if (entry.catalog is None) == (entry.pricing is None):
                _fail(path, "models[]", "must declare pricing or catalog, but not both")
            if entry.catalog is not None:
                catalog_raw = catalog_entries.get(entry.catalog)
                if not isinstance(catalog_raw, dict):
                    _fail(path, "models[].catalog", "must resolve in catalog.yaml")
                model_limits_raw = catalog_raw.get("limits")
                pricing_raw = catalog_raw.get("pricing")
            else:
                model_limits_raw = entry.limits
                pricing_raw = entry.pricing
            if not isinstance(model_limits_raw, dict) or set(model_limits_raw) != {
                "context_window_tokens",
                "max_output_tokens",
            }:
                _fail(path, "models[].limits", "must declare context and output limits")
            if not isinstance(pricing_raw, dict):
                _fail(path, "models[].pricing", "must be a mapping")
            try:
                limits = ModelLimits(
                    context_window_tokens=model_limits_raw["context_window_tokens"],
                    max_output_tokens=model_limits_raw["max_output_tokens"],
                    default_output_reserve=profile.limits.default_output_reserve,
                    max_cache_breakpoints=profile.limits.max_cache_breakpoints,
                    max_tool_count=profile.limits.max_tool_count,
                )
            except ValidationError as exc:
                raise ProfileValidationError(f"{path}: models[].limits: invalid value") from exc
            models.append(
                RegistryModel(
                    profile=profile.profile,
                    model_id=entry.id,
                    aliases=tuple(entry.aliases),
                    limits=limits,
                    pricing=_pricing(path, pricing_raw),
                )
            )
        return profile, models


class StaticModelRouter:
    """Resolve declared policies and refuse silent provider changes on resume."""

    def __init__(self, registry: ProviderRegistry, clock: Clock) -> None:
        self.registry = registry
        self._clock = clock

    async def resolve(
        self,
        model_policy: str,
        *,
        tenant_id: str,
        required: CapabilitySet | None = None,
    ) -> ResolvedModel:
        del tenant_id
        target = self.registry.policies.model_policies.get(model_policy)
        if target is None:
            raise ConfigurationError(f"model policy {model_policy!r} is not declared")
        profile = self.registry.profiles.get(target.provider)
        if profile is None:
            raise ConfigurationError(f"provider profile {target.provider!r} is not enabled")
        model = self._find_model(profile, target.model)
        capabilities = profile.document.capabilities
        if required is not None and not required.issubset(capabilities.enabled()):
            raise ConfigurationError(f"model policy {model_policy!r} lacks required capabilities")
        return ResolvedModel(
            provider=profile.document.adapter,
            model=model.model_id,
            capabilities=capabilities,
            limits=model.limits,
            pricing=model.pricing,
            credential_ref=profile.document.credential_ref or "none",
            policy_name=model_policy,
            resolved_at=self._clock.now(),
        )

    async def resolve_pinned(self, pin: ProviderPin) -> ResolvedModel:
        matching = [
            profile
            for profile in self.registry.profiles.values()
            if profile.document.adapter == pin.provider
            and profile.registry_version == pin.registry_version
        ]
        if len(matching) != 1:
            raise ConfigurationError("pinned provider registry version is unavailable")
        profile = matching[0]
        model = self._find_model(profile, pin.model)
        return ResolvedModel(
            provider=pin.provider,
            model=model.model_id,
            capabilities=profile.document.capabilities,
            limits=model.limits,
            pricing=model.pricing,
            credential_ref=profile.document.credential_ref or "none",
            policy_name="pinned",
            resolved_at=self._clock.now(),
        )

    @staticmethod
    def _find_model(profile: LoadedProfile, model_or_alias: str) -> RegistryModel:
        for model in profile.models:
            if model.model_id == model_or_alias or model_or_alias in model.aliases:
                return model
        raise ConfigurationError(
            f"model {model_or_alias!r} is not declared by profile {profile.document.profile!r}"
        )

    def pin(self, run_id: UUID, resolved: ResolvedModel) -> ProviderPin:
        matching = [
            profile
            for profile in self.registry.profiles.values()
            if profile.document.adapter == resolved.provider
            and any(model.model_id == resolved.model for model in profile.models)
        ]
        if len(matching) != 1:
            raise ConfigurationError("resolved model does not identify one enabled profile")
        return ProviderPin(
            run_id=run_id,
            provider=resolved.provider,
            model=resolved.model,
            registry_version=matching[0].registry_version,
            pinned_at=self._clock.now(),
        )
