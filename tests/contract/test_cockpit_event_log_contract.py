"""Cockpit event log contract, shared by the in-memory and PostgreSQL adapters."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_core.adapters.persistence.cockpit import InMemoryCockpitEventLog
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import CockpitEventKind
from agent_core.ports.cockpit import CockpitEventLog
from tests.contract.cockpit_support import Scenario, foreign, scenarios
from tests.contract.support import NOW, principal

SCENARIOS, scenario = scenarios()


async def _append(log: CockpitEventLog, owner: Principal, subject: str, minutes: int = 0) -> int:
    event = await log.append(
        owner,
        kind=CockpitEventKind.FEED_UPDATED,
        subject=subject,
        occurred_at=NOW + timedelta(minutes=minutes),
    )
    return event.sequence


@scenario
async def events_replay_in_order_after_a_sequence(log: CockpitEventLog, owner: Principal) -> None:
    assert await log.oldest_sequence(owner) is None
    assert await log.latest_sequence(owner) is None
    first = await _append(log, owner, "agent.weather")
    await _append(log, foreign(owner), "agent.weather")
    second = await _append(log, owner, "weather")
    third = await _append(log, owner, "agent.markets")
    assert first < second < third
    replayed = await log.after(owner, sequence=first, limit=10)
    assert [(item.sequence, item.subject) for item in replayed] == [
        (second, "weather"),
        (third, "agent.markets"),
    ]
    assert [item.sequence for item in await log.after(owner, sequence=0, limit=2)] == [
        first,
        second,
    ]
    assert await log.after(owner, sequence=third, limit=10) == []
    assert await log.oldest_sequence(owner) == first
    assert await log.latest_sequence(owner) == third
    assert all(item.principal_id == owner.principal_id for item in replayed)


@scenario
async def retention_moves_the_oldest_resumable_sequence(
    log: CockpitEventLog, owner: Principal
) -> None:
    await _append(log, owner, "a", minutes=0)
    kept = await _append(log, owner, "b", minutes=10)
    assert await log.prune_before(owner.tenant_id, before=NOW + timedelta(minutes=5)) >= 1
    assert await log.oldest_sequence(owner) == kept


@scenario
async def erasure_removes_only_the_owners_events(log: CockpitEventLog, owner: Principal) -> None:
    await _append(log, owner, "a")
    other = await _append(log, foreign(owner), "b")
    assert await log.erase_principal(owner) == 1
    assert await log.latest_sequence(owner) is None
    assert await log.latest_sequence(foreign(owner)) == other


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda check: check.__name__)
async def test_in_memory_cockpit_event_log(check: Scenario) -> None:
    await check(InMemoryCockpitEventLog(), principal())
