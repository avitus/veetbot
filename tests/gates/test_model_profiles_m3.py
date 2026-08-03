"""Strict provider-profile corpus and pinning gates."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.model.registry import ProfileValidationError, ProviderRegistry, StaticModelRouter
from tests.contract.support import NOW

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "src/agent_core/models"
CORPUS = ROOT / "tests/fixtures/models/invalid-profiles.yaml"


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def mutate(root: Path, name: str) -> None:
    openai_path = root / "providers/openai.yaml"
    profile = load_yaml(openai_path)
    model = profile["models"][0]
    pricing = model["pricing"]
    if name == "schema_version":
        profile["schema_version"] = 2
    elif name == "profile_grammar":
        profile["profile"] = "OpenAI!"
    elif name == "profile_file_stem":
        profile["profile"] = "renamed"
    elif name == "adapter_unknown":
        profile["adapter"] = "missing"
    elif name == "api_unknown":
        profile["api"] = "completions"
    elif name == "api_wrong_adapter":
        profile["api"] = "messages"
    elif name == "base_url_remote_http":
        profile["base_url"] = "http://models.example.test/v1"
    elif name == "credential_missing":
        profile.pop("credential_ref")
    elif name == "capability_missing":
        profile["capabilities"].pop("streaming")
    elif name == "capability_widened":
        profile["capabilities"]["explicit_cache_control"] = True
    elif name == "limit_missing":
        profile["limits"].pop("max_tool_count")
    elif name == "in_band_unexpected":
        profile["in_band_reasoning"] = {"open": "<r>", "close": "</r>"}
    elif name == "models_empty":
        profile["models"] = []
    elif name == "model_id_duplicate":
        profile["models"].append(copy.deepcopy(model))
    elif name == "catalog_missing":
        model.pop("limits")
        model.pop("pricing")
        model["catalog"] = "missing-entry"
    elif name == "pricing_and_catalog":
        model["catalog"] = "open-local-8b"
    elif name == "pricing_float":
        pricing["input_per_mtok"] = 5.0
    elif name == "pricing_none":
        pricing["input_per_mtok"] = None
    elif name == "effective_without_offset":
        pricing["effective_at"] = "2026-08-03T00:00:00"
    elif name == "unknown_key":
        profile["unknown"] = True
    elif name in {"duplicate_enabled", "alias_duplicate"}:
        pass
    else:
        raise AssertionError(f"unknown corpus mutation {name}")
    write_yaml(openai_path, profile)

    if name == "duplicate_enabled":
        policies_path = root / "policies.yaml"
        policies = load_yaml(policies_path)
        policies["enabled_profiles"].append("openai")
        write_yaml(policies_path, policies)
    elif name == "alias_duplicate":
        anthropic_path = root / "providers/anthropic.yaml"
        anthropic = load_yaml(anthropic_path)
        anthropic["models"][0]["aliases"].append("default")
        write_yaml(anthropic_path, anthropic)


def test_all_shipped_provider_profiles_load_as_one_total_registry() -> None:
    registry = ProviderRegistry.load(MODELS, adapters=ADAPTER_DEFINITIONS)
    assert set(registry.profiles) == {"openai", "anthropic", "ollama"}
    assert all("@" in profile.registry_version for profile in registry.profiles.values())
    assert all("+r" in profile.registry_version for profile in registry.profiles.values())


def test_every_invalid_profile_corpus_member_names_the_rule_it_broke(tmp_path: Path) -> None:
    members = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    assert isinstance(members, list)
    assert len(members) == 22
    for member in members:
        assert isinstance(member, dict)
        case_root = tmp_path / str(member["rule"])
        shutil.copytree(MODELS, case_root)
        mutate(case_root, str(member["mutation"]))
        with pytest.raises(ProfileValidationError) as captured:
            ProviderRegistry.load(case_root, adapters=ADAPTER_DEFINITIONS)
        assert str(member["expected"]) in str(captured.value)


def test_unquoted_yaml_dates_fail_as_profile_validation_not_hashing_type_errors(
    tmp_path: Path,
) -> None:
    case_root = tmp_path / "unquoted-date"
    shutil.copytree(MODELS, case_root)
    openai_path = case_root / "providers/openai.yaml"
    source = openai_path.read_text(encoding="utf-8")
    openai_path.write_text(
        source.replace('effective_at: "2026-08-03T00:00:00Z"', "effective_at: 2026-08-03"),
        encoding="utf-8",
    )
    with pytest.raises(ProfileValidationError, match="effective_at"):
        ProviderRegistry.load(case_root, adapters=ADAPTER_DEFINITIONS)


async def test_provider_pin_resolves_identically_after_router_reconstruction() -> None:
    registry = ProviderRegistry.load(MODELS, adapters=ADAPTER_DEFINITIONS)
    first = StaticModelRouter(registry, FixedClock(NOW))
    resolved = await first.resolve("balanced", tenant_id="tenant-a")
    pin = first.pin(UUID("00000000-0000-0000-0000-000000000399"), resolved)

    reconstructed = StaticModelRouter(
        ProviderRegistry.load(MODELS, adapters=ADAPTER_DEFINITIONS), FixedClock(NOW)
    )
    resumed = await reconstructed.resolve_pinned(pin)
    assert (resumed.provider, resumed.model) == (resolved.provider, resolved.model)
    assert pin.registry_version.startswith("openai@")
