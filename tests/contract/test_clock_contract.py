from datetime import UTC, timedelta

from agent_core.adapters.determinism import FixedClock, SystemClock
from tests.contract.support import NOW


def test_clock_returns_aware_utc_and_sleep_is_observable() -> None:
    system_now = SystemClock().now()
    assert system_now.tzinfo is UTC


async def test_fixed_clock_sleep_advances_deterministically() -> None:
    clock = FixedClock(NOW)
    await clock.sleep(1.5)
    assert clock.now() == NOW + timedelta(seconds=1.5)
