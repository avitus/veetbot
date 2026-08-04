"""Top-level CLI over the shared application services."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import socket
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol, cast
from uuid import UUID

import typer
import uvicorn
from pydantic import ValidationError
from typer.core import TyperGroup

from agent_core import __version__
from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.config import ConfigurationError
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import (
    ConflictError,
    EvalExpectationError,
    ExportConsentError,
    ExportRedactionError,
    ExportStateError,
    NotFoundError,
)
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import AssistantMessage, TextPart
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import (
    ApprovalFilters,
    PersistedStreamFrame,
    RunView,
    StreamFrame,
    TextContentBlock,
)

RUN_RESERVED_WORDS = frozenset({"get", "events", "cancel", "export"})
RUN_WAIT_TIMEOUT_SECONDS = 30.0


class WorkerRole(StrEnum):
    WORKER = "worker"
    MAINTENANCE = "maintenance"


class QueuedRunTimeoutError(TimeoutError):
    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"run remains queued: {run_id}")
        self.run_id = run_id


class ReservedRunGroup(TyperGroup):
    """Route non-reserved first arguments to the plan's implicit run submission."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in {*RUN_RESERVED_WORDS, "--help", "-h"}:
            args = ["submit", *args]
        return super().parse_args(ctx, args)


app = typer.Typer(
    name="agent",
    help="Modular general-purpose agent platform.",
    no_args_is_help=True,
)
run_app = typer.Typer(name="run", cls=ReservedRunGroup, no_args_is_help=True)
session_app = typer.Typer(name="session", no_args_is_help=True)
eval_app = typer.Typer(name="eval", no_args_is_help=True)
approval_app = typer.Typer(name="approval", no_args_is_help=True)
app.add_typer(run_app)
app.add_typer(session_app)
app.add_typer(eval_app)
app.add_typer(approval_app)


class _EvalRunnerModule(Protocol):
    def run_selected_sync(
        self,
        repository_root: Path,
        *,
        current_milestone: int,
        tag: str | None,
        case_name: str | None,
    ) -> list[Any]: ...


def version_callback(value: bool) -> None:
    """Print the package version and stop command processing."""

    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the installed version.",
    ),
) -> None:
    """Provide the shared command group."""

    del version


def _progress_lines(events: Sequence[EventEnvelope | PersistedStreamFrame]) -> list[str]:
    lines: list[str] = []
    requested_tool = False
    final_model_turn = False
    for event in events:
        event_type = event.event_type if isinstance(event, EventEnvelope) else event.event
        payload = event.payload if isinstance(event, EventEnvelope) else event.data
        if event_type == "run.queued" and "run created" not in lines:
            lines.append("run created")
        elif event_type == "model.response.completed":
            names = payload.get("tool_names")
            if isinstance(names, list) and names and not requested_tool:
                lines.append(f"model requests {names[0]}")
                requested_tool = True
            elif not names and not final_model_turn:
                lines.append("model produces final response")
                final_model_turn = True
        elif event_type == "tool.call.started" and "tool executes" not in lines:
            lines.append("tool executes")
        elif event_type in {"tool.call.completed", "tool.call.failed"} and (
            "tool result returned to model" not in lines
        ):
            lines.append("tool result returned to model")
        elif event_type == "run.completed" and "run completes" not in lines:
            lines.append("run completes")
    return lines


async def _submit(
    prompt: str,
    session_id: UUID | None,
    idempotency_key: str | None,
    model_policy: str | None,
) -> tuple[RunView, list[PersistedStreamFrame]]:
    async with build(storage="postgres", model_policy=model_policy) as composition:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda _signum, _frame: composition.runs.interrupt())
        try:
            if session_id is None:
                session = await composition.services.sessions.create(
                    composition.principal,
                    "general",
                    {},
                )
                session_id = session.id
            submitted = await composition.services.runs.submit(
                composition.principal,
                session_id,
                [TextContentBlock(text=prompt)],
                idempotency_key,
                None,
            )
            run_id = submitted.run_id
            try:
                async with asyncio.timeout(RUN_WAIT_TIMEOUT_SECONDS):
                    while True:
                        run = await composition.services.runs.get(composition.principal, run_id)
                        if run.status in {
                            RunStatus.COMPLETED,
                            RunStatus.FAILED,
                            RunStatus.CANCELLED,
                            RunStatus.WAITING_FOR_APPROVAL,
                            RunStatus.WAITING_FOR_USER,
                        }:
                            break
                        await composition.clock.sleep(0.05)
            except TimeoutError as exc:
                raise QueuedRunTimeoutError(run_id) from exc
            events: list[PersistedStreamFrame] = []
            stream = cast(
                AsyncGenerator[StreamFrame, None],
                composition.services.runs.stream(composition.principal, run_id, None),
            )
            try:
                async with asyncio.timeout(RUN_WAIT_TIMEOUT_SECONDS), aclosing(stream):
                    async for frame in stream:
                        if not isinstance(frame, PersistedStreamFrame):
                            continue
                        events.append(frame)
                        if frame.event in {
                            "run.waiting_for_approval",
                            "run.waiting_for_user",
                        }:
                            break
            except TimeoutError as exc:
                raise QueuedRunTimeoutError(run_id) from exc
            return run, events
        finally:
            signal.signal(signal.SIGINT, previous_handler)


@run_app.command("submit", hidden=True)
def run_command(
    prompt: Annotated[str, typer.Argument(help="User request to execute.")],
    session_id: Annotated[UUID | None, typer.Option("--session", help="Reuse a session.")] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option("--idempotency-key", help="Deduplicate a retried submission."),
    ] = None,
    model_policy: Annotated[
        str | None,
        typer.Option(
            "--model-policy",
            help="Use a declared model policy for a new session (for example balanced or local).",
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the run record as JSON.")
    ] = False,
) -> None:
    """Execute one run through the shared RunService."""

    if session_id is not None and model_policy is not None:
        raise typer.BadParameter("--model-policy can only be used for a new session")
    try:
        run, events = asyncio.run(_submit(prompt, session_id, idempotency_key, model_policy))
    except QueuedRunTimeoutError as exc:
        typer.echo(f"run did not reach a terminal state: {exc.run_id}", err=True)
        raise typer.Exit(5) from exc
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    for line in _progress_lines(events):
        typer.echo(line, err=True)
    if json_output:
        typer.echo(run.model_dump_json())
    elif run.status is RunStatus.COMPLETED:
        completed = next(
            (event for event in reversed(events) if event.event == "run.completed"),
            None,
        )
        raw_message = None if completed is None else completed.data.get("final_message")
        try:
            message = None if raw_message is None else AssistantMessage.model_validate(raw_message)
        except ValidationError:
            message = None
        text = (
            None
            if message is None
            else "\n".join(part.text for part in message.content if isinstance(part, TextPart))
        )
        typer.echo(text or str(run.id))
    elif run.status in {RunStatus.WAITING_FOR_APPROVAL, RunStatus.WAITING_FOR_USER}:
        typer.echo(str(run.id))
        raise typer.Exit(3)
    else:
        typer.echo(str(run.id), err=True)
        raise typer.Exit(1)


async def _ephemeral_read(run_id: UUID, *, events: bool) -> str:
    async with build(storage="postgres") as composition:
        if events:
            rows: list[PersistedStreamFrame] = []
            stream = cast(
                AsyncGenerator[StreamFrame, None],
                composition.services.runs.stream(composition.principal, run_id, None),
            )
            try:
                async with asyncio.timeout(RUN_WAIT_TIMEOUT_SECONDS), aclosing(stream):
                    async for frame in stream:
                        if isinstance(frame, PersistedStreamFrame):
                            rows.append(frame)
            except TimeoutError:
                pass
            return json.dumps([row.model_dump(mode="json") for row in rows], default=str)
        run = await composition.services.runs.get(composition.principal, run_id)
        return run.model_dump_json()


async def _cancel_run(run_id: UUID) -> RunView:
    async with build(storage="postgres") as composition:
        result = await composition.services.runs.cancel(composition.principal, run_id)
        return result.run


@run_app.command("cancel")
def run_cancel(run_id: UUID) -> None:
    """Request cooperative cancellation through the shared RunService."""

    try:
        typer.echo(asyncio.run(_cancel_run(run_id)).model_dump_json())
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    except (ConflictError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@run_app.command("get")
def run_get(run_id: UUID) -> None:
    """Read a run when the selected composition has durable shared state."""

    try:
        typer.echo(asyncio.run(_ephemeral_read(run_id, events=False)))
    except (ConfigurationError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@run_app.command("events")
def run_events(run_id: UUID) -> None:
    """Read a run's persisted event sequence."""

    try:
        typer.echo(asyncio.run(_ephemeral_read(run_id, events=True)))
    except (ConfigurationError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


async def _export_run(run_id: UUID) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.trajectories.export(run_id)


@run_app.command("export")
def run_export(
    run_id: UUID,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the complete ArtifactRef as JSON.")
    ] = False,
) -> None:
    """Materialize one consent-gated, redacted trajectory artifact."""

    try:
        artifact = asyncio.run(_export_run(run_id))
    except NotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except (
        ConfigurationError,
        ExportConsentError,
        ExportRedactionError,
        ExportStateError,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(artifact.model_dump_json() if json_output else str(artifact.id))


async def _create_session() -> UUID:
    async with build(storage="postgres") as composition:
        session = await composition.services.sessions.create(
            composition.principal,
            "general",
            {},
        )
        return session.id


async def _serve_worker(role: WorkerRole) -> None:
    async with build(storage="postgres") as composition:
        loop = asyncio.get_running_loop()
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        if role is WorkerRole.WORKER:
            service = composition.worker_factory(worker_id)
        else:
            service = composition.maintenance_factory()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, service.stop)
        await service.run_forever()


@app.command("worker")
def worker_command(
    role: Annotated[
        WorkerRole, typer.Option("--role", help="Process role: worker or maintenance.")
    ] = WorkerRole.WORKER,
) -> None:
    """Execute the durable worker or maintenance queue role."""

    try:
        asyncio.run(_serve_worker(role))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc


async def _serve_api() -> None:
    async with build(storage="postgres") as composition:
        host = "127.0.0.1" if composition.settings.auth_mode.value == "dev" else "0.0.0.0"
        api = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        server = uvicorn.Server(uvicorn.Config(api, host=host, port=8000, log_config=None))
        await server.serve()


@app.command("api")
def api_command() -> None:
    """Serve the shared application services over the versioned HTTP API."""

    try:
        asyncio.run(_serve_api())
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc


@session_app.command("create")
def session_create() -> None:
    """Create a session and print only its identifier."""

    try:
        typer.echo(str(asyncio.run(_create_session())))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc


async def _change_export_consent(action: str) -> Any:
    async with build(storage="postgres") as composition:
        if action == "grant":
            return await composition.trajectories.grant_consent()
        return await composition.trajectories.withdraw_consent()


@session_app.command("export-consent")
def session_export_consent(
    action: Annotated[str, typer.Argument(help="Consent action: grant or withdraw.")],
) -> None:
    """Grant prospective export consent or withdraw it globally."""

    if action not in {"grant", "withdraw"}:
        raise typer.BadParameter("action must be grant or withdraw")
    try:
        consent = asyncio.run(_change_export_consent(action))
    except (ConfigurationError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(consent.model_dump_json())


async def _approval_list() -> list[Any]:
    async with build(storage="postgres") as composition:
        rows: list[Any] = []
        cursor: str | None = None
        while True:
            page = await composition.services.approvals.list(
                composition.principal,
                ApprovalFilters(),
                200,
                cursor,
            )
            rows.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return rows


@approval_app.command("list")
def approval_list() -> None:
    """List tenant-scoped pending approvals."""

    try:
        rows = asyncio.run(_approval_list())
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    typer.echo(json.dumps([row.model_dump(mode="json") for row in rows], default=str))


async def _resolve_approval(
    approval_id: UUID,
    resolution: ApprovalResolutionType,
    reason: str | None,
) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.services.approvals.resolve(
            composition.principal,
            approval_id,
            resolution,
            reason,
        )


def _approval_resolution_command(
    approval_id: UUID,
    resolution: ApprovalResolutionType,
    reason: str | None,
) -> None:
    try:
        resolved = asyncio.run(_resolve_approval(approval_id, resolution, reason))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    except (ConflictError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(resolved.model_dump_json())


@approval_app.command("approve")
def approval_approve(
    approval_id: UUID,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Approve one pending action and resume its run."""

    _approval_resolution_command(approval_id, ApprovalResolutionType.APPROVE_ONCE, reason)


@approval_app.command("deny")
def approval_deny(
    approval_id: UUID,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Deny one pending action and resume its run with a denial result."""

    _approval_resolution_command(approval_id, ApprovalResolutionType.DENY, reason)


@eval_app.command("run")
def eval_run(
    suite: Annotated[str, typer.Argument(help="Evaluation suite to run.")] = "deterministic",
    tag: Annotated[str | None, typer.Option("--tag", help="Select one tag.")] = None,
    case_name: Annotated[str | None, typer.Option("--case", help="Select one case name.")] = None,
) -> None:
    """Run checked-in deterministic cases without loading evals in normal startup."""

    if suite != "deterministic":
        raise typer.BadParameter("the current implementation provides only the deterministic suite")
    try:
        module = cast(_EvalRunnerModule, importlib.import_module("agent_core.evals.runner"))
        results = module.run_selected_sync(
            Path.cwd(), current_milestone=5, tag=tag, case_name=case_name
        )
    except (EvalExpectationError, ImportError, OSError, ValueError) as exc:
        typer.echo(f"evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    for result in results:
        typer.echo(f"pass {result.case.name}", err=True)
    typer.echo(f"{len(results)} passed")
