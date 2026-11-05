"""Job execution uses a caller-authorized broker, never ambient capabilities."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import BuildDiagnostic
from agent_core.domain.jobs import JobProgram, JobRecord, JobResult
from agent_core.ports.cockpit import BuildScreen

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


class JobStore(Protocol):
    async def get(self, principal: Principal, slug: str) -> JobRecord: ...

    async def list(self, principal: Principal) -> list[JobRecord]: ...

    async def put(self, record: JobRecord, *, expected_version: int | None) -> JobRecord:
        """Insert or replace the expected version, else raise ConflictError."""
        ...

    async def erase_principal(self, principal: Principal) -> int: ...


class JobScreen(Protocol):
    async def check(
        self, principal: Principal, files: Mapping[str, str], *, screen: BuildScreen
    ) -> tuple[BuildDiagnostic, ...]: ...
