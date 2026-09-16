"""Deterministic People source keys and complete identifier matching."""

import hashlib
import json
import re
import unicodedata
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.email_semantics import EmailSemanticSource


def source_id(principal: Principal, session_id: UUID, sequence: int) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        json.dumps(
            [
                "people-source@1",
                principal.tenant_id,
                principal.principal_id,
                str(session_id),
                sequence,
            ]
        ),
    )


def email_source_id(principal: Principal, email: EmailSemanticSource) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        json.dumps(
            [
                "people-email-source@1",
                principal.tenant_id,
                principal.principal_id,
                email.account_id,
                email.provider_thread_id,
                email.message_id,
                email.body_offset,
                hashlib.sha256(email.body.encode()).hexdigest(),
            ]
        ),
    )


def identifier_occurs(text: str, value: str, kind: str = "name") -> bool:
    """Match a complete identifier, never a substring of another person's name or address."""
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    boundary = r"[\w.+@-]" if kind in {"email", "handle"} else r"\w"
    return bool(
        value and re.search(rf"(?<!{boundary}){re.escape(value)}(?!{boundary})", normalized)
    )
