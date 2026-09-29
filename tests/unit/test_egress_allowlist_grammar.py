"""The egress allowlist grammar and its one evaluator (sandbox-isolation.md, egress boundary).

The sandbox proxy and the worker's outbound guard share this evaluator; these tests pin
the grammar rules and refusal reasons without a container, a proxy, or DNS.
"""

from __future__ import annotations

import pytest

from agent_core.domain.execution import EgressDestination, EgressMode, EgressPolicy
from agent_core.execution.egress import (
    EgressReason,
    evaluate_egress,
    validate_destination,
)
from agent_core.execution.egress_core import (
    address_is_public,
    host_matches,
    validate_host_and_ports,
)

PUBLIC = ("93.184.216.34",)


def _allowlist(*destinations: tuple[str, frozenset[int]]) -> EgressPolicy:
    return EgressPolicy(
        mode=EgressMode.ALLOWLIST,
        destinations=tuple(EgressDestination(host, ports) for host, ports in destinations),
    )


@pytest.mark.parametrize(
    ("pattern", "host", "matches"),
    [
        ("*.example.com", "a.example.com", True),
        ("*.example.com", "A.Example.COM.", True),
        # A wildcard is one leftmost label and nothing else (rule 2).
        ("*.example.com", "example.com", False),
        ("*.example.com", "a.b.example.com", False),
        ("*.example.com", "evilexample.com", False),
        ("*.example.com", "a.evilexample.com", False),
        ("*.example.com", "a.example.com.evil.net", False),
        ("api.example.com", "api.example.com", True),
        ("api.example.com", "API.example.com.", True),
        ("api.example.com", "x.api.example.com", False),
        ("api.example.com", "example.com", False),
    ],
)
def test_wildcards_match_exactly_one_leftmost_label(pattern: str, host: str, matches: bool) -> None:
    assert host_matches(pattern, host) is matches


@pytest.mark.parametrize(
    ("host", "ports", "message"),
    [
        # No IP-address destination form (rule 5), including IPv6 and a wildcard over one.
        ("93.184.216.34", frozenset({443}), "not IP addresses"),
        ("169.254.169.254", frozenset({80}), "not IP addresses"),
        ("::1", frozenset({443}), "not IP addresses"),
        # Ports are required and explicit (rule 3).
        ("pypi.org", frozenset(), "ports must be explicit"),
        ("pypi.org", frozenset({0}), "ports must be explicit"),
        ("pypi.org", frozenset({443, 65536}), "ports must be explicit"),
        # A wildcard replaces exactly one leftmost label (rule 2).
        ("*", frozenset({443}), "invalid DNS name"),
        ("*.*.example.com", frozenset({443}), "invalid DNS name"),
        ("a.*.example.com", frozenset({443}), "invalid DNS name"),
        ("*example.com", frozenset({443}), "invalid DNS name"),
        # There is no scheme and no path in the grammar (rule 4).
        ("https://pypi.org", frozenset({443}), "invalid DNS name"),
        ("pypi.org/simple", frozenset({443}), "invalid DNS name"),
        ("", frozenset({443}), "invalid DNS name"),
        ("-bad.example.com", frozenset({443}), "invalid DNS name"),
    ],
)
def test_invalid_destinations_are_refused_at_configuration(
    host: str, ports: frozenset[int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_host_and_ports(host, ports)
    with pytest.raises(ValueError, match=message):
        validate_destination(EgressDestination(host, ports))


@pytest.mark.parametrize(
    ("host", "ports"),
    [
        ("pypi.org", frozenset({443})),
        ("*.pythonhosted.org", frozenset({443})),
        ("API.Example.com.", frozenset({1, 65535})),
    ],
)
def test_valid_destinations_are_accepted(host: str, ports: frozenset[int]) -> None:
    validate_destination(EgressDestination(host, ports))


def test_deny_mode_refuses_even_a_listed_destination() -> None:
    policy = EgressPolicy(
        mode=EgressMode.DENY,
        destinations=(EgressDestination("pypi.org", frozenset({443})),),
    )

    decision = evaluate_egress(policy, "pypi.org", 443, PUBLIC)

    assert decision.allowed is False
    assert decision.reason is EgressReason.MODE_DENY


def test_the_default_policy_denies_everything() -> None:
    decision = evaluate_egress(EgressPolicy(), "pypi.org", 443, PUBLIC)

    assert (decision.allowed, decision.reason) == (False, EgressReason.MODE_DENY)


@pytest.mark.parametrize(
    ("host", "port", "addresses", "reason"),
    [
        ("pypi.org", 443, PUBLIC, EgressReason.ALLOWED),
        ("files.pythonhosted.org", 443, PUBLIC, EgressReason.ALLOWED),
        ("example.org", 443, PUBLIC, EgressReason.DESTINATION_MISS),
        ("pythonhosted.org", 443, PUBLIC, EgressReason.DESTINATION_MISS),
        ("evilpypi.org", 443, PUBLIC, EgressReason.DESTINATION_MISS),
        ("pypi.org", 8080, PUBLIC, EgressReason.PORT_MISS),
        ("pypi.org", 443, ("10.0.0.5",), EgressReason.PRIVATE_ADDRESS),
        # One bad address refuses the whole connection.
        ("pypi.org", 443, ("93.184.216.34", "192.168.1.1"), EgressReason.PRIVATE_ADDRESS),
        # IPv4-mapped IPv6 is unwrapped and checked as IPv4.
        ("pypi.org", 443, ("::ffff:169.254.169.254",), EgressReason.PRIVATE_ADDRESS),
        # A name that resolved to nothing is not allowed through.
        ("pypi.org", 443, (), EgressReason.PRIVATE_ADDRESS),
    ],
)
def test_allowlist_evaluation_reports_the_refusal_reason(
    host: str, port: int, addresses: tuple[str, ...], reason: EgressReason
) -> None:
    policy = _allowlist(
        ("pypi.org", frozenset({443})),
        ("*.pythonhosted.org", frozenset({443})),
    )

    decision = evaluate_egress(policy, host, port, addresses)

    assert decision.reason is reason
    assert decision.allowed is (reason is EgressReason.ALLOWED)


def test_a_port_on_one_matching_destination_is_not_borrowed_by_another() -> None:
    policy = _allowlist(
        ("api.example.com", frozenset({443})),
        ("*.example.com", frozenset({8443})),
    )

    assert evaluate_egress(policy, "api.example.com", 443, PUBLIC).allowed is True
    assert evaluate_egress(policy, "api.example.com", 8443, PUBLIC).allowed is True
    assert evaluate_egress(policy, "cdn.example.com", 8443, PUBLIC).allowed is True
    refused = evaluate_egress(policy, "cdn.example.com", 443, PUBLIC)
    assert refused.reason is EgressReason.PORT_MISS


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.1",
        "10.1.2.3",
        "127.0.0.1",
        "169.254.169.254",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.0.1",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "fd12:3456::1",
        "fe80::1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
    ],
)
def test_the_fixed_address_denylist_is_not_public(address: str) -> None:
    assert address_is_public(address) is False


@pytest.mark.parametrize(
    "address",
    ["93.184.216.34", "172.32.0.1", "100.128.0.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"],
)
def test_public_addresses_just_outside_the_denylist_are_public(address: str) -> None:
    assert address_is_public(address) is True
