from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_core.adapters.determinism import FixedClock, SystemClock
from tests.contract.support import NOW


async def test_clock_returns_aware_utc_and_sleep_is_observable() -> None:
    clock = SystemClock()
    system_now = clock.now()
    assert system_now.tzinfo is UTC
    await clock.sleep(0.01)
    assert clock.now() - system_now >= timedelta(seconds=0.01)


async def test_fixed_clock_sleep_advances_deterministically() -> None:
    clock = FixedClock(NOW)
    await clock.sleep(1.5)
    assert clock.now() == NOW + timedelta(seconds=1.5)


def test_fixed_clock_requires_an_aware_instant_and_reports_it_in_utc() -> None:
    with pytest.raises(ValueError, match="aware"):
        FixedClock(datetime(2026, 7, 25, 13, 0))

    offset = NOW.astimezone(timezone(timedelta(hours=-7)))
    clock = FixedClock(offset)

    assert clock.now() == NOW
    assert clock.now().tzinfo is UTC
