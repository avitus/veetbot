"""Job execution uses a caller-authorized broker, never ambient capabilities."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from uuid import UUID

from agent_core.domain.jobs import JobProgram, JobResult

type JobDispatch = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]


class JobRunner(Protocol):
    async def run(
        self,
        program: JobProgram,
        inputs: Mapping[str, Any],
        *,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        dispatch: JobDispatch,
    ) -> JobResult: ...
