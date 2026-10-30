"""Prepare a widget privately, then install its exact package with one approval."""

import ast
import json
from dataclasses import replace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.application.authorization import require_scope
from agent_core.application.cockpit import screen_names, screen_settings_values
from agent_core.application.cockpit_installation import (
    INSTALL_ARGUMENTS,
    installation_approval,
    installation_key,
    prepared_installation,
)
from agent_core.application.jobs import _strings, job_commit_message
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.cockpit_installation import WidgetInstallation
from agent_core.domain.errors import AgentCoreError, NotFoundError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.jobs import JobGrant, JobProgram
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.ports.cockpit import BuildScreen
from agent_core.tools.cockpit import CockpitToolset, CockpitWidgetWriteTool, offers_widget_writes
from agent_core.tools.job_schedules import JobScheduleCreateTool
from agent_core.tools.jobs import JobToolset, _failure, _ok

INSTALL_TOOL_NAMES = ("cockpit.prepare", "cockpit.install")
_INSTALL_SCOPES = {"cockpit.write", "jobs.write", "jobs.run", "schedule.write"}


def _spec(name: str, description: str, schema: dict[str, Any], *, prepare: bool) -> ToolSpec:
    """Declare shared scope requirements and the preparation or publication effect."""
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=description,
        input_schema=schema,
        output_schema={"type": "object"},
        side_effect=SideEffectClass.NONE if prepare else SideEffectClass.PUBLICATION,
        risk=RiskLevel.LOW if prepare else RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.READ_ONLY
        if prepare
        else IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        required_scopes=_INSTALL_SCOPES,
        timeout_seconds=660,
        maximum_output_bytes=65536,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )


class CockpitPrepareTool:
    spec = _spec(
        "cockpit.prepare",
        "Prepare a complete Cockpit widget installation without publishing or asking approval. "
        "Use this instead of separate widget/job writes when creating a widget. Supply widget "
        "files and optionally a Python collector with private access and refresh interval. "
        "Run job.manage trials first with fixtures or existing approved access; preparation "
        "checks packages, privacy, inputs and revisions without new network/model access. "
        "Repair all diagnostics, then pass the successful preparation IDs to cockpit.install "
        "for ONE summarized approval covering publication, private access and scheduling.",
        WidgetInstallation.model_json_schema(),
        prepare=True,
    )

    def __init__(
        self,
        cockpit: CockpitToolset,
        jobs: JobToolset,
        schedule: JobScheduleCreateTool,
        operator_hosts: frozenset[str] | None = None,
    ) -> None:
        """Bind existing publication and scheduling services with operator host limits."""
        self.cockpit = cockpit
        self.jobs = jobs
        self.schedule = schedule
        self.operator_hosts = operator_hosts

    async def _owner_chat(self, context: ToolExecutionContext) -> None:
        """Require every installation scope and an eligible interactive owner session."""
        for scope in _INSTALL_SCOPES:
            require_scope(context.principal, scope)
        async with self.cockpit.uow_factory() as uow:
            session = await uow.sessions.get(context.session_id, context.principal)
        if context.run_kind != "interactive" or not offers_widget_writes(session.metadata):
            raise ValueError("Prepare and install widgets in the owner's interactive chat.")

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Validate a private proposal and return immutable preparation identifiers."""
        try:
            await self._owner_chat(context)
            plan = WidgetInstallation.model_validate(arguments)
            await self._check(plan, context)
            return _ok(
                {
                    "ready": True,
                    "preparation_run_id": str(context.run_id),
                    "preparation_id": str(context.invocation_id),
                    "summary": plan.summary(),
                    "next": "Call cockpit.install with these IDs for one final approval.",
                }
            )
        except (ValueError, SyntaxError, AgentCoreError) as exc:
            return _failure("cockpit.install_invalid", str(exc))

    async def _check(self, plan: WidgetInstallation, context: ToolExecutionContext) -> None:
        """Reject stale, invalid or privacy-violating packages before any installation effect."""
        owner = context.principal
        head = await self.cockpit.publication.head_revision(owner, plan.slug)
        if (None if head is None else head.id) != plan.expected_revision:
            raise ValueError("The widget changed. Read its head revision and prepare again.")
        values: tuple[str, ...] = ()
        if plan.job is not None:
            job = plan.job
            access = job.access
            values = tuple(_strings(access.inputs))
            JobProgram(job.source, job.manifest.calls)
            ast.parse(job.source)
            if not access.feeds or not access.feeds <= job.manifest.feeds:
                raise ValueError(
                    "A collector schedule needs granted feeds declared by its manifest."
                )
            if self.operator_hosts is not None and not set(access.hosts) <= self.operator_hosts:
                raise ValueError("The proposed hosts are outside the operator's enabled job hosts.")
            schema = self.jobs.publication.schema_check
            if schema.schema_violations(job.manifest.input_schema) or schema.violations(
                job.manifest.input_schema, access.inputs
            ):
                raise ValueError("Private inputs must fit the job's self-contained input schema.")
            manifest = json.loads(plan.files.get("widget.json", "{}"))
            feeds = manifest.get("feeds") if isinstance(manifest, dict) else None
            if not isinstance(feeds, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in feeds
            ):
                raise ValueError(
                    "The widget manifest must declare feeds as objects with string IDs."
                )
            declared = {item["id"] for item in feeds}
            if not access.feeds <= declared:
                raise ValueError("The widget must declare the collector's feeds.")
            async with self.cockpit.uow_factory() as uow:
                try:
                    saved = await uow.jobs.get(owner, job.slug)
                except NotFoundError:
                    saved = None
                if (None if saved is None else saved.live_revision_id) != job.expected_revision:
                    raise ValueError("The job changed. Read its live revision and prepare again.")
                if saved is not None and (
                    saved.archived
                    or any(item.state == "pending_commit" for item in saved.revisions.values())
                ):
                    raise ValueError("The job is archived or has pending publication.")
                names = await screen_names(uow, owner, self.jobs.publication.owner_names)
                settings = list(await screen_settings_values(uow, owner))
                for prior in await uow.jobs.list(owner):
                    for grant in prior.grants.values():
                        settings.extend(_strings(grant.access.inputs))
            diagnostics = await self.jobs.publication.screen.check(
                owner,
                {"main.py": job.source, "job.json": job.manifest.model_dump_json()},
                screen=BuildScreen(
                    names=names,
                    settings_values=(*settings, *values),
                    commit_message=job_commit_message(job.slug, UUID(int=0)),
                ),
            )
            if diagnostics:
                raise ValueError("; ".join(f"{d.rule}: {d.message}" for d in diagnostics))
        build = await self.cockpit.publication.check(
            owner,
            plan.slug,
            plan.files,
            private_values=values,
        )
        if not build.ok:
            raise ValueError("; ".join(f"{d.rule}: {d.message}" for d in build.diagnostics))


class CockpitInstallTool(CockpitPrepareTool):
    spec = _spec(
        "cockpit.install",
        "Install the exact successful cockpit.prepare proposal after ONE summarized approval. "
        "The approval covers widget/job publication, private access and the refresh schedule. "
        "If publication is pending, repeat these same IDs to resume without another approval. "
        "Changed code or access needs a new preparation. Collection begins at the next interval.",
        INSTALL_ARGUMENTS,
        prepare=False,
    )

    async def approval_view_in_session(
        self,
        arguments: dict[str, Any],
        **context: Any,
    ) -> ApprovalPresentation:
        """Describe only the successful proposal owned by the current conversation."""
        plan = await prepared_installation(
            self.cockpit.uow_factory,
            context["principal"],
            context["run"].session_id,
            arguments,
        )
        return ApprovalPresentation(
            summary=plan.headline(),
            arguments={**plan.summary(), **arguments},
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Verify exact approval and record its receipt before resuming installation."""
        try:
            await self._owner_chat(context)
            plan = await prepared_installation(
                self.cockpit.uow_factory,
                context.principal,
                context.session_id,
                arguments,
            )
            approval = await installation_approval(
                self.cockpit.uow_factory,
                context.principal,
                context.session_id,
                arguments,
                context.invocation_id,
            )
            if approval is None:
                return _failure(
                    "cockpit.install_approval_required", "Approve this prepared installation first."
                )
            key = installation_key(context.principal, UUID(arguments["preparation_id"]))
            async with self.cockpit.uow_factory() as uow:
                receipt = await uow.process_events.get_by_derivation(key)
            if receipt is None:
                # Catch stale revisions or changed operator constraints before any publication.
                await self._check(plan, context)
                async with self.cockpit.uow_factory() as uow:
                    await uow.process_events.append(
                        ProcessEvent(
                            id=self.jobs.publication.ids.new_id(),
                            event_type="cockpit.install.authorized",
                            actor_type="cockpit",
                            actor_id=context.principal.principal_id,
                            derivation_key=key,
                            payload={"approval_id": str(approval.id)},
                            created_at=self.cockpit.clock.now(),
                        )
                    )
            await context.mark_effect_sent()
            return await self._install(plan, context, key, approval.id)
        except (ValueError, KeyError, AgentCoreError) as exc:
            return _failure("cockpit.install_invalid", str(exc))

    async def _install(
        self,
        plan: WidgetInstallation,
        context: ToolExecutionContext,
        key: str,
        approval_id: UUID,
    ) -> ToolResult:
        """Publish approved components idempotently, then schedule the live collector."""
        owner = context.principal
        task: dict[str, Any] | None = None
        if plan.job is not None:
            job = plan.job
            publication = self.jobs.publication
            revision = await publication.write(
                principal=owner,
                slug=job.slug,
                source=job.source,
                manifest=job.manifest,
                expected_revision=job.expected_revision,
                request_key=key + ":job",
            )
            await publication.flush(owner)
            async with self.cockpit.uow_factory() as uow:
                saved = await uow.jobs.get(owner, job.slug)
                revision = saved.revisions[str(revision.id)]
                if (
                    revision.state != "live"
                    or saved.live_revision_id != revision.id
                    or saved.archived
                ):
                    return _ok({"state": revision.state, "stage": "job", "complete": False})
                grant_id = uuid5(NAMESPACE_URL, key + ":grant")
                if str(grant_id) not in saved.grants:
                    grant = JobGrant(
                        id=grant_id,
                        access=job.access,
                        approval_id=approval_id,
                        created_at=self.cockpit.clock.now(),
                    )
                    await uow.jobs.put(
                        saved.model_copy(
                            update={
                                "version": saved.version + 1,
                                "grants": {**saved.grants, str(grant_id): grant},
                            }
                        ),
                        expected_version=saved.version,
                    )
            task = {
                "kind": "job",
                "slug": job.slug,
                "revision_id": str(revision.id),
                "grant_id": str(grant_id),
            }
        widget_arguments: dict[str, Any] = {
            "operation": "create" if plan.expected_revision is None else "update",
            "slug": plan.slug,
            "files": plan.files,
        }
        if plan.expected_revision is not None:
            widget_arguments["expected_revision"] = str(plan.expected_revision)
        result = await CockpitWidgetWriteTool(self.cockpit).execute(
            widget_arguments,
            replace(context, idempotency_key=key + ":widget"),
        )
        if not result.ok or result.structured is None or result.structured.get("state") != "live":
            return result
        if task is not None:
            assert plan.job is not None
            scheduled = await self.schedule.execute(
                {
                    "title": plan.job.manifest.title,
                    "instruction": "Refresh the approved widget feed.",
                    "cadence": {
                        "kind": "INTERVAL",
                        "every_minutes": plan.job.every_minutes,
                        "timezone": "UTC",
                    },
                    "task": task,
                },
                replace(context, idempotency_key=key + ":schedule"),
            )
            if not scheduled.ok:
                return scheduled
            return _ok(
                {"complete": True, "widget": result.structured, "schedule": scheduled.structured}
            )
        return _ok({"complete": True, "widget": result.structured})
