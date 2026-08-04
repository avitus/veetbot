"""Conservative, replaceable token estimation with stable reconciliation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence

from agent_core.domain.messages import ConversationItem
from agent_core.domain.tools import ToolSpec


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class ConservativeTokenEstimator:
    """Estimate high, while keeping repeated estimates byte-for-byte stable."""

    def __init__(self) -> None:
        self._correction: dict[str, float] = {}
        self._observations: dict[str, tuple[int, int]] = {}
        self._memo: dict[tuple[str, str, str], int] = {}

    @staticmethod
    def _base(payload: bytes, item_count: int) -> int:
        if item_count == 0:
            return 0
        # Three UTF-8 bytes per token is conservative for ordinary English and
        # code. Per-item framing accounts for provider message separators.
        return max(1, math.ceil(len(payload) / 3) + (4 * item_count))

    def _estimate(self, kind: str, payload: bytes, item_count: int, model_id: str) -> int:
        digest = hashlib.sha256(payload).hexdigest()
        key = (kind, model_id, digest)
        base = self._memo.get(key)
        if base is None:
            base = self._base(payload, item_count)
            self._memo[key] = base
        factor = max(1.0, self._correction.get(model_id, 1.0))
        return math.ceil(base * factor)

    def estimate(self, items: Sequence[ConversationItem], model_id: str) -> int:
        payload = canonical_json_bytes([item.model_dump(mode="json") for item in items])
        return self._estimate("items", payload, len(items), model_id)

    def estimate_tools(self, tools: Sequence[ToolSpec], model_id: str) -> int:
        payload = canonical_json_bytes([tool.model_dump(mode="json") for tool in tools])
        return self._estimate("tools", payload, len(tools), model_id)

    def estimate_text(self, text: str, model_id: str) -> int:
        return self._estimate("text", text.encode("utf-8"), 1, model_id)

    def reconcile(self, model_id: str, estimated: int, actual: int) -> None:
        if estimated <= 0 or actual < 0:
            raise ValueError("token reconciliation requires positive estimates and nonnegative use")
        previous_estimated, previous_actual = self._observations.get(model_id, (0, 0))
        total_estimated = previous_estimated + estimated
        total_actual = previous_actual + actual
        self._observations[model_id] = (total_estimated, total_actual)
        self._correction[model_id] = max(1.0, total_actual / total_estimated)

    def error_ratio(self, model_id: str) -> float | None:
        observed = self._observations.get(model_id)
        if observed is None or observed[1] == 0:
            return None
        estimated, actual = observed
        return (estimated - actual) / actual
