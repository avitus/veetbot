"""Provider-neutral model gateway primitives."""

from agent_core.model.streaming import (
    ModelStreamAccumulator,
    ModelStreamError,
    collect_turn,
    validate_conversation_pairing,
    validated_stream,
)

__all__ = [
    "ModelStreamAccumulator",
    "ModelStreamError",
    "collect_turn",
    "validate_conversation_pairing",
    "validated_stream",
]
"""Provider-neutral model-layer constants and services."""

NON_ROUTED_MODEL_POLICIES = frozenset({"deterministic", "fake-balanced"})
