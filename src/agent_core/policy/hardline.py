"""Frozen, non-substitutable hardline policy evaluation."""

from __future__ import annotations

import ipaddress
import posixpath
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from agent_core.domain.policies import HardlineRule, HardlineRuleKind, ProposedAction


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)


def _canonical_path(value: str) -> str:
    normalized = posixpath.normpath(value.replace("\\", "/"))
    if normalized.startswith("~/"):
        normalized = f"/home/{normalized.removeprefix('~/')}"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return posixpath.normpath(normalized)


def _is_within(candidate: str, protected: str) -> bool:
    normalized = _canonical_path(candidate)
    target = _canonical_path(protected)
    return normalized == target or normalized.startswith(f"{target}/")


def hardline_matches(rule: HardlineRule, action: ProposedAction) -> bool:
    if action.side_effect not in rule.applies_to:
        return False
    values = tuple(_strings(action.arguments))
    if rule.kind is HardlineRuleKind.SIDE_EFFECT:
        return True
    if rule.kind is HardlineRuleKind.COMMAND_REGEX:
        return any(re.search(rule.pattern or r"(?!x)x", value) is not None for value in values)
    if rule.kind is HardlineRuleKind.PROTECTED_PATH:
        for value in values:
            normalized = _canonical_path(value)
            components = tuple(part for part in normalized.split("/") if part)
            for protected in rule.paths:
                if protected == ".env":
                    basename = components[-1] if components else ""
                    if basename == ".env" or (
                        basename.startswith(".env.") and basename != ".env.example"
                    ):
                        return True
                if _is_within(normalized, protected):
                    return True
                if protected == "~/.ssh" and ".ssh" in components:
                    return True
        return False
    if rule.kind is HardlineRuleKind.NETWORK_RANGE:
        networks = tuple(ipaddress.ip_network(cidr) for cidr in rule.cidrs)
        for value in values:
            host = urlparse(value).hostname or value
            try:
                if any(ipaddress.ip_address(host) in network for network in networks):
                    return True
            except ValueError:
                continue
        return False
    if rule.kind is HardlineRuleKind.TRUST_FLOW:
        credential_shape = re.compile(
            r"(?:api[_-]?key|secret|password|token|bearer)\s*[:=]\s*\S+", re.I
        )
        return any(credential_shape.search(value) is not None for value in values)
    raise ValueError(f"unknown hardline rule kind {rule.kind!r}")
