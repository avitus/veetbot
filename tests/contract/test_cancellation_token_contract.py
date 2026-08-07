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
