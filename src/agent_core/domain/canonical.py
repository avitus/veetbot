"""Shared canonical JSON normalization for tool and HTTP idempotency."""

from __future__ import annotations

import json
import math
import unicodedata

from agent_core.domain.errors import ToolValidationError


def canonical_value(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float):
        if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
            raise ToolValidationError("non-finite and negative-zero numbers are forbidden")
        return value
    if isinstance(value, list):
        return [canonical_value(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, child in sorted(value.items(), key=lambda item: str(item[0]).encode()):
            normalized_key = unicodedata.normalize("NFC", str(key))
            if normalized_key in normalized:
                raise ToolValidationError("JSON contains colliding normalized keys")
            normalized[normalized_key] = canonical_value(child)
        return normalized
    return value


def canonical_json(value: object) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
