"""One egress policy evaluator for sandbox proxy and worker outbound calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent_core.domain.execution import EgressDestination, EgressPolicy
from agent_core.execution.egress_core import (
    evaluate_core,
    validate_host_and_ports,
)


class EgressReason(StrEnum):
    ALLOWED = "allowed"
    MODE_DENY = "mode_deny"
    DESTINATION_MISS = "destination_miss"
    PORT_MISS = "port_miss"
    PRIVATE_ADDRESS = "private_address"


@dataclass(frozen=True, slots=True)
class EgressDecision:
    allowed: bool
    reason: EgressReason


def validate_destination(destination: EgressDestination) -> None:
    validate_host_and_ports(destination.host, destination.ports)


def evaluate_egress(
    policy: EgressPolicy,
    host: str,
    port: int,
    resolved_addresses: tuple[str, ...],
) -> EgressDecision:
    allowed, reason = evaluate_core(
        policy.mode.value,
        tuple((item.host, item.ports) for item in policy.destinations),
        host,
        port,
        resolved_addresses,
    )
    return EgressDecision(allowed, EgressReason(reason))
