import asyncio
from datetime import timedelta

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.errors import RunCancelledError
from agent_core.domain.runs import CancelReason
from agent_core.runtime.cancellation import RunCancellationToken
from tests.contract.support import NOW


async def test_cancellation_token_is_one_shot_and_deadline_is_lazy() -> None:
    clock = FixedClock(NOW)
    token = RunCancellationToken(clock, NOW + timedelta(seconds=1))
    assert token.reason is None
    clock.advance(timedelta(seconds=1))
    assert token.reason is CancelReason.DEADLINE
    with pytest.raises(RunCancelledError):
        token.raise_if_cancelled()
    assert await token.wait() is CancelReason.DEADLINE


async def test_a_waiter_wakes_with_the_first_reason_and_later_reasons_do_not_replace_it() -> None:
    clock = FixedClock(NOW)
    token = RunCancellationToken(clock, NOW + timedelta(seconds=1))
    waiter = asyncio.create_task(token.wait())
    await asyncio.sleep(0)
    assert not waiter.done()

    token.cancel(CancelReason.FENCED)
    token.cancel(CancelReason.REQUESTED)
    clock.advance(timedelta(seconds=5))

    assert await asyncio.wait_for(waiter, timeout=1) is CancelReason.FENCED
    assert token.reason is CancelReason.FENCED
    with pytest.raises(RunCancelledError, match=CancelReason.FENCED.value):
        token.raise_if_cancelled()


async def test_a_token_without_a_deadline_never_cancels_itself() -> None:
    clock = FixedClock(NOW)
    token = RunCancellationToken(clock, None)
    clock.advance(timedelta(days=3650))

    assert token.reason is None
    token.raise_if_cancelled()
