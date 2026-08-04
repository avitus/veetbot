"""Strict, content-addressed policy ruleset loader."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from agent_core.domain.policies import (
    HardlineRule,
    LoadedRuleset,
    PolicyDecisionType,
    PolicyRule,
    RiskLevel,
    SideEffectClass,
)

_PROFILE_KEYS = {
    "schema_version",
    "name",
    "default_effect",
    "rules",
    "unknown_tool",
    "trust_overlay",
    "approval_expiry_seconds",
    "self_approval",
    "advisory",
}
_HARDLINE_KEYS = {"schema_version", "rules"}
_RULE_KEYS = {"decision", "condition", "otherwise"}


def _read_yaml(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise ValueError(f"policy document {path} must contain a mapping")
    return value, hashlib.sha256(raw).hexdigest()


def _require_exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    unknown = sorted(set(value) - expected)
    if unknown:
        raise ValueError(f"{location} contains unknown fields: {unknown}")


def load_ruleset(profile_path: Path, hardline_path: Path) -> LoadedRuleset:
    profile, profile_sha = _read_yaml(profile_path)
    hardline, hardline_sha = _read_yaml(hardline_path)
    _require_exact_keys(profile, _PROFILE_KEYS, "policy profile")
    _require_exact_keys(hardline, _HARDLINE_KEYS, "hardline policy")
    if profile.get("schema_version") != 1 or hardline.get("schema_version") != 1:
        raise ValueError("unsupported policy schema version")
    profile_rules = profile.get("rules")
    if not isinstance(profile_rules, dict):
        raise ValueError("policy profile rules must be a mapping")
    expected = {item.value for item in SideEffectClass}
    actual = set(profile_rules)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"policy matrix must be total; missing={missing}, extra={extra}")
    rules: list[PolicyRule] = []
    for effect in SideEffectClass:
        row = profile_rules[effect.value]
        if not isinstance(row, dict):
            raise ValueError(f"policy rule {effect.value} must be a mapping")
        _require_exact_keys(row, _RULE_KEYS, f"policy rule {effect.value}")
        if "decision" not in row:
            raise ValueError(f"policy rule {effect.value} requires a decision")
        rules.append(
            PolicyRule(
                side_effect=effect,
                decision=PolicyDecisionType(row["decision"]),
                condition=row.get("condition"),
                otherwise=(PolicyDecisionType(row["otherwise"]) if row.get("otherwise") else None),
            )
        )
    hardline_rows = hardline.get("rules")
    if not isinstance(hardline_rows, list):
        raise ValueError("hardline rules must be a list")
    hardline_rules = tuple(HardlineRule.model_validate(row) for row in hardline_rows)
    if len({rule.id for rule in hardline_rules}) != len(hardline_rules):
        raise ValueError("hardline rule ids must be unique")
    if any(not rule.near_miss.strip() for rule in hardline_rules):
        raise ValueError("every hardline rule requires a near_miss")
    expiry = profile.get("approval_expiry_seconds", {})
    if not isinstance(expiry, dict) or set(expiry) != {risk.value for risk in RiskLevel}:
        raise ValueError("approval expiry must define every risk level exactly once")
    expiry_values = tuple((risk, int(expiry[risk.value])) for risk in RiskLevel)
    if any(seconds <= 0 for _risk, seconds in expiry_values):
        raise ValueError("approval expiry values must be positive")
    trust_overlay = profile.get("trust_overlay")
    if not isinstance(trust_overlay, dict) or set(trust_overlay) != {
        "external_untrusted_requires_approval"
    }:
        raise ValueError("trust overlay has an invalid shape")
    external_requires_approval = trust_overlay["external_untrusted_requires_approval"]
    if not isinstance(external_requires_approval, bool):
        raise ValueError("external untrusted approval overlay must be boolean")
    unknown_tool = profile.get("unknown_tool")
    if not isinstance(unknown_tool, dict) or set(unknown_tool) != {"decision"}:
        raise ValueError("unknown-tool policy has an invalid shape")
    PolicyDecisionType(unknown_tool["decision"])
    for section in ("self_approval", "advisory"):
        value = profile.get(section)
        if not isinstance(value, dict) or set(value) != {"enabled"}:
            raise ValueError(f"{section} policy has an invalid shape")
        if not isinstance(value["enabled"], bool):
            raise ValueError(f"{section}.enabled must be boolean")
    name = str(profile["name"])
    if not name or name != profile["name"]:
        raise ValueError("policy profile name must be a non-empty string")
    return LoadedRuleset(
        policy_version=f"{name}@{profile_sha[:12]}+h{hardline_sha[:8]}",
        profile_name=name,
        profile_sha256=profile_sha,
        hardline_sha256=hardline_sha,
        rules=tuple(rules),
        hardline=hardline_rules,
        default_effect=PolicyDecisionType(profile.get("default_effect", "deny")),
        external_untrusted_requires_approval=external_requires_approval,
        approval_expiry_seconds=expiry_values,
    )


POLICY_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_RULESET = load_ruleset(
    POLICY_DIRECTORY / "default.yaml", POLICY_DIRECTORY / "hardline.yaml"
)
