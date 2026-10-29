"""One-time and scheduled execution of locally pinned job revisions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from agent_core.application.cockpit_feeds import CockpitFeedRefusedError, CockpitFeeds
from agent_core.application.job_broker import JobBroker
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.jobs import JobAccess, JobBrokerError, JobManifest, JobProgram, JobResult
from agent_core.ports.cockpit import JsonSchemaCheck
from agent_core.ports.determinism import Clock
from agent_core.ports.jobs import JobRunner
from agent_core.ports.persistence import UnitOfWorkFactory


class JobExecution:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        runner: Callable[[int], JobRunner],
        schema_check: JsonSchemaCheck,
        http: Callable[..., Awaitable[dict[str, Any]]],
        feeds: CockpitFeeds,
        enabled: bool = True,
    ) -> None:
        self.uow_factory = uow_factory
        self.clock = clock
        self.runner = runner
        self.schema_check = schema_check
        self.http = http
        self.feeds = feeds
        self.enabled = enabled

    async def run(
        self,
        *,
        principal: Principal,
        slug: str,
        revision_id: UUID,
        grant_id: UUID,
        run_id: UUID,
        lease_epoch: int,
        model: Callable[..., Awaitable[dict[str, Any]]],
        trial: bool = False,
    ) -> JobResult:
        """Execute a locally stored committed revision using its owned immutable grant."""
        if not self.enabled:
            raise JobBrokerError("job.disabled")
        async with self.uow_factory() as uow:
            saved = await uow.jobs.get(principal, slug)
        revision = saved.revisions.get(str(revision_id))
        grant = saved.grants.get(str(grant_id))
        if saved.archived:
            raise JobBrokerError("job.archived")
        if revision is None or revision.commit_sha is None:
            raise JobBrokerError("job.revision_unpublished")
        if grant is None:
            raise JobBrokerError("job.grant_missing")
        return await self._execute(
            principal=principal,
            program=JobProgram(revision.source, revision.manifest.calls),
            manifest=revision.manifest,
            access=grant.access,
            run_id=run_id,
            lease_epoch=lease_epoch,
            model=model,
            trial=trial,
        )

    async def trial(
        self,
        *,
        principal: Principal,
        source: str,
        manifest: JobManifest,
        inputs: dict[str, Any],
        run_id: UUID,
        lease_epoch: int,
        model: Callable[..., Awaitable[dict[str, Any]]],
        slug: str | None = None,
        grant_id: UUID | None = None,
    ) -> JobResult:
        """Preview supplied source offline or under an existing grant without live feed writes."""
        access = JobAccess(inputs, {})
        if grant_id is not None:
            if slug is None:
                raise JobBrokerError("job.grant_missing")
            async with self.uow_factory() as uow:
                saved = await uow.jobs.get(principal, slug)
            grant = saved.grants.get(str(grant_id))
            if grant is None or saved.archived:
                raise JobBrokerError("job.grant_missing")
            access = grant.access
            if inputs and inputs != access.inputs:
                raise JobBrokerError("job.grant_inputs_changed")
        return await self._execute(
            principal=principal,
            program=JobProgram(source, manifest.calls),
            manifest=manifest,
            access=access,
            run_id=run_id,
            lease_epoch=lease_epoch,
            model=model,
            trial=True,
        )

    async def _execute(
        self,
        *,
        principal: Principal,
        program: JobProgram,
        manifest: JobManifest,
        access: JobAccess,
        run_id: UUID,
        lease_epoch: int,
        model: Callable[..., Awaitable[dict[str, Any]]],
        trial: bool,
    ) -> JobResult:
        """Validate inputs, construct the granted broker, and run within the approved deadline."""
        if not self.enabled:
            raise JobBrokerError("job.disabled")
        if self.schema_check.schema_violations(
            manifest.input_schema
        ) or self.schema_check.violations(
            manifest.input_schema,
            access.inputs,
        ):
            raise JobBrokerError("job.inputs_invalid")
        if not access.feeds <= manifest.feeds:
            raise JobBrokerError("job.feed_undeclared")
        logs: list[str] = []

        async def event(kind: str, payload: dict[str, Any]) -> None:
            async with self.uow_factory() as uow:
                run = await uow.runs.get(run_id, principal)
                await uow.events.append(
                    NewEvent(
                        session_id=run.session_id,
                        run_id=run_id,
                        event_type=kind,
                        actor_type="jobs",
                        actor_id=principal.principal_id,
                        payload=payload,
                    )
                )

        async def log(*, message: str, call_id: str) -> None:
            logs.append(message)
            await event("job.log", {"message": message, "call_id": call_id})

        async def feed(*, preview: bool, call_id: str, **arguments: Any) -> dict[str, Any]:
            """Validate feed data and return either a live receipt or a non-mutating preview."""
            del call_id
            try:
                published, snapshot = await self.feeds.publish(
                    principal, preview=preview, **arguments
                )
            except CockpitFeedRefusedError as exc:
                raise JobBrokerError(f"job.{exc.reason}") from None
            result: dict[str, Any] = {
                "feed_id": published.id,
                "sequence": snapshot.sequence,
                "preview": preview,
            }
            if preview:
                result["data"] = snapshot.data
            return result

        broker = JobBroker(access, http=self.http, model=model, feed=feed, log=log, trial=trial)

        async def dispatch(call: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
            logs.append(f"Broker request: {call}")
            # Deliberately omit arguments: URLs, bodies and prompts stay out of the log.
            await event(
                "job.broker.requested", {"operation": call, "call_id": call_id, "trial": trial}
            )
            return await broker.dispatch(call, arguments, call_id)

        async with asyncio.timeout(access.timeout_seconds):
            result = await self.runner(access.timeout_seconds).run(
                program,
                access.inputs,
                tenant_id=principal.tenant_id,
                run_id=run_id,
                lease_epoch=lease_epoch,
                dispatch=dispatch,
            )
        broker.check_result()
        return JobResult(
            result.value, "\n".join([*logs, result.diagnostics]).strip(), result.broker_calls
        )
