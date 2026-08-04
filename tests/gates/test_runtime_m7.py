"""Milestone 7 runtime/context handshake hard gates."""

from __future__ import annotations

from typing import Any

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.context import WorkingState
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
)
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.ports.context import PressureAwareContextBuilder
from tests.contract.support import NOW


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/runtime-m7",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
    )


class _ObservedBuilder:
    def __init__(self, inner: PressureAwareContextBuilder) -> None:
        self.inner = inner
        self.calls: list[str] = []

    async def measure(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("measure")
        return await self.inner.measure(*args, **kwargs)

    async def assemble(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("assemble")
        return await self.inner.assemble(*args, **kwargs)

    async def build(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("build")
        return await self.inner.build(*args, **kwargs)


async def test_build_fits() -> None:
    async with build(
        settings=_settings(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        executor = composition.executor
        observed = _ObservedBuilder(executor._context_builder)
        executor._context_builder = observed
        run_id = await composition.runs.submit("measure context pressure before building")
        completed = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)

    assert completed.status is RunStatus.COMPLETED
    assert observed.calls
    assert set(observed.calls) == {"assemble"}
    checked_requests = 0
    for event in events:
        if event.event_type != "model.request.started":
            continue
        checked_requests += 1
        assert int(event.payload["context_total_tokens"]) <= int(
            event.payload["context_capacity_tokens"]
        )
    assert checked_requests > 0
    assert len(observed.calls) == checked_requests


async def test_build_stable() -> None:
    async def capture_request() -> Any:
        async with build(
            settings=_settings(),
            script=FakeModelScript(turns=[ScriptedTurn(text="stable response")]),
            clock=FixedClock(NOW),
            ids=SequenceIdFactory(),
            limits=RunLimits(max_steps=2, max_model_calls=2, max_tool_calls=1),
        ) as composition:
            run_id = await composition.runs.submit("build this deterministic step")
            completed = await composition.runs.wait_terminal(run_id)
            provider = composition.executor._model_provider
            assert isinstance(provider, FakeModelProvider)
            assert completed.status is RunStatus.COMPLETED
            assert len(provider.requests) == 1
            return provider.requests[0]

    first = await capture_request()
    second = await capture_request()

    assert first.model_dump_json() == second.model_dump_json()
    assert first.metadata["prefix_sha256"] == second.metadata["prefix_sha256"]


async def test_working_state_event_replays_into_the_next_run() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="context.update_working_state",
                        call_id="state-update",
                        arguments={
                            "objective": "ship milestone 7",
                            "add_constraints": ["retain provenance"],
                            "upsert_tasks": [
                                {
                                    "task_id": "done",
                                    "description": "completed task",
                                    "status": "completed",
                                },
                                {
                                    "task_id": "open",
                                    "description": "open task",
                                    "status": "open",
                                },
                            ],
                            "next_action": "finish this run",
                        },
                    )
                ]
            ),
            ScriptedTurn(text="state recorded"),
            ScriptedTurn(text="state carried"),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        limits=RunLimits(max_steps=3, max_model_calls=4, max_tool_calls=2),
    ) as composition:
        session_id = await composition.sessions.create()
        first_id = await composition.runs.submit("record state", session_id)
        first = await composition.runs.wait_terminal(first_id)
        first_events = await composition.runs.events(first_id)
        second_id = await composition.runs.submit("continue", session_id)
        second = await composition.runs.wait_terminal(second_id)
        async with composition.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(second_id)

    assert first.status is RunStatus.COMPLETED
    assert second.status is RunStatus.COMPLETED
    assert sum(event.event_type == "context.working_state.updated" for event in first_events) == 1
    assert checkpoint is not None
    state = WorkingState.model_validate(checkpoint.working_state["context"])
    assert state.objective == "ship milestone 7"
    assert state.constraints == ["retain provenance"]
    assert [task.task_id for task in state.tasks] == ["open"]
    assert state.next_action is None
