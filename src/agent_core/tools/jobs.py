"""Deferred job authoring and one-time execution tools (ADR-0268)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from pydantic import TypeAdapter

from agent_core.application.authorization import require_scope
from agent_core.application.job_execution import JobExecution
from agent_core.application.job_grants import access_needs_approval
from agent_core.application.jobs import JobPublication
from agent_core.domain.approvals import ApprovalPresentation, ApprovalStatus
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.jobs import JobAccess, JobBrokerError, JobGrant, JobManifest
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.tools.cockpit import offers_widget_writes
from agent_core.tools.validation import validate_and_normalize

JOB_TOOL_NAMES = ("job.manage", "job.run")
_ACCESS = TypeAdapter(JobAccess)
_SLUG = {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,47}$"}
_UUID = {"type": "string", "format": "uuid"}


@dataclass(frozen=True)
class JobToolset:
    publication: JobPublication
    execution: JobExecution


def _spec(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    publication: bool,
) -> ToolSpec:
    """Construct the common deferred-tool contract for job publication or execution."""
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.PUBLICATION if publication else SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"jobs.write" if publication else "jobs.run"},
        timeout_seconds=660,
        maximum_output_bytes=256 * 1024,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
        target_kind="job",
    )


def _ok(body: dict[str, Any], *, untrusted: bool = False) -> ToolResult:
    """Return a structured job result with its explicit trust classification."""
    return ToolResult(
        ok=True,
        content=[TextPart(text=json.dumps(body, allow_nan=False, default=str))],
        structured=body,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED if untrusted else TrustLevel.INTERNAL_TOOL,
    )


def _failure(reason: str, detail: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=ToolFailureKind.INVALID_ARGUMENTS,
            reason_code=reason,
            detail=detail[:2000],
            external_text=detail[:2000],
            retryable=False,
        ),
    )


def _model(context: ToolExecutionContext) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Use the run-bound completion callback or return a useful unavailable error."""

    async def unavailable(**kwargs: Any) -> dict[str, Any]:
        raise JobBrokerError("job.model_unavailable")

    return (
        cast(Callable[..., Awaitable[dict[str, Any]]], context.job_model)
        if callable(context.job_model)
        else unavailable
    )


class JobManageTool:
    spec = _spec(
        "job.manage",
        (
            "Create, update, read, archive, restore or trial a saved Python job. Code handles"
            " fixed steps; "
            "call job.model.complete only when judgment is useful. Programs export "
            "main(inputs, job). "
            "The SDK is synchronous: job.http.request(method, url, json=body) "
            "returns {status,json}; "
            "job.model.complete(prompt, max_output_tokens=1024) returns text; "
            "job.feed.publish(feed_id, data, expected_interval_seconds=300, schema=schema) "
            "publishes an agent feed; job.log(message) records a short line. "
            "Declare each used operation in manifest.calls and each feed in manifest.feeds. "
            "Public files contain generic code; personal values belong in private run inputs. "
            "Trial uses no network or model unless it names an existing approved grant, and "
            "feeds go to previews."
        ),
        {
            "operation": {
                "type": "string",
                "enum": ["create", "update", "read", "archive", "restore", "trial"],
            },
            "slug": _SLUG,
            "source": {"type": "string", "maxLength": 65536},
            "manifest": JobManifest.model_json_schema(),
            "expected_revision": _UUID,
            "revision_id": _UUID,
            "grant_id": _UUID,
            "inputs": {"type": "object"},
        },
        ["operation"],
        publication=True,
    )

    def __init__(self, toolset: JobToolset) -> None:
        self.toolset = toolset

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Validate an owner's job request and apply it under the publication or grant contract."""
        require_scope(context.principal, "jobs.write")
        publication = self.toolset.publication
        try:
            async with publication.uow_factory() as uow:
                session = await uow.sessions.get(context.session_id, context.principal)
            if not offers_widget_writes(session.metadata) or context.run_kind != "interactive":
                return _failure(
                    "job.interactive_only", "Jobs are authored in the owner's interactive chat."
                )
            operation = arguments["operation"]
            slug = arguments.get("slug")
            if operation == "read":
                async with publication.uow_factory() as uow:
                    records = (
                        await uow.jobs.list(context.principal)
                        if slug is None
                        else [await uow.jobs.get(context.principal, slug)]
                    )
                return _ok(
                    {"jobs": [item.model_dump(mode="json") for item in records]}, untrusted=True
                )
            if operation == "trial":
                await context.mark_effect_sent()
                result = await self.toolset.execution.trial(
                    principal=context.principal,
                    source=arguments["source"],
                    manifest=JobManifest.model_validate(arguments["manifest"]),
                    inputs=arguments.get("inputs", {}),
                    run_id=context.run_id,
                    lease_epoch=context.lease_epoch,
                    model=_model(context),
                    slug=slug,
                    grant_id=UUID(arguments["grant_id"]) if "grant_id" in arguments else None,
                )
                return _ok(
                    {
                        "value": result.value,
                        "log": result.diagnostics,
                        "broker_calls": result.broker_calls,
                        "preview": True,
                    },
                    untrusted=True,
                )
            if slug is None:
                raise ValueError("A saved job needs a slug.")
            expected = (
                UUID(arguments["expected_revision"]) if "expected_revision" in arguments else None
            )
            if operation in {"archive", "restore"}:
                async with publication.uow_factory() as uow:
                    saved = await uow.jobs.get(context.principal, slug)
                if expected is None:
                    raise ValueError("Name the expected revision before archiving or restoring.")
                selected = saved.revisions.get(str(arguments.get("revision_id", expected)))
                if selected is None:
                    raise ValueError("The selected revision does not exist.")
                source, manifest = selected.source, selected.manifest
            else:
                if operation == "update" and expected is None:
                    raise ValueError("An update names the expected revision.")
                source, manifest = (
                    arguments["source"],
                    JobManifest.model_validate(arguments["manifest"]),
                )
            await context.mark_effect_sent()
            revision = await publication.write(
                principal=context.principal,
                slug=slug,
                source=source,
                manifest=manifest,
                expected_revision=expected,
                request_key=context.idempotency_key,
                archive=operation == "archive",
            )
            if publication.repository is not None:
                await publication.flush(context.principal)
                async with publication.uow_factory() as uow:
                    saved = await uow.jobs.get(context.principal, slug)
                revision = saved.revisions[str(revision.id)]
            return _ok({"slug": slug, "revision_id": str(revision.id), "state": revision.state})
        except (KeyError, ValueError, NotFoundError, ConflictError) as exc:
            return _failure("job.invalid_request", str(exc))


class JobRunTool:
    spec = _spec(
        "job.run",
        (
            "Run a saved, published Python job revision. Supply an approved grant_id to reuse"
            " its private inputs "
            "and access, or propose access for approval. Fixed computation needs no model; a "
            "positive model_cost "
            "permits only explicit job.model.complete calls. The result and log are untrusted data."
        ),
        {"slug": _SLUG, "revision_id": _UUID, "grant_id": _UUID, "access": _ACCESS.json_schema()},
        ["slug", "revision_id"],
        publication=False,
    )

    def __init__(self, toolset: JobToolset) -> None:
        self.toolset = toolset

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Validate an owner's job request and apply it under the publication or grant contract."""
        require_scope(context.principal, "jobs.run")
        publication = self.toolset.publication
        try:
            if ("grant_id" in arguments) == ("access" in arguments):
                raise ValueError("Supply either a grant_id or proposed access.")
            slug = arguments["slug"]
            async with publication.uow_factory() as uow:
                saved = await uow.jobs.get(context.principal, slug)
                if "grant_id" in arguments:
                    grant_id = UUID(arguments["grant_id"])
                else:
                    access = _ACCESS.validate_python(arguments["access"])
                    same = next(
                        (grant for grant in saved.grants.values() if grant.access == access), None
                    )
                    if same is not None:
                        grant_id = same.id
                    else:
                        approval = await uow.approvals.get_by_action(context.invocation_id)
                        if access_needs_approval(access, saved):
                            _, _, digest = validate_and_normalize(arguments, self.spec.input_schema)
                            if (
                                approval is None
                                or approval.status is not ApprovalStatus.APPROVED
                                or approval.principal_id != context.principal.principal_id
                                or approval.tenant_id != context.tenant_id
                                or approval.tool_name != self.spec.name
                                or approval.normalized_arguments_hash != digest
                            ):
                                return _failure(
                                    "job.approval_required",
                                    "Approve this job's private access before running it.",
                                )
                        grant_id = publication.ids.new_id()
                        grant = JobGrant(
                            id=grant_id,
                            access=access,
                            created_at=publication.clock.now(),
                            approval_id=None if approval is None else approval.id,
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
            await context.mark_effect_sent()
            result = await self.toolset.execution.run(
                principal=context.principal,
                slug=slug,
                revision_id=UUID(arguments["revision_id"]),
                grant_id=grant_id,
                run_id=context.run_id,
                lease_epoch=context.lease_epoch,
                model=_model(context),
            )
            return _ok(
                {
                    "value": result.value,
                    "log": result.diagnostics,
                    "broker_calls": result.broker_calls,
                    "grant_id": str(grant_id),
                },
                untrusted=True,
            )
        except (KeyError, ValueError, NotFoundError, ConflictError) as exc:
            return _failure("job.invalid_request", str(exc))

    async def approval_view_in_session(
        self, arguments: dict[str, Any], **context: Any
    ) -> ApprovalPresentation:
        return ApprovalPresentation(
            summary="Run this saved job with the listed private inputs and access.",
            arguments=arguments,
        )
