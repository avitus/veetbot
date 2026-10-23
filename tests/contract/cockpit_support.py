"""Shared factories for the Cockpit store contract suites (Milestone 35).

Each suite is a list of scenarios, `async def scenario(store, owner)`. The
contract modules run them on the in-memory adapters; the PostgreSQL parity
test runs the same lists inside a unit of work with a fresh owner, so both
adapters pass one suite.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    RevisionSource,
    RevisionState,
    Widget,
    WidgetRevision,
    WidgetState,
)
from tests.contract.support import NOW

type Scenario = Callable[[Any, Principal], Awaitable[None]]

BUNDLE = "(()=>{globalThis.VeetbotRealm.register(()=>null)})();"
BUNDLE_SHA256 = hashlib.sha256(BUNDLE.encode()).hexdigest()
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
FILES = {
    "widget.json": "{}",
    "widget.tsx": "export default function W() { return null; }",
    "fixtures/typical.json": "{}",
    "fixtures/empty.json": "{}",
    "fixtures/stale.json": "{}",
}


def scenarios() -> tuple[list[Scenario], Callable[[Scenario], Scenario]]:
    registered: list[Scenario] = []

    def scenario(check: Scenario) -> Scenario:
        registered.append(check)
        return check

    return registered, scenario


def foreign(owner: Principal) -> Principal:
    return owner.model_copy(update={"principal_id": f"{owner.principal_id}-other"})


def widget(owner: Principal, slug: str = "weather") -> Widget:
    return Widget(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        slug=slug,
        state=WidgetState.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )


def revision(
    owner: Principal,
    slug: str = "weather",
    *,
    revision_id: UUID | None = None,
    state: RevisionState = RevisionState.VALIDATING,
    at_minutes: int = 0,
) -> WidgetRevision:
    built = state not in {RevisionState.VALIDATING, RevisionState.REJECTED}
    return WidgetRevision(
        id=revision_id or uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        slug=slug,
        source=RevisionSource.ASSISTANT,
        state=state,
        files=FILES,
        bundle=BUNDLE if built else None,
        bundle_sha256=BUNDLE_SHA256 if built else None,
        kit_api=1 if built else None,
        commit_sha=COMMIT_A
        if state in {RevisionState.COMMITTED, RevisionState.LIVE, RevisionState.SUPERSEDED}
        else None,
        created_at=NOW + timedelta(minutes=at_minutes),
        updated_at=NOW + timedelta(minutes=at_minutes),
    )
