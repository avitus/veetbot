"""Top-level CLI over the shared application services."""

from __future__ import annotations

import asyncio
import importlib
import json
import math
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
from agent_core.bootstrap import (
    build,
    build_notification_worker,
    build_schedule_worker,
    serve_execution_service,
)
from agent_core.config import ConfigurationError
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import (
    ConflictError,
    EvalExpectationError,
    ExportConsentError,
    ExportRedactionError,
    ExportStateError,
    NotFoundError,
    PersonaContentError,
)
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import MemoryEdit, Portability, Sensitivity
from agent_core.domain.messages import AssistantMessage, TextPart
from agent_core.domain.persona import PersonaEntryDraft, PersonaNominationState
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import (
    ApprovalFilters,
    PersistedStreamFrame,
    RunView,
    StreamFrame,
    TextContentBlock,
)
from agent_core.ports.dispatch import WorkerService

RUN_RESERVED_WORDS = frozenset({"get", "events", "cancel", "export"})
DEFAULT_RUN_WAIT_TIMEOUT_SECONDS = 300.0
EVENT_READ_TIMEOUT_SECONDS = 30.0
API_BIND_HOST = "127.0.0.1"


class WorkerRole(StrEnum):
    WORKER = "worker"
    INTERACTIVE = "interactive"
    ASYNC = "async"
    MAINTENANCE = "maintenance"
    SCHEDULE = "schedule"
    NOTIFY = "notify"


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
memory_app = typer.Typer(name="memory", no_args_is_help=True)
persona_app = typer.Typer(name="persona", no_args_is_help=True)
app.add_typer(run_app)
app.add_typer(session_app)
app.add_typer(eval_app)
app.add_typer(approval_app)
app.add_typer(memory_app)
app.add_typer(persona_app)


class _EvalRunnerModule(Protocol):
    def run_selected_sync(
        self,
        repository_root: Path,
        *,
        current_milestone: int,
        tag: str | None,
        case_name: str | None,
    ) -> list[Any]: ...


class _EvalGateModule(Protocol):
    def current_milestone(self, repository_root: Path) -> int: ...

    def maximum_milestone(self, repository_root: Path) -> int: ...

    def collect_status(
        self,
        repository_root: Path,
        *,
        milestone: int,
        area: str | None = None,
    ) -> list[Any]: ...


class _CapabilityModule(Protocol):
    async def run_live_suite(
        self,
        repository_root: Path,
        *,
        suite: str,
        build_ref: str | None,
    ) -> Any | None: ...

    def resolve_build_ref(self, repository_root: Path, explicit: str | None) -> str: ...


class _MemoryFormationEvalModule(Protocol):
    async def run_live_evaluation(
        self,
        repository_root: Path,
        *,
        model_policy: str,
        policy_profile: str,
        build_ref: str,
        output: Path,
    ) -> Any | None: ...


class _MemoryDistillationEvalModule(Protocol):
    async def run_live_evaluation(
        self,
        repository_root: Path,
        *,
        model_policy: str,
        policy_profile: str,
        build_ref: str,
        output: Path,
    ) -> Any | None: ...


class _MemoryBenchmarkModule(Protocol):
    async def run_benchmark(
        self,
        repository_root: Path,
        *,
        deterministic_only: bool,
        model_policy: str,
        policy_profile: str,
        build_ref: str,
        output: Path | None,
        baseline_output: Path | None,
    ) -> Any | None: ...


class _MemoryBenchmarkLiveModule(Protocol):
    def incomplete_run_diagnostics(self, live: Any) -> list[str]: ...


class _MemoryBenchmarkExternalModule(Protocol):
    async def run_external_benchmark(
        self,
        repository_root: Path,
        *,
        dataset: str,
        path: Path,
        sample: int | None,
        seed: int,
        principal_speaker: str,
        deterministic_only: bool,
        model_policy: str,
        policy_profile: str,
        build_ref: str,
        output: Path,
    ) -> Any | None: ...


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
    *,
    wait_timeout_seconds: float,
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
            events: list[PersistedStreamFrame] = []
            try:
                async with asyncio.timeout(wait_timeout_seconds):
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
                    stream = cast(
                        AsyncGenerator[StreamFrame, None],
                        composition.services.runs.stream(composition.principal, run_id, None),
                    )
                    async with aclosing(stream):
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
    wait_timeout_seconds: Annotated[
        float,
        typer.Option(
            "--wait-timeout",
            help="Seconds to wait for the run to reach a terminal or suspended state.",
        ),
    ] = DEFAULT_RUN_WAIT_TIMEOUT_SECONDS,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the run record as JSON.")
    ] = False,
) -> None:
    """Execute one run through the shared RunService."""

    if session_id is not None and model_policy is not None:
        raise typer.BadParameter("--model-policy can only be used for a new session")
    if not math.isfinite(wait_timeout_seconds) or wait_timeout_seconds <= 0:
        raise typer.BadParameter("--wait-timeout must be greater than zero")
    try:
        run, events = asyncio.run(
            _submit(
                prompt,
                session_id,
                idempotency_key,
                model_policy,
                wait_timeout_seconds=wait_timeout_seconds,
            )
        )
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
                async with asyncio.timeout(EVENT_READ_TIMEOUT_SECONDS), aclosing(stream):
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
    if role is WorkerRole.NOTIFY:
        async with build_notification_worker() as notification_service:
            await _run_worker_service(notification_service)
        return
    if role is WorkerRole.SCHEDULE:
        async with build_schedule_worker() as schedule_service:
            await _run_worker_service(schedule_service)
        return
    async with build(storage="postgres") as composition:
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        service: WorkerService
        if role in {WorkerRole.WORKER, WorkerRole.INTERACTIVE}:
            service = composition.worker_factory(worker_id)
        elif role is WorkerRole.ASYNC:
            service = composition.async_worker_factory(worker_id)
        else:
            service = composition.maintenance_factory()
        await _run_worker_service(service)


async def _run_worker_service(service: WorkerService) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, service.stop)
    await service.run_forever()


@app.command("execution-service")
def execution_service_command(
    socket_path: Annotated[
        Path,
        typer.Option("--socket", help="Absolute Unix socket used by application workers."),
    ] = Path("/run/veetbot/execution.sock"),
) -> None:
    """Serve sandbox lifecycle without loading application credentials."""

    if not socket_path.is_absolute():
        raise typer.BadParameter("execution service socket must be absolute", param_hint="--socket")
    asyncio.run(serve_execution_service(socket_path))


@app.command("worker")
def worker_command(
    role: Annotated[
        WorkerRole,
        typer.Option(
            "--role",
            help=(
                "Process role: interactive, async, maintenance, schedule, notify, or legacy worker."
            ),
        ),
    ] = WorkerRole.WORKER,
) -> None:
    """Execute an interactive, async, maintenance, schedule, notify, or legacy worker role."""

    try:
        asyncio.run(_serve_worker(role))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc


async def _serve_api() -> None:
    async with build(storage="postgres") as composition:
        api = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        server = uvicorn.Server(uvicorn.Config(api, host=API_BIND_HOST, port=8000, log_config=None))
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


async def _memory_list(include_inactive: bool, session_id: UUID | None, limit: int) -> list[Any]:
    async with build(storage="postgres") as composition:
        return await composition.memory.list_memories(
            include_inactive=include_inactive,
            session_id=session_id,
            limit=limit,
        )


async def _memory_get(belief_id: UUID) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.memory.get_memory(belief_id)


@memory_app.command("get")
def memory_get(belief_id: UUID) -> None:
    """Inspect one governed memory, including formation and source identifiers."""

    try:
        row = asyncio.run(_memory_get(belief_id))
    except (ConfigurationError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(row.model_dump_json())


@memory_app.command("list")
def memory_list(
    include_inactive: Annotated[
        bool,
        typer.Option("--include-inactive", help="Include superseded and expired beliefs."),
    ] = False,
    session_id: Annotated[
        UUID | None,
        typer.Option("--session", help="Restrict beliefs to one source session."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=200)] = 100,
) -> None:
    """Inspect the authenticated principal's governed memories."""

    try:
        rows = asyncio.run(_memory_list(include_inactive, session_id, limit))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    typer.echo(json.dumps([row.model_dump(mode="json") for row in rows], default=str))


async def _memory_edit(belief_id: UUID, edit: MemoryEdit) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.memory.edit(belief_id, edit)


@memory_app.command("edit")
def memory_edit(
    belief_id: UUID,
    statement: Annotated[str, typer.Option("--statement", help="Replacement statement.")],
    sensitivity: Annotated[Sensitivity | None, typer.Option()] = None,
    portability: Annotated[Portability | None, typer.Option()] = None,
    scope: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Edit one belief through an auditable, tenant-scoped correction path."""

    try:
        row = asyncio.run(
            _memory_edit(
                belief_id,
                MemoryEdit(
                    statement=statement,
                    sensitivity=sensitivity,
                    portability=portability,
                    scope=scope,
                ),
            )
        )
    except (ConfigurationError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(row.model_dump_json())


async def _memory_delete(belief_id: UUID) -> None:
    async with build(storage="postgres") as composition:
        await composition.memory.delete(belief_id)


@memory_app.command("delete")
def memory_delete(belief_id: UUID) -> None:
    """Delete one belief and retain only its non-recallable rejection tombstone."""

    try:
        asyncio.run(_memory_delete(belief_id))
    except (ConfigurationError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({"id": str(belief_id)}))


async def _memory_formations(session_id: UUID | None, limit: int) -> list[Any]:
    async with build(storage="postgres") as composition:
        return await composition.memory.list_consolidations(
            session_id=session_id,
            limit=limit,
        )


@memory_app.command("formations")
def memory_formations(
    session_id: Annotated[
        UUID | None,
        typer.Option("--session", help="Restrict formation runs to one source session."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=200)] = 100,
) -> None:
    """Inspect formation runs, candidate outcomes, policies, and watermarks."""

    try:
        rows = asyncio.run(_memory_formations(session_id, limit))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    typer.echo(json.dumps([row.model_dump(mode="json") for row in rows], default=str))


async def _memory_diagnose(session_id: UUID) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.memory.diagnose(session_id)


@memory_app.command("diagnose")
def memory_diagnose(
    session_id: Annotated[
        UUID,
        typer.Option("--session", help="Diagnose formation for one source session."),
    ],
) -> None:
    """Inspect flags, watermarks, provider attempts, audits, and formed beliefs."""

    try:
        diagnosis = asyncio.run(_memory_diagnose(session_id))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    except NotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    payload = diagnosis if isinstance(diagnosis, dict) else diagnosis.model_dump(mode="json")
    typer.echo(json.dumps(payload, default=str))


async def _memory_replay(session_id: UUID) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.memory.replay(session_id)


@memory_app.command("replay")
def memory_replay(
    session_id: Annotated[
        UUID,
        typer.Option("--session", help="Replay formation for one source session."),
    ],
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Confirm the auditable formation replay."),
    ] = False,
) -> None:
    """Reprocess original session evidence through the governed formation path."""

    if not confirm:
        typer.echo("memory replay requires --confirm", err=True)
        raise typer.Exit(2)
    try:
        result = asyncio.run(_memory_replay(session_id))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    except NotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(result.model_dump_json())


async def _memory_trace(trace_id: UUID) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.memory.get_recall_trace(trace_id)


@memory_app.command("trace")
def memory_trace(trace_id: UUID) -> None:
    """Inspect one principal-scoped retrieval trace and its ranked beliefs."""

    try:
        row = asyncio.run(_memory_trace(trace_id))
    except (ConfigurationError, NotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(row.model_dump_json())


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
            Path.cwd(), current_milestone=9, tag=tag, case_name=case_name
        )
    except (EvalExpectationError, ImportError, OSError, ValueError) as exc:
        typer.echo(f"evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    for result in results:
        typer.echo(f"pass {result.case.name}", err=True)
    typer.echo(f"{len(results)} passed")


@eval_app.command("gates")
def eval_gates(
    milestone: Annotated[
        int | None,
        typer.Option("--milestone", min=0, help="Treat gates through MILESTONE as active."),
    ] = None,
    area: Annotated[
        str | None,
        typer.Option("--area", help="Limit output to one registry area, such as policy."),
    ] = None,
) -> None:
    """Execute active registered gates and show later gates as pending."""

    try:
        module = cast(_EvalGateModule, importlib.import_module("agent_core.evals.gates"))
        if milestone is not None:
            maximum_milestone = module.maximum_milestone(Path.cwd())
            if milestone > maximum_milestone:
                raise typer.BadParameter(
                    f"must not exceed the registry maximum of {maximum_milestone}",
                    param_hint="--milestone",
                )
        selected_milestone = (
            module.current_milestone(Path.cwd()) if milestone is None else milestone
        )
        statuses = module.collect_status(Path.cwd(), milestone=selected_milestone, area=area)
    except (ImportError, OSError, ValueError) as exc:
        typer.echo(f"gate evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    failures = 0
    for gate_milestone in sorted({status.milestone for status in statuses}):
        rows = [status for status in statuses if status.milestone == gate_milestone]
        counts = {
            outcome: sum(status.outcome == outcome for status in rows)
            for outcome in ("pass", "fail", "pending")
        }
        typer.echo(
            f"Milestone {gate_milestone}: {len(rows)} "
            f"{'gate' if len(rows) == 1 else 'gates'}  "
            f"{counts['pass']} pass  {counts['fail']} fail  {counts['pending']} pending"
        )
        for status in rows:
            suffix = f" ({status.detail})" if status.detail else ""
            typer.echo(f"  {status.id:<46} {status.outcome:<7} {status.kind}{suffix}")
        failures += counts["fail"]
    if failures:
        raise typer.Exit(1)


@eval_app.command("capability")
def eval_capability(
    suite: Annotated[
        str,
        typer.Option("--suite", help="Run one configured live capability suite."),
    ],
    build_ref: Annotated[
        str | None,
        typer.Option("--build-ref", help="Commit or build identifier; defaults to CI or Git."),
    ] = None,
) -> None:
    """Run repeated, judged live scenarios and persist their distributions."""

    try:
        module = cast(_CapabilityModule, importlib.import_module("agent_core.evals.capability"))
        result = asyncio.run(module.run_live_suite(Path.cwd(), suite=suite, build_ref=build_ref))
    except (ConfigurationError, ImportError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"capability evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if result is None:
        typer.echo("skipped: set RUN_LIVE_MODEL_TESTS=1 to run live capability scenarios")
        return
    typer.echo(
        json.dumps(
            {
                "suite": result.suite,
                "build_ref": result.build_ref,
                "repeats": len(result.runs),
                "mean": None if result.mean is None else str(result.mean),
                "floor": None if result.floor is None else str(result.floor),
                "variance": None if result.variance is None else str(result.variance),
                "ceiling_hits": result.ceiling_hits,
                "policy_failures": result.policy_failures,
                "release_blocked": result.release_blocked,
                "stopped_by": result.stopped_by,
            },
            sort_keys=True,
        )
    )
    if result.release_blocked:
        raise typer.Exit(1)


@eval_app.command("memory-formation")
def eval_memory_formation(
    model_policy: Annotated[
        str,
        typer.Option("--model-policy", help="Evaluate one declared model policy."),
    ],
    policy_profile: Annotated[
        str,
        typer.Option("--policy-profile", help="Evaluate one policy profile."),
    ],
    build_ref: Annotated[
        str,
        typer.Option("--build-ref", help="Commit or immutable build identifier."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="Write passing activation evidence to this path."),
    ],
) -> None:
    """Compare provider-assisted formation with the deterministic baseline."""

    try:
        module = cast(
            _MemoryFormationEvalModule,
            importlib.import_module("agent_core.evals.memory_formation"),
        )
        result = asyncio.run(
            module.run_live_evaluation(
                Path.cwd(),
                model_policy=model_policy,
                policy_profile=policy_profile,
                build_ref=build_ref,
                output=output,
            )
        )
    except (ConfigurationError, ImportError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"memory-formation evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if result is None:
        typer.echo("skipped: set RUN_LIVE_MODEL_TESTS=1 to evaluate memory formation")
        return
    typer.echo(result.model_dump_json())
    if not result.passed:
        typer.echo(f"memory-formation evaluation failed: {result.failure_summary}", err=True)
        raise typer.Exit(1)


@eval_app.command("memory-distillation")
def eval_memory_distillation(
    model_policy: Annotated[
        str,
        typer.Option("--model-policy", help="Evaluate one declared model policy."),
    ],
    policy_profile: Annotated[
        str,
        typer.Option("--policy-profile", help="Evaluate one policy profile."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="Write passing formation@9 evidence to this path."),
    ],
    build_ref: Annotated[
        str | None,
        typer.Option(
            "--build-ref",
            help="Full commit sha; resolved from CI or git HEAD when omitted.",
        ),
    ] = None,
) -> None:
    """Compare formation@7, formation@8, and formation@9 on corpus v3."""

    try:
        module = cast(
            _MemoryDistillationEvalModule,
            importlib.import_module("agent_core.evals.memory_distillation"),
        )
        capability = cast(_CapabilityModule, importlib.import_module("agent_core.evals.capability"))
        result = asyncio.run(
            module.run_live_evaluation(
                Path.cwd(),
                model_policy=model_policy,
                policy_profile=policy_profile,
                build_ref=capability.resolve_build_ref(Path.cwd(), build_ref),
                output=output,
            )
        )
    except (ConfigurationError, ImportError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"memory-distillation evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if result is None:
        typer.echo("skipped: set RUN_LIVE_MODEL_TESTS=1 to evaluate memory distillation")
        return
    typer.echo(result.model_dump_json())
    if not result.passed:
        typer.echo(f"memory-distillation evaluation failed: {result.failure_summary}", err=True)
        raise typer.Exit(1)


@eval_app.command("memory-benchmark")
def eval_memory_benchmark(
    deterministic_only: Annotated[
        bool,
        typer.Option(
            "--deterministic-only/--no-deterministic-only",
            help="Run only the deterministic arm; the live arm is opt-in.",
        ),
    ] = True,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write passing live evidence to this path."),
    ] = None,
    write_baseline: Annotated[
        Path | None,
        typer.Option("--write-baseline", help="Record this run as the deterministic baseline."),
    ] = None,
    build_ref: Annotated[
        str | None,
        typer.Option("--build-ref", help="Commit or build identifier; defaults to CI or Git."),
    ] = None,
    model_policy: Annotated[
        str,
        typer.Option("--model-policy", help="Evaluate one declared model policy."),
    ] = "balanced",
    policy_profile: Annotated[
        str,
        typer.Option("--policy-profile", help="Evaluate one policy profile."),
    ] = "default",
    external: Annotated[
        str | None,
        typer.Option(
            "--external",
            help="Run a local dataset instead: longmemeval, locomo, or halumem.",
        ),
    ] = None,
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Local dataset file; it is never committed."),
    ] = None,
    sample: Annotated[
        int | None,
        typer.Option("--sample", help="Draw this many instances from the dataset."),
    ] = None,
    seed: Annotated[
        int,
        typer.Option("--seed", help="Seed the dataset sample so a run repeats."),
    ] = 0,
    principal_speaker: Annotated[
        str,
        typer.Option(
            "--principal-speaker",
            help="Which LoCoMo speaker is the formation source: a or b.",
        ),
    ] = "a",
) -> None:
    """Measure what memory forms across sessions and recalls when probed."""

    if external is not None:
        _run_external_memory_benchmark(
            dataset=external,
            path=path,
            sample=sample,
            seed=seed,
            principal_speaker=principal_speaker,
            deterministic_only=deterministic_only,
            model_policy=model_policy,
            policy_profile=policy_profile,
            build_ref=build_ref,
            output=output,
            write_baseline=write_baseline,
        )
        return
    for name, value in (("--path", path), ("--sample", sample)):
        if value is not None:
            raise typer.BadParameter(
                f"{name} belongs to an external dataset run; pass --external too",
                param_hint=name,
            )
    if deterministic_only:
        if output is not None:
            raise typer.BadParameter(
                "--output belongs to the live arm; "
                "the deterministic arm records a baseline with --write-baseline",
                param_hint="--output",
            )
    else:
        if output is None:
            raise typer.BadParameter(
                "--output is required for a live run, which publishes its evidence there",
                param_hint="--output",
            )
        if build_ref is None:
            raise typer.BadParameter(
                "--build-ref is required for a live run and is never guessed from the tree",
                param_hint="--build-ref",
            )
    try:
        capability = cast(_CapabilityModule, importlib.import_module("agent_core.evals.capability"))
        module = cast(
            _MemoryBenchmarkModule,
            importlib.import_module("agent_core.evals.memory_benchmark_driver"),
        )
        result = asyncio.run(
            module.run_benchmark(
                Path.cwd(),
                deterministic_only=deterministic_only,
                model_policy=model_policy,
                policy_profile=policy_profile,
                build_ref=capability.resolve_build_ref(Path.cwd(), build_ref),
                output=output,
                baseline_output=write_baseline,
            )
        )
    except (ConfigurationError, ImportError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"memory-benchmark evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if result is None:
        typer.echo("skipped: set RUN_LIVE_MODEL_TESTS=1 to run the live memory benchmark")
        return
    typer.echo(result.model_dump_json())
    if not result.passed:
        for line in _incomplete_run_diagnostics(getattr(result, "live", None)):
            typer.echo(line, err=True)
        typer.echo(f"memory-benchmark evaluation failed: {result.failure_summary}", err=True)
        raise typer.Exit(1)


async def _persona_get() -> Any:
    async with build(storage="postgres") as composition:
        return await composition.services.persona.get(composition.principal)


@persona_app.command("show")
def persona_show() -> None:
    """Print the authenticated principal's persona document head."""

    try:
        view = asyncio.run(_persona_get())
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    typer.echo(view.model_dump_json())


async def _persona_edit(expected_version: int, entries: list[str], keep_affirmed: bool) -> Any:
    async with build(storage="postgres") as composition:
        drafts: list[PersonaEntryDraft] = []
        if keep_affirmed:
            head = await composition.services.persona.get(composition.principal)
            drafts.extend(
                PersonaEntryDraft(
                    text=entry.text,
                    sensitivity=entry.sensitivity,
                    source_belief_id=entry.source_belief_id,
                )
                for entry in head.entries
                if entry.source_belief_id is not None
            )
        drafts.extend(PersonaEntryDraft(text=text) for text in entries)
        return await composition.services.persona.update(
            composition.principal,
            expected_version=expected_version,
            entries=drafts,
        )


@persona_app.command("edit")
def persona_edit(
    expected_version: Annotated[
        int, typer.Option("--expected-version", min=0, help="The current head version.")
    ],
    entry: Annotated[
        list[str],
        typer.Option("--entry", help="One owner-typed persona entry; repeatable."),
    ],
    keep_affirmed: Annotated[
        bool,
        typer.Option(
            "--keep-affirmed/--drop-affirmed",
            help="Carry the head's affirmed entries forward (the default).",
        ),
    ] = True,
) -> None:
    """Replace the persona's owner-typed entries behind the version guard."""

    try:
        view = asyncio.run(_persona_edit(expected_version, entry, keep_affirmed))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    except PersonaContentError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except ConflictError as exc:
        typer.echo(f"version conflict: {exc}", err=True)
        raise typer.Exit(3) from exc
    typer.echo(view.model_dump_json())


async def _persona_history(limit: int) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.services.persona.history(composition.principal, limit=limit)


@persona_app.command("history")
def persona_history(limit: Annotated[int, typer.Option(min=1, max=200)] = 20) -> None:
    """List persona versions, newest first."""

    try:
        page = asyncio.run(_persona_history(limit))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    typer.echo(json.dumps([row.model_dump(mode="json") for row in page.items], default=str))


async def _persona_nominations(state: PersonaNominationState | None) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.services.persona.nominations(composition.principal, state=state)


@persona_app.command("nominations")
def persona_nominations(
    state: Annotated[
        PersonaNominationState | None,
        typer.Option("--state", help="Filter by nomination state."),
    ] = None,
) -> None:
    """List persona nominations awaiting or past the owner's verdict."""

    try:
        page = asyncio.run(_persona_nominations(state))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    typer.echo(json.dumps([row.model_dump(mode="json") for row in page.items], default=str))


async def _persona_affirm(nomination_id: UUID) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.services.persona.affirm(composition.principal, nomination_id)


@persona_app.command("affirm")
def persona_affirm(nomination_id: UUID) -> None:
    """Affirm one nomination into the persona at USER authority."""

    try:
        view = asyncio.run(_persona_affirm(nomination_id))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    except NotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except PersonaContentError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except ConflictError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc
    typer.echo(view.model_dump_json())


async def _persona_decline(nomination_id: UUID) -> Any:
    async with build(storage="postgres") as composition:
        return await composition.services.persona.decline(composition.principal, nomination_id)


@persona_app.command("decline")
def persona_decline(nomination_id: UUID) -> None:
    """Decline one nomination, durably: the belief is never raised again."""

    try:
        view = asyncio.run(_persona_decline(nomination_id))
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(4) from exc
    except NotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except ConflictError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc
    typer.echo(view.model_dump_json())


def _incomplete_run_diagnostics(live: Any) -> list[str]:
    """Describe every run the live arm left incomplete, content-free.

    The one-line summary says how many runs ended without an answer; these say
    which, why, and at what cost, so a failed live arm is diagnosable from the
    command's own output rather than only from the metrics document.  A
    deterministic run has no live arm and prints nothing extra.
    """

    if live is None:
        return []
    module = cast(
        _MemoryBenchmarkLiveModule,
        importlib.import_module("agent_core.evals.memory_benchmark_live"),
    )
    return module.incomplete_run_diagnostics(live)


def _run_external_memory_benchmark(
    *,
    dataset: str,
    path: Path | None,
    sample: int | None,
    seed: int,
    principal_speaker: str,
    deterministic_only: bool,
    model_policy: str,
    policy_profile: str,
    build_ref: str | None,
    output: Path | None,
    write_baseline: Path | None,
) -> None:
    """Run one locally supplied dataset and print its informational metrics.

    The dataset arm publishes metrics rather than activation evidence, so it
    takes an `--output` in either arm and records no baseline: the authored
    corpus is the arm a baseline is compared against.
    """

    if dataset not in {"longmemeval", "locomo", "halumem"}:
        raise typer.BadParameter(
            f"unknown dataset {dataset!r}; expected longmemeval, locomo, or halumem",
            param_hint="--external",
        )
    if path is None:
        raise typer.BadParameter(
            "--path is required for an external dataset, which is read locally and never committed",
            param_hint="--path",
        )
    if output is None:
        raise typer.BadParameter(
            "--output is required for an external dataset, which publishes its metrics there",
            param_hint="--output",
        )
    if write_baseline is not None:
        raise typer.BadParameter(
            "an external dataset records no baseline; the authored corpus does",
            param_hint="--write-baseline",
        )
    if principal_speaker not in {"a", "b"}:
        raise typer.BadParameter(
            f"unknown principal speaker {principal_speaker!r}; expected a or b",
            param_hint="--principal-speaker",
        )
    try:
        capability = cast(_CapabilityModule, importlib.import_module("agent_core.evals.capability"))
        module = cast(
            _MemoryBenchmarkExternalModule,
            importlib.import_module("agent_core.evals.memory_benchmark_external"),
        )
        result = asyncio.run(
            module.run_external_benchmark(
                Path.cwd(),
                dataset=dataset,
                path=path,
                sample=sample,
                seed=seed,
                principal_speaker=principal_speaker,
                deterministic_only=deterministic_only,
                model_policy=model_policy,
                policy_profile=policy_profile,
                build_ref=capability.resolve_build_ref(Path.cwd(), build_ref),
                output=output,
            )
        )
    except (ConfigurationError, ImportError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"memory-benchmark evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if result is None:
        typer.echo("skipped: set RUN_LIVE_MODEL_TESTS=1 to run the live memory benchmark")
        return
    typer.echo(result.model_dump_json())
