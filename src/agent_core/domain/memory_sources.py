"""Governed owner-source excerpts; admission never changes source trust."""

import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.policies import TrustLevel

_CREDENTIAL_CONTEXT = re.compile(
    r"\b(?:password|passphrase|credential|(?:access|verification|one[\s-]time|recovery)\s+code|"
    r"private\s+key|api[\s_-]*key|bearer|(?:access|auth(?:entication|orization)?|refresh)\s+token)\b",
    re.I,
)


def recovery_source_id(principal: Principal, session_id: UUID, sequence: int) -> UUID:
    """Opaque generated-copy lineage, distinct from source or People authority."""
    return uuid5(
        NAMESPACE_URL,
        json.dumps(
            [
                "memory-source-copy@1",
                principal.tenant_id,
                principal.principal_id,
                str(session_id),
                sequence,
            ]
        ),
    )


class OwnerSourceExcerpt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: UUID
    event_sequence: int = Field(gt=0)
    created_at: datetime
    text: str = Field(min_length=1, max_length=8192)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sensitivity: Literal["restricted"] = "restricted"


class RecalledSource(OwnerSourceExcerpt):
    copy_id: UUID
    people_source_id: UUID


class SourceRecoveryObservation(BaseModel):
    """Transient provenance of an admitted read, without its source prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    belief_id: UUID
    session_id: UUID
    event_sequence: int = Field(gt=0)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def source_admission_digest(event: NewEvent | EventEnvelope, owner: str) -> str | None:
    """Conservatively classify a bounded native owner text as restricted.

    This decision is stamped only at ingestion. Calling it on a legacy event
    does not grant admission: the repository also requires its stored stamp.
    """
    content = event.payload.get("content")
    if (
        event.event_type != "user.message.created"
        or event.actor_type != "principal"
        or event.actor_id != owner
        or event.payload.get("origin") is not None
        or event.payload.get("trust", TrustLevel.USER.value) != TrustLevel.USER.value
        or not isinstance(content, str)
        or not content.strip()
        or len(content) > 8192
        or contains_secret_material(content)
        or contains_injection_pattern(content)
        or _CREDENTIAL_CONTEXT.search(content)
    ):
        return None
    try:
        raw = content.encode()
    except UnicodeError:
        return None
    return hashlib.sha256(raw).hexdigest() if len(raw) <= 8192 else None


def admitted_excerpt(
    event: EventEnvelope, owner: str, digest: str | None
) -> OwnerSourceExcerpt | None:
    if digest is None or source_admission_digest(event, owner) != digest:
        return None
    return OwnerSourceExcerpt(
        session_id=event.session_id,
        event_sequence=event.sequence,
        created_at=event.created_at,
        text=event.payload["content"],
        text_sha256=digest,
    )
