from uuid import UUID

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
