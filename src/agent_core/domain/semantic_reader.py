"""Content-free identity of the reader configuration actually used by a run."""

import hashlib
import json

from agent_core.domain.messages import ReasoningEffort, ResolvedModel


def reader_identity(
    model: ResolvedModel,
    *,
    reasoning_effort: ReasoningEffort | None,
    reasoning_summary: bool,
    registry_version: str | None,
) -> str:
    identity = {
        "version": "recall-reader@1",
        "model": model.model_dump(mode="json", exclude={"resolved_at", "credential_ref"}),
        "registry_version": registry_version,
        "reasoning_effort": reasoning_effort,
        "reasoning_summary": reasoning_summary,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
