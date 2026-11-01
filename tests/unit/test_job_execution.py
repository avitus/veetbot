"""Saved jobs reuse private approved access and require no GitHub read or model plan."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck
from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.application.job_execution import JobExecution
from agent_core.domain.jobs import JobAccess, JobBrokerError, JobProgram, JobResult
from tests.contract.support import RUN_ID, SESSION_ID, memory_uow_factory, principal, run
from tests.contract.test_job_store_contract import record


class Runner:
    def __init__(self) -> None:
        self.inputs: Any = None
        self.program: JobProgram | None = None

    async def run(self, program: JobProgram, inputs: Any, **kwargs: Any) -> JobResult:
        self.inputs = inputs
        self.program = program
        return JobResult({"count": len(inputs["items"])}, "", 0)


async def test_run_uses_stored_revision_and_grant_without_repository_or_model_access() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal()
    saved = record(owner)
    async with factory() as uow:
        await uow.jobs.put(saved, expected_version=None)
    runner = Runner()

    async def forbidden(**kwargs: Any) -> dict[str, Any]:
        pytest.fail("Code-only work invoked a remote service.")

    service = JobExecution(
        uow_factory=factory,
        clock=clock,
        runner=lambda timeout: runner,
        schema_check=Draft202012SchemaCheck(),
        http=forbidden,
        feeds=CockpitFeeds(
            uow_factory=factory, clock=clock, schema_check=Draft202012SchemaCheck(), history_max=10
        ),
    )
    for _ in range(2):
        result = await service.run(
            principal=owner,
            slug=saved.slug,
            revision_id=UUID(int=1),
            grant_id=UUID(int=2),
            run_id=UUID(int=300),
            lease_epoch=1,
            model=forbidden,
        )
        assert result.value == {"count": 2}
    assert runner.inputs == {"items": [1, 2]}
    assert (
        runner.program is not None
        and runner.program.source == saved.revisions[str(UUID(int=1))].source
    )
    service.enabled = False
    with pytest.raises(ValueError, match=r"job.disabled"):
        await service.run(
            principal=owner,
            slug=saved.slug,
            revision_id=UUID(int=1),
            grant_id=UUID(int=2),
            run_id=UUID(int=300),
            lease_epoch=1,
            model=forbidden,
        )


class BrokerCallingRunner:
    """Stands in for a sandboxed program that calls the broker twice."""

    def __init__(self, clock: Any) -> None:
        self.clock = clock

    async def run(
        self, program: JobProgram, inputs: Any, *, dispatch: Any, **kwargs: Any
    ) -> JobResult:
        url = "https://api.example.com/private-path"
        first = await dispatch("http.request", {"url": url}, "call-1")
        second = await dispatch("http.request", {"url": url}, "call-2")
        return JobResult({"outcomes": [first["status"], second["status"]]}, "", 2)


async def test_each_broker_call_records_its_outcome_and_duration_without_its_arguments() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal()
    saved = record(owner)
    grant = saved.grants[str(UUID(int=2))]
    saved = saved.model_copy(
        update={
            "grants": {
                str(grant.id): grant.model_copy(
                    update={"access": JobAccess({"items": [1]}, {"api.example.com": None})}
                )
            }
        }
    )
    async with factory() as uow:
        await uow.jobs.put(saved, expected_version=None)
        await uow.runs.create(run())
    calls = 0

    async def http(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        clock.advance(timedelta(seconds=3))
        if calls == 2:
            raise JobBrokerError("job.response_limit")
        return {"status": 200, "json": {}}

    async def unused(**kwargs: Any) -> dict[str, Any]:
        pytest.fail("No model call was expected.")

    service = JobExecution(
        uow_factory=factory,
        clock=clock,
        runner=lambda timeout: BrokerCallingRunner(clock),
        schema_check=Draft202012SchemaCheck(),
        http=http,
        feeds=CockpitFeeds(
            uow_factory=factory, clock=clock, schema_check=Draft202012SchemaCheck(), history_max=10
        ),
    )
    with pytest.raises(ValueError, match=r"job\.response_limit"):
        await service.run(
            principal=owner,
            slug=saved.slug,
            revision_id=UUID(int=1),
            grant_id=UUID(int=2),
            run_id=RUN_ID,
            lease_epoch=1,
            model=unused,
        )
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, 0, owner, limit=50)
    completed = [event.payload for event in events if event.event_type == "job.broker.completed"]
    assert completed == [
        {
            "operation": "http.request",
            "call_id": "call-1",
            "trial": False,
            "outcome": "succeeded",
            "reason_code": None,
            "duration_ms": 3000,
        },
        {
            "operation": "http.request",
            "call_id": "call-2",
            "trial": False,
            "outcome": "failed",
            "reason_code": "job.response_limit",
            "duration_ms": 3000,
        },
    ]
    assert "private-path" not in str([event.payload for event in events])


async def test_a_lost_timing_event_does_not_lose_the_broker_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call already happened, so the program must still receive its result."""

    clock, factory = await memory_uow_factory()
    owner = principal()
    saved = record(owner)
    grant = saved.grants[str(UUID(int=2))]
    saved = saved.model_copy(
        update={
            "grants": {
                str(grant.id): grant.model_copy(
                    update={"access": JobAccess({"items": [1]}, {"api.example.com": None})}
                )
            }
        }
    )
    async with factory() as uow:
        await uow.jobs.put(saved, expected_version=None)
        await uow.runs.create(run())
        events = type(uow.events)
    append = events.append

    async def failing_append(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        if event.event_type == "job.broker.completed":
            raise ConnectionError("database went away")
        return await append(self, event, *args, **kwargs)

    monkeypatch.setattr(events, "append", failing_append)
    calls = 0

    async def http(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": 200, "json": {}}

    async def unused(**kwargs: Any) -> dict[str, Any]:
        pytest.fail("No model call was expected.")

    service = JobExecution(
        uow_factory=factory,
        clock=clock,
        runner=lambda timeout: BrokerCallingRunner(clock),
        schema_check=Draft202012SchemaCheck(),
        http=http,
        feeds=CockpitFeeds(
            uow_factory=factory, clock=clock, schema_check=Draft202012SchemaCheck(), history_max=10
        ),
    )
    result = await service.run(
        principal=owner,
        slug=saved.slug,
        revision_id=UUID(int=1),
        grant_id=UUID(int=2),
        run_id=RUN_ID,
        lease_epoch=1,
        model=unused,
    )
    assert result.value == {"outcomes": ["succeeded", "succeeded"]}
    assert calls == 2
