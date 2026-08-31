"""Deterministic weighted routing across provider-neutral web adapters."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_core.ports.web import WebProvider


def _stable_bucket(routing_key: str) -> int:
    return int.from_bytes(hashlib.sha256(routing_key.encode("utf-8")).digest()[:8], "big")


@dataclass(frozen=True, slots=True)
class WeightedWebProviderAllocation:
    provider: WebProvider
    weight: int


class WeightedWebProviderRouter:
    """Select one provider deterministically and keep retries on that provider."""

    def __init__(
        self,
        allocations: Sequence[tuple[WebProvider, int]],
        *,
        bucket_for_key: Callable[[str], int] = _stable_bucket,
    ) -> None:
        normalized = tuple(
            WeightedWebProviderAllocation(provider=provider, weight=weight)
            for provider, weight in allocations
        )
        if not normalized or any(allocation.weight <= 0 for allocation in normalized):
            raise ValueError("web-provider allocations require positive weights")
        provider_names = [allocation.provider.name for allocation in normalized]
        if len(set(provider_names)) != len(provider_names):
            raise ValueError("web-provider allocations must not contain duplicates")
        self.allocations = normalized
        self._total_weight = sum(allocation.weight for allocation in normalized)
        self._bucket_for_key = bucket_for_key

    def select(self, *, routing_key: str) -> WebProvider:
        bucket = self._bucket_for_key(routing_key) % self._total_weight
        upper_bound = 0
        for allocation in self.allocations:
            upper_bound += allocation.weight
            if bucket < upper_bound:
                return allocation.provider
        raise AssertionError("weighted web-provider bucket escaped its allocation")
