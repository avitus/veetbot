"""Lossless equality of current recall presentations, apart from observation time."""

import hashlib
import re

from agent_core.domain.memory import RecallQuery


def current_evidence_digest(rendered: str, query: RecallQuery) -> str | None:
    """Historical timestamps are evidence; only current observation time is ignored."""
    if query.as_of is not None or query.known_at is not None:
        return None
    normalized, count = re.subn(r'^<memory as_of="[^"]+" ', "<memory ", rendered, count=1)
    if count != 1:
        return None
    return hashlib.sha256(normalized.encode()).hexdigest()
