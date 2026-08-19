from uuid import UUID

import pytest

from agent_core.adapters.dispatch.inline import InlineRunDispatcher
from tests.contract.support import RUN_ID


async def test_inline_dispatcher_executes_a_committed_run_exactly_once() -> None:
    executed: list[object] = []

    async def execute(run_id: UUID) -> None:
        executed.append(run_id)

    dispatcher = InlineRunDispatcher(execute, unit_of_work_open=lambda: False)
    await dispatcher.dispatch(RUN_ID)
    await dispatcher.dispatch(RUN_ID)
    assert executed == [RUN_ID]
    await dispatcher.resume(RUN_ID)
    assert executed == [RUN_ID, RUN_ID]


async def test_inline_dispatch_inside_an_open_unit_of_work_is_refused() -> None:
    executed: list[object] = []

    async def execute(run_id: UUID) -> None:
        executed.append(run_id)

    dispatcher = InlineRunDispatcher(execute, unit_of_work_open=lambda: True)
    with pytest.raises(RuntimeError, match="after the creating commit"):
        await dispatcher.dispatch(RUN_ID)
    assert executed == []
