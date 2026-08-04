"""Dependency-free egress evaluator shared with the isolated proxy image."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)
_DENIED_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def validate_host_and_ports(host: str, ports: frozenset[int]) -> None:
    normalized = host.lower().rstrip(".")
    candidate = normalized[2:] if normalized.startswith("*.") else normalized
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise ValueError("egress ports must be explicit values from 1 through 65535")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ValueError("egress destinations must be DNS names, not IP addresses")
    if not candidate or any(_HOST_LABEL.fullmatch(label) is None for label in candidate.split(".")):
        raise ValueError("egress destination has an invalid DNS name")
    if "*" in candidate or ("*" in normalized and not normalized.startswith("*.")):
        raise ValueError("egress wildcard may replace exactly one leftmost label")


def host_matches(pattern: str, host: str) -> bool:
    pattern = pattern.lower().rstrip(".")
    host = host.lower().rstrip(".")
    if not pattern.startswith("*."):
        return pattern == host
    suffix_labels = pattern[2:].split(".")
    host_labels = host.split(".")
    return len(host_labels) == len(suffix_labels) + 1 and host_labels[1:] == suffix_labels


def address_is_public(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not any(
        address in network for network in _DENIED_NETWORKS if address.version == network.version
    )


def evaluate_core(
    mode: str,
    destinations: Sequence[tuple[str, frozenset[int]]],
    host: str,
    port: int,
    resolved_addresses: tuple[str, ...],
) -> tuple[bool, str]:
    if mode == "deny":
        return False, "mode_deny"
    matching = tuple(item for item in destinations if host_matches(item[0], host))
    if not matching:
        return False, "destination_miss"
    if not any(port in item[1] for item in matching):
        return False, "port_miss"
    if not resolved_addresses or any(not address_is_public(value) for value in resolved_addresses):
        return False, "private_address"
    return True, "allowed"
