"""Schedule tool versions that pin a saved job and its existing private grant."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from agent_core.application.schedule_service import ScheduleService, _definition_from_revision
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.errors import AgentCoreError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass
from agent_core.domain.schedules import (
    FEED_KEEPING_SCOPE,
    JobTask,
    ScheduleDefinition,
    ScheduleDefinitionLimits,
    ScheduleNotify,
)
from agent_core.domain.tools import ToolExecutionContext, ToolResult
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.scheduling.defaults import derived_definition, schedule_run_limits
from agent_core.tools.jobs import _failure
from agent_core.tools.schedule_create import CockpitScheduleCreateTool, parse_cadence
from agent_core.tools.schedule_lifecycle import CockpitScheduleUpdateTool


async def pin_job(
    definition: ScheduleDefinition, task: JobTask, principal: Principal, factory: UnitOfWorkFactory
) -> ScheduleDefinition:
    """Bind a published revision and approved grant while deriving bounded schedule authority."""
    async with factory() as uow:
        saved = await uow.jobs.get(principal, task.slug)
    grant = saved.grants.get(str(task.grant_id))
    revision = saved.revisions.get(str(task.revision_id))
    if saved.archived or grant is None or revision is None or revision.commit_sha is None:
        raise ValueError("Select a published revision and its approved grant.")
    access = grant.access
    cost = min(Decimal(access.model_cost), definition.limits.max_cost or Decimal(access.model_cost))
    limits = definition.limits.model_copy(
        update={
            "max_cost": cost,
            "max_model_calls": min(access.maximum_requests, definition.limits.max_model_calls)
            if cost
            else 0,
            "synthesis_reserve_steps": 0,
            "synthesis_reserve_model_calls": 0,
            "synthesis_reserve_cost": Decimal(0),
        }
    )
    return ScheduleDefinition.model_validate(
        {
            **definition.model_dump(),
            "task": task,
            "limits": limits,
            "run_timeout_seconds": min(definition.run_timeout_seconds, access.timeout_seconds),
            "requested_scopes": {"jobs.run"}
            | ({"cockpit.read", "cockpit.write"} if access.feeds else set()),
            "notify": ScheduleNotify.ATTENTION_ONLY if access.feeds else definition.notify,
            "browser_profile_id": None,
            "call_plan": None,
        }
    )


class JobScheduleCreateTool(CockpitScheduleCreateTool):
    spec = CockpitScheduleCreateTool.spec.model_copy(
        update={
            "version": "1.7.0",
            "description": CockpitScheduleCreateTool.spec.description
            + (
                " For saved code, supply task with kind=job, slug, revision_id and grant_id. "
                "This runs code directly; only explicit model calls cost tokens. The existing "
                "grant supplies private inputs, feeds and limits. Do not combine a job with "
                "website or calling flags."
            ),
            "input_schema": {
                **CockpitScheduleCreateTool.spec.input_schema,
                "properties": {
                    **CockpitScheduleCreateTool.spec.input_schema["properties"],
                    "task": JobTask.model_json_schema(),
                },
            },
        }
    )

    async def approval_view_in_session(
        self, arguments: dict[str, Any], **context: Any
    ) -> ApprovalPresentation:
        """Show the selected job's private grant alongside the requested schedule change."""
        if "task" not in arguments:
            return await super().approval_view_in_session(arguments, **context)
        assert self._uow_factory is not None
        task = JobTask.model_validate(arguments["task"])
        async with self._uow_factory() as uow:
            saved = await uow.jobs.get(context["principal"], task.slug)
        grant = saved.grants[str(task.grant_id)]
        return ApprovalPresentation(
            summary="Schedule this saved job at the listed cadence using its approved access.",
            arguments={**arguments, "approved_access": grant.model_dump(mode="json")},
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Apply job schedule changes or delegate ordinary schedules to the existing tool."""
        if "task" not in arguments:
            return await super().execute(arguments, context)
        try:
            if any(
                arguments.get(key)
                for key in ("use_website", "place_call", "call_plan", "publish_cockpit_feed")
            ):
                raise ValueError("A job uses its grant, without agent-run flags.")
            assert self._uow_factory is not None
            definition = derived_definition(
                self._agent,
                self._limits,
                title=arguments["title"],
                instruction=arguments["instruction"],
                cadence=parse_cadence(arguments, interval=True),
            )
            definition = await pin_job(
                definition,
                JobTask.model_validate(arguments["task"]),
                context.principal,
                self._uow_factory,
            )
            await context.mark_effect_sent()
            record = await self._service.create(
                context.principal, definition, context.idempotency_key
            )
            schedule = record.schedule
            body = {
                "schedule_id": str(schedule.id),
                "state": schedule.state.value,
                "next_fire_at": schedule.next_fire_at.isoformat()
                if schedule.next_fire_at
                else None,
                "replayed": record.replayed,
            }
            return ToolResult(
                ok=True, content=[TextPart(text="Job schedule created.")], structured=body
            )
        except (ValueError, KeyError, AgentCoreError) as exc:
            return _failure("schedule.job_invalid", str(exc))


class JobScheduleUpdateTool(CockpitScheduleUpdateTool):
    spec = CockpitScheduleUpdateTool.spec.model_copy(
        update={
            "version": "1.2.0",
            "idempotency": IdempotencyClass.NON_IDEMPOTENT,
            "description": CockpitScheduleUpdateTool.spec.description
            + (
                " Supply task to switch a feed refresh to a saved job or repin a job schedule "
                "to a published revision and approved grant. "
                "Existing occurrences keep their pins."
            ),
            "input_schema": {
                **CockpitScheduleUpdateTool.spec.input_schema,
                "properties": {
                    **CockpitScheduleUpdateTool.spec.input_schema["properties"],
                    "task": JobTask.model_json_schema(),
                },
            },
        }
    )

    def __init__(
        self,
        service: ScheduleService,
        factory: UnitOfWorkFactory,
        agent: AgentSpec,
        limits: ScheduleDefinitionLimits,
    ) -> None:
        super().__init__(service)
        self.factory = factory
        self.agent = agent
        self.limits = limits

    async def approval_view_in_session(
        self, arguments: dict[str, Any], **context: Any
    ) -> ApprovalPresentation:
        """Show the selected job's private grant alongside the requested schedule change."""
        if "task" not in arguments:
            summary, view = await self.approval_view(
                arguments, tenant_id=context["principal"].tenant_id
            )
            return ApprovalPresentation(summary=summary, arguments=view)
        task = JobTask.model_validate(arguments["task"])
        async with self.factory() as uow:
            saved = await uow.jobs.get(context["principal"], task.slug)
        return ApprovalPresentation(
            summary="Update the pinned job and access for future schedule occurrences.",
            arguments={
                **arguments,
                "approved_access": saved.grants[str(task.grant_id)].model_dump(mode="json"),
            },
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Apply job schedule changes or delegate ordinary schedules to the existing tool."""
        if "task" not in arguments:
            return await super().execute(arguments, context)
        try:
            current = await self._service.get(context.principal, UUID(arguments["schedule_id"]))
            if (
                not isinstance(current.revision.task, JobTask)
                and FEED_KEEPING_SCOPE not in current.revision.requested_scopes
            ):
                raise ValueError(
                    "Only a feed-keeping agent schedule can be switched to a saved job."
                )
            definition = _definition_from_revision(current.revision).model_copy(
                update={
                    "limits": schedule_run_limits(self.agent, self.limits),
                    "run_timeout_seconds": self.limits.max_run_timeout_seconds,
                }
            )
            updates = {key: arguments[key] for key in ("title", "instruction") if key in arguments}
            if "at" in arguments or "cadence" in arguments:
                updates["cadence"] = parse_cadence(arguments, interval=True)
            definition = await pin_job(
                definition.model_copy(update=updates),
                JobTask.model_validate(arguments["task"]),
                context.principal,
                self.factory,
            )
            if (
                not isinstance(current.revision.task, JobTask)
                and FEED_KEEPING_SCOPE not in definition.requested_scopes
            ):
                raise ValueError("The replacement job needs approved feed publication access.")
            await context.mark_effect_sent()
            result = await self._service.update(
                context.principal, current.schedule.id, arguments["expected_revision"], definition
            )
            schedule = result.schedule
            return ToolResult(
                ok=True,
                content=[TextPart(text="Job schedule updated.")],
                structured={
                    "schedule_id": str(schedule.id),
                    "current_revision": schedule.current_revision,
                    "state": schedule.state.value,
                    "pause_reason": None
                    if schedule.pause_reason is None
                    else schedule.pause_reason.value,
                    "next_fire_at": schedule.next_fire_at.isoformat()
                    if schedule.next_fire_at
                    else None,
                    "replayed": result.replayed,
                },
            )
        except (ValueError, KeyError, AgentCoreError) as exc:
            return _failure("schedule.job_invalid", str(exc))
