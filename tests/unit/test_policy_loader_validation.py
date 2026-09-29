"""The policy loader refuses every malformed profile or hardline document.

policy-and-approvals.md: the loader validates the rules before exposing them,
and a malformed policy must stop startup rather than load a partial or
permissive ruleset. Each case below breaks one shipped document in one place;
`test_policy_m4.py::test_totality` covers matrix totality, unknown fields, a
misspelled condition, non-integer expiry and an unknown-tool allow.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_core.policy.loader import POLICY_DIRECTORY, load_ruleset, load_ruleset_documents

type Documents = tuple[dict[str, Any], dict[str, Any]]


def _shipped() -> Documents:
    profile = yaml.safe_load((POLICY_DIRECTORY / "default.yaml").read_bytes())
    hardline = yaml.safe_load((POLICY_DIRECTORY / "hardline.yaml").read_bytes())
    return copy.deepcopy(profile), copy.deepcopy(hardline)


def _set(document: str, path: tuple[str | int, ...], value: Any) -> Callable[[Documents], None]:
    def mutate(documents: Documents) -> None:
        target: Any = documents[0] if document == "profile" else documents[1]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _drop(document: str, path: tuple[str | int, ...]) -> Callable[[Documents], None]:
    def mutate(documents: Documents) -> None:
        target: Any = documents[0] if document == "profile" else documents[1]
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _duplicate_hardline_id(documents: Documents) -> None:
    rules = documents[1]["rules"]
    rules.append(copy.deepcopy(rules[0]))


MALFORMED = [
    pytest.param(_set("profile", ("schema_version",), 2), "schema version", id="profile_schema"),
    pytest.param(_set("hardline", ("schema_version",), 2), "schema version", id="hardline_schema"),
    pytest.param(_set("hardline", ("extra",), True), "unknown fields", id="hardline_unknown_key"),
    pytest.param(
        _set("profile", ("rules",), []), "rules must be a mapping", id="rules_not_mapping"
    ),
    pytest.param(
        _set("profile", ("rules", "external_write"), "require_approval"),
        "policy rule external_write must be a mapping",
        id="rule_row_not_mapping",
    ),
    pytest.param(
        _drop("profile", ("rules", "external_write", "decision")),
        "external_write requires a decision",
        id="rule_without_decision",
    ),
    pytest.param(
        _set("profile", ("rules", "external_write", "decision"), "maybe"),
        "maybe",
        id="rule_unknown_decision",
    ),
    pytest.param(
        _set("hardline", ("rules",), {"id": "x"}), "must be a list", id="hardline_not_list"
    ),
    pytest.param(_duplicate_hardline_id, "ids must be unique", id="hardline_duplicate_id"),
    pytest.param(
        _set("hardline", ("rules", 0, "near_miss"), "  "),
        "near_miss",
        id="hardline_blank_near_miss",
    ),
    pytest.param(
        _drop("profile", ("approval_expiry_seconds", "critical")),
        "every risk level",
        id="expiry_missing_risk",
    ),
    pytest.param(
        _set("profile", ("approval_expiry_seconds", "unknown"), 60),
        "every risk level",
        id="expiry_extra_risk",
    ),
    pytest.param(
        _set("profile", ("approval_expiry_seconds", "critical"), 0),
        "must be positive",
        id="expiry_zero",
    ),
    pytest.param(
        _set("profile", ("approval_expiry_seconds", "low"), -60),
        "must be positive",
        id="expiry_negative",
    ),
    pytest.param(
        _set("profile", ("trust_overlay",), {}), "trust overlay", id="trust_overlay_empty"
    ),
    pytest.param(
        _set("profile", ("trust_overlay", "external_untrusted_requires_approval"), "yes"),
        "must be boolean",
        id="trust_overlay_not_boolean",
    ),
    pytest.param(
        _set("profile", ("unknown_tool",), {"decision": "deny", "why": "x"}),
        "unknown-tool policy has an invalid shape",
        id="unknown_tool_extra_key",
    ),
    pytest.param(
        _set("profile", ("self_approval",), {"enabled": "false"}),
        "self_approval.enabled must be boolean",
        id="self_approval_not_boolean",
    ),
    pytest.param(
        _set("profile", ("advisory",), {"enabled": False, "extra": 1}),
        "advisory policy has an invalid shape",
        id="advisory_extra_key",
    ),
    pytest.param(_set("profile", ("name",), ""), "non-empty string", id="empty_name"),
    pytest.param(_set("profile", ("name",), 7), "non-empty string", id="non_string_name"),
]


@pytest.mark.parametrize(("mutate", "message"), MALFORMED)
def test_a_malformed_policy_document_is_refused(
    mutate: Callable[[Documents], None], message: str
) -> None:
    documents = _shipped()
    load_ruleset_documents(*documents)
    mutate(documents)

    with pytest.raises(ValueError, match=message):
        load_ruleset_documents(*documents)


def test_a_policy_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    listing = tmp_path / "profile.yaml"
    listing.write_text("- allow everything\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must contain a mapping"):
        load_ruleset(listing, POLICY_DIRECTORY / "hardline.yaml")


def test_the_policy_version_changes_with_either_document() -> None:
    profile, hardline = _shipped()
    baseline = load_ruleset_documents(profile, hardline).policy_version

    stricter_profile = copy.deepcopy(profile)
    stricter_profile["approval_expiry_seconds"]["critical"] = 1800
    varied_hardline = copy.deepcopy(hardline)
    varied_hardline["rules"][0]["near_miss"] = "rm -rf ./dist"

    assert load_ruleset_documents(profile, hardline).policy_version == baseline
    assert baseline.startswith(f"{profile['name']}@")
    assert load_ruleset_documents(stricter_profile, hardline).policy_version != baseline
    assert load_ruleset_documents(profile, varied_hardline).policy_version != baseline
