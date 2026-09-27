"""Provider-neutral cache identity chosen by context assembly."""

import hashlib
import json
from uuid import UUID


def session_cache_key(tenant_id: str, session_id: UUID) -> str:
    """Keep routing/accounting stable across runs and epochs without raw identifiers."""
    identity = json.dumps(
        [tenant_id, str(session_id)], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "session-" + hashlib.sha256(identity).hexdigest()[:32]
