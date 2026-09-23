"""The owner's own mail identities, which never name or match a person (ADR-0121)."""

from __future__ import annotations

from agent_core.domain.agents import Principal
from agent_core.ports.email import EmailStore


async def owner_references(email: EmailStore, principal: Principal) -> frozenset[str]:
    """The owner's own addresses and handles, from every mail account (ADR-0121).

    Account status is irrelevant: an address stays the owner's while its account
    syncs or is unavailable.
    """
    refs: set[str] = set()
    after: str | None = None
    for _ in range(10):
        rows = await email.list(principal, "account", after=after, limit=100)
        for row in rows:
            payload = row.payload
            verified = payload.get("verified_addresses")
            values: list[object] = [
                payload.get("email_address"),
                *(verified if isinstance(verified, list) else []),
            ]
            for value in values:
                if isinstance(value, str) and "@" in value:
                    address = value.strip().casefold()
                    refs.add("email:" + address)
                    refs.add("handle:" + address.split("@", 1)[0])
        if len(rows) < 100:
            break
        after = rows[-1].key
    return frozenset(refs)
