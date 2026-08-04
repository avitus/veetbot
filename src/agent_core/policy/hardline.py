"""Frozen, non-substitutable hardline policy evaluation."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from agent_core.domain.policies import HardlineRule, ProposedAction


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


def hardline_matches(rule: HardlineRule, action: ProposedAction) -> bool:
    if action.side_effect not in rule.applies_to:
        return False
    values = tuple(_strings(action.arguments))
    if rule.kind == "side_effect":
        return True
    if rule.kind == "command_regex":
        return any(re.search(rule.pattern or r"(?!x)x", value) is not None for value in values)
    if rule.kind == "protected_path":
        for value in values:
            normalized = value.replace("\\", "/")
            for protected in rule.paths:
                expanded = protected.replace("~/", "/home/")
                if protected == ".env":
                    basename = normalized.rsplit("/", 1)[-1]
                    if basename == ".env" or (
                        basename.startswith(".env.") and basename != ".env.example"
                    ):
                        return True
                if normalized == protected or normalized.startswith(f"{protected}/"):
                    return True
                if expanded.startswith("/home/") and "/.ssh" in normalized:
                    return True
        return False
    if rule.kind == "network_range":
        networks = tuple(ipaddress.ip_network(cidr) for cidr in rule.cidrs)
        for value in values:
            host = urlparse(value).hostname or value
            try:
                if any(ipaddress.ip_address(host) in network for network in networks):
                    return True
            except ValueError:
                continue
        return False
    if rule.kind == "trust_flow":
        credential_shape = re.compile(
            r"(?:api[_-]?key|secret|password|token|bearer)\s*[:=]\s*\S+", re.I
        )
        return any(credential_shape.search(value) is not None for value in values)
    raise ValueError(f"unknown hardline rule kind {rule.kind!r}")
