"""The shipped hardline rules block every listed shape and nothing beside it.

policy-and-approvals.md, "The hardline layer": writes to `/etc`, `/usr`,
`~/.ssh`, `.git/config`, and `.env` and its variants are refused; egress to
link-local metadata addresses is refused. `test_policy_m4.py::test_hardline_immutable`
checks one target and one near miss per rule; these cases cover the variants
the rule text names.
"""

from __future__ import annotations

import pytest

from agent_core.domain.policies import HardlineRule, SideEffectClass
from agent_core.policy.hardline import hardline_matches
from agent_core.policy.loader import DEFAULT_RULESET
from tests.gates.test_policy_m4 import action


def _rule(rule_id: str) -> HardlineRule:
    return next(rule for rule in DEFAULT_RULESET.hardline if rule.id == rule_id)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "./.env",
        "app/.env",
        ".env.production",
        "services/api/.env.local",
        ".git/config",
        "./.git/config",
        "~/.ssh/authorized_keys",
        "/usr/local/bin/tool",
        "/etc",
    ],
)
def test_a_write_to_a_protected_path_is_hardlined(path: str) -> None:
    assert hardline_matches(
        _rule("protected_host_path"),
        action(SideEffectClass.WORKSPACE_WRITE, {"path": path}),
    )


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "config/.env.example",
        "docs/environment.md",
        "src/usr/readme.md",
        "workspace/etc/settings.yaml",
        "notes/ssh.md",
    ],
)
def test_a_workspace_path_that_only_resembles_a_protected_one_is_not(path: str) -> None:
    assert not hardline_matches(
        _rule("protected_host_path"),
        action(SideEffectClass.WORKSPACE_WRITE, {"path": path}),
    )


def test_the_protected_path_rule_governs_only_its_declared_side_effects() -> None:
    rule = _rule("protected_host_path")

    assert not hardline_matches(rule, action(SideEffectClass.WORKSPACE_READ, {"path": ".env"}))
    assert hardline_matches(rule, action(SideEffectClass.CODE_EXECUTION, {"path": ".env"}))


@pytest.mark.parametrize(
    ("target", "blocked"),
    [
        ("http://169.254.169.254/latest/meta-data", True),
        ("169.254.170.2", True),
        ("http://[fe80::1]/", True),
        ("https://10.0.0.1/", False),
        ("https://example.com/", False),
    ],
)
def test_metadata_egress_is_hardlined_by_address_range(target: str, blocked: bool) -> None:
    rule = _rule("metadata_egress")

    assert hardline_matches(rule, action(SideEffectClass.NETWORK_READ, {"url": target})) is blocked
    assert (
        hardline_matches(rule, action(SideEffectClass.SANDBOX_NETWORK, {"url": target})) is blocked
    )
