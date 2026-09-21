"""The loaded ruleset carries the advisory enforce switch without changing the shipped hash."""

from __future__ import annotations

from typing import Any

import yaml

from agent_core.config import shipped_policy_version
from agent_core.policy.loader import DEFAULT_RULESET, POLICY_DIRECTORY, load_ruleset_documents

# The bundled release evidence is bound to this version; the advisory layer must not move it.
SHIPPED_POLICY_VERSION = "default@19be675c1b3c+hb03f69cf"


def _documents(**advisory: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = yaml.safe_load((POLICY_DIRECTORY / "default.yaml").read_text(encoding="utf-8"))
    hardline = yaml.safe_load((POLICY_DIRECTORY / "hardline.yaml").read_text(encoding="utf-8"))
    profile["advisory"] = {**profile["advisory"], **advisory}
    return profile, hardline


def test_the_shipped_profile_keeps_the_advisory_layer_off_and_its_version_unmoved() -> None:
    assert DEFAULT_RULESET.advisory_enabled is False
    # The evidence binds to the canonical-document hash, not the raw-file one.
    assert shipped_policy_version() == SHIPPED_POLICY_VERSION
    assert load_ruleset_documents(*_documents()).policy_version == SHIPPED_POLICY_VERSION


def test_an_enabled_profile_loads_as_enforcing_and_changes_the_policy_version() -> None:
    ruleset = load_ruleset_documents(*_documents(enabled=True))

    assert ruleset.advisory_enabled is True
    # Enforcing changes decisions, so it must be visible in the audit trail.
    assert ruleset.policy_version != SHIPPED_POLICY_VERSION
