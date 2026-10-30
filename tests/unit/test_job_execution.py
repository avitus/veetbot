"""Saved jobs reuse private approved access and require no GitHub read or model plan."""

from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck
from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.application.job_execution import JobExecution
from agent_core.domain.jobs import JobProgram, JobResult
from tests.contract.support import memory_uow_factory, principal
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
