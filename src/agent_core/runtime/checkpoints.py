"""Shared checkpoint seeding for submission and checkpoint-loss recovery."""

from __future__ import annotations

from agent_core.domain.agents import Principal
from agent_core.domain.context import TaskStatus, WorkingState
from agent_core.domain.events import NewEvent
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork


class DurableCheckpointSeeder:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def __call__(
        self,
        uow: RepositoryUnitOfWork,
        run: Run,
        through_sequence: int | None,
        lease: WorkerLease | None,
        principal: Principal,
    ) -> RunCheckpoint:
        history = await uow.history.catch_up(run.session_id)
        through = history.through_sequence if through_sequence is None else through_sequence
        history = await uow.history.read(run.session_id, through)
        state = WorkingState()
        event = await uow.events.latest_before(
            run.session_id,
            through + 1,
            "context.working_state.updated",
            principal,
        )
        if event is not None:
            raw_state = event.payload.get("working_state")
            if isinstance(raw_state, dict):
                state = WorkingState.model_validate(raw_state)
        carried = state.model_copy(
            update={
                "tasks": [
                    task.model_copy(deep=True)
                    for task in state.tasks
                    if task.status is not TaskStatus.COMPLETED
                ],
                "next_action": None,
            },
            deep=True,
        )
        working_container: dict[str, object] = {}
        if carried != WorkingState():
            working_container["context"] = carried.model_dump(mode="json")
        checkpoint = RunCheckpoint(
            run_id=run.id,
            version=1,
            status=run.status,
            conversation=[item.model_copy(deep=True) for item in history.items],
            working_state=working_container,
            budget_state={
                "step_count": run.step_count,
                "model_call_count": run.model_call_count,
                "tool_call_count": run.tool_call_count,
                "usage": run.usage.model_dump(mode="json"),
            },
            created_at=self._clock.now(),
        )
        event = await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="run.checkpointed",
                actor_type="runtime" if lease is not None else "application",
                payload={"version": 1, "trigger": "seed", "full": True},
            ),
            lease=lease,
        )
        checkpoint.last_event_sequence = event.sequence
        await uow.checkpoints.write(run.id, checkpoint, full=True, lease=lease)
        return checkpoint
