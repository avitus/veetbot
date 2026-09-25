"""People operator commands use the same owner, ceilings, and CAS services as HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Coroutine
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

import typer

from agent_core.application.services import (
    PeopleErasureOperations,
    PeopleService,
    SessionService,
)
from agent_core.config import ConfigurationError
from agent_core.domain.agents import Principal
from agent_core.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ToolValidationError,
)
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleErasure
from agent_core.domain.people_imports import PeopleImportRequest
from agent_core.domain.people_public import (
    IdentityOperationView,
    PeoplePageView,
    PeopleSectionPageView,
    PersonProfileView,
)
from agent_core.domain.people_views import (
    PeopleForgetRequest,
    PeopleIdentityRequest,
    PeopleRepairReport,
    PeopleSectionQuery,
)


class _DirectoryRepair(Protocol):
    async def run(
        self, principal: Principal, *, confirm: bool, session_id: UUID | None = None
    ) -> PeopleRepairReport: ...


class _PeopleServices(Protocol):
    @property
    def people(self) -> PeopleService | None: ...

    @property
    def sessions(self) -> SessionService: ...


class _Composition(Protocol):
    @property
    def people_erasure(self) -> PeopleErasureOperations: ...

    @property
    def people_repair(self) -> _DirectoryRepair | None: ...

    @property
    def principal(self) -> Principal: ...

    @property
    def services(self) -> _PeopleServices: ...


class _Build(Protocol):
    def __call__(
        self, *, storage: Literal["memory", "postgres"]
    ) -> AbstractAsyncContextManager[_Composition]: ...


_build: _Build | None = None


def configure(factory: _Build) -> None:
    """The CLI composition root supplies its shared application factory."""
    global _build
    _build = factory


def build(*, storage: Literal["memory", "postgres"]) -> AbstractAsyncContextManager[_Composition]:
    if _build is None:
        raise ConfigurationError("People CLI composition is not configured")
    return _build(storage=storage)


app = typer.Typer(name="people", no_args_is_help=True)
Owner = Annotated[str, typer.Option("--owner", help="Exact configured tenant/principal identity.")]
RequestFile = Annotated[
    Path, typer.Option("--request", help="JSON request, including preview/apply revision.")
]
Key = Annotated[
    str, typer.Option("--key", help="Keep this idempotency key when retrying a request.")
]
Ceiling = Annotated[Sensitivity, typer.Option("--ceiling")]


def emit(operation: Coroutine[Any, Any, object]) -> None:
    try:
        result = asyncio.run(operation)
        typer.echo(json.dumps(result, default=str))
    except (
        ConfigurationError,
        AuthorizationError,
        ConflictError,
        NotFoundError,
        ToolValidationError,
        ValueError,
        OSError,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


async def read(
    action: str,
    person_id: UUID | None,
    ceiling: Sensitivity,
    text: str | None = None,
    cursor: str | None = None,
) -> object:
    async with build(storage="postgres") as composition:
        service = composition.services.people
        if service is None:
            raise NotFoundError("People is disabled")
        owner = composition.principal
        if action == "list":
            result = await service.list(owner, ceiling=ceiling, text=text, cursor=cursor)
            return PeoplePageView.model_validate(result).model_dump(mode="json")
        assert person_id is not None
        if action == "history":
            section = await service.section(
                owner,
                person_id,
                PeopleSectionQuery(section="history", cursor=cursor),
                ceiling=ceiling,
            )
            return PeopleSectionPageView.model_validate(section).model_dump(mode="json")
        profile = await service.get(owner, person_id, ceiling=ceiling)
        if action == "diagnose":
            return {
                "person_id": str(person_id),
                "revision": profile.person.revision,
                "identity_state": profile.person.state,
                "visible_aliases": len(profile.aliases),
                "visible_facts": len(profile.facts),
                "visible_interactions": len(profile.history),
                "visible_relationships": len(profile.relationships),
                "visible_commitments": len(profile.commitments),
                "truncated": profile.truncated,
                "coverage": profile.coverage,
                "injected": "Use the run's recall trace to inspect actual injection.",
            }
        return PersonProfileView.model_validate(profile).model_dump(mode="json")


@app.command("list")
def list_people(ceiling: Ceiling, text: str | None = None, cursor: str | None = None) -> None:
    """List visible people; continue using the returned cursor."""
    emit(read("list", None, ceiling, text, cursor))


async def link_legacy(owner: str, limit: int, cursor: str | None) -> object:
    async with build(storage="postgres") as composition:
        principal = composition.principal
        if owner != f"{principal.tenant_id}/{principal.principal_id}":
            raise AuthorizationError("--owner must match the configured tenant/principal")
        if composition.services.people is None:
            raise NotFoundError("People is disabled")
        result = await composition.services.people.link_existing(
            principal, limit=limit, cursor=cursor
        )
        return result.model_dump(mode="json")


@app.command("link-existing")
def link_existing(
    owner: Owner,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 100,
    cursor: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Link one bounded page of existing owner beliefs; repeat using its returned cursor."""
    emit(link_legacy(owner, limit, cursor))


async def repair_directory_report(owner: str, confirm: bool) -> object:
    async with build(storage="postgres") as composition:
        principal = composition.principal
        if owner != f"{principal.tenant_id}/{principal.principal_id}":
            raise AuthorizationError("--owner must match the configured tenant/principal")
        repair = composition.people_repair
        if repair is None:
            raise NotFoundError("People is disabled")
        session_id = None
        if confirm:
            # The audit trail and the owner-confirmed aliases live in a People
            # management session, which the conversation list hides.
            session = await composition.services.sessions.create(
                principal, "general", {"purpose": "people-management"}
            )
            session_id = session.id
        report = await repair.run(principal, confirm=confirm, session_id=session_id)
        return report.model_dump(mode="json")


@app.command("repair-directory")
def repair_directory(
    owner: Owner,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Apply the repair. Without it, only preview.")
    ] = False,
) -> None:
    """Keep only people you know or write to (ADR-0121); previews unless --confirm."""
    emit(repair_directory_report(owner, confirm))


async def dedupe_report(owner: str, confirm: bool) -> object:
    async with build(storage="postgres") as composition:
        principal = composition.principal
        if owner != f"{principal.tenant_id}/{principal.principal_id}":
            raise AuthorizationError("--owner must match the configured tenant/principal")
        service = composition.services.people
        if service is None:
            raise NotFoundError("People is disabled")
        report = await service.dedupe(principal, apply=confirm)
        return report.model_dump(mode="json")


@app.command("dedupe")
def dedupe(
    owner: Owner,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Merge and record suggestions. Without it, preview.")
    ] = False,
) -> None:
    """Merge duplicates on decisive evidence and ask about the rest (ADR-0125)."""
    emit(dedupe_report(owner, confirm))


@app.command("get")
def get_person(person_id: UUID, ceiling: Ceiling) -> None:
    """Read identity, relationships, facts, history and commitments."""
    emit(read("get", person_id, ceiling))


@app.command("history")
def history(person_id: UUID, ceiling: Ceiling, cursor: str | None = None) -> None:
    """Read chronological evidence with bounded pagination."""
    emit(read("history", person_id, ceiling, cursor=cursor))


@app.command("diagnose")
def diagnose(person_id: UUID, ceiling: Ceiling) -> None:
    """Show visible stored counts and the remaining coverage limits."""
    emit(read("diagnose", person_id, ceiling))


async def write(
    action: str,
    owner: str,
    request_path: Path,
    key: str,
    ceiling: Sensitivity,
    person_id: UUID | None = None,
) -> object:
    payload = json.loads(request_path.read_text())
    async with build(storage="postgres") as composition:
        principal = composition.principal
        if owner != f"{principal.tenant_id}/{principal.principal_id}":
            raise AuthorizationError("--owner must match the configured tenant/principal")
        service = composition.services.people
        if service is None:
            raise NotFoundError("People is disabled")
        if action == "import":
            imported = await service.create_import(
                principal, PeopleImportRequest.model_validate(payload), key=key, ceiling=ceiling
            )
            return imported.model_dump(mode="json")
        if action == "forget":
            assert person_id is not None
            erased = await service.forget(
                principal,
                person_id,
                PeopleForgetRequest.model_validate(payload),
                key=key,
                ceiling=ceiling,
            )
            return erased.model_dump(mode="json")
        request = PeopleIdentityRequest.model_validate(payload)
        if request.operation not in {action, "apply"}:
            raise ToolValidationError("request operation differs from the selected command")
        result = await service.identity_operation(principal, request, key=key, ceiling=ceiling)
        return IdentityOperationView.model_validate(result).model_dump(mode="json")


@app.command("merge")
def merge(owner: Owner, request: RequestFile, key: Key, ceiling: Ceiling) -> None:
    """Preview or apply a revision-bound merge from a reviewed JSON request."""
    emit(write("merge", owner, request, key, ceiling))


@app.command("split")
def split(owner: Owner, request: RequestFile, key: Key, ceiling: Ceiling) -> None:
    """Preview or apply movement of selected evidence to another identity."""
    emit(write("split", owner, request, key, ceiling))


@app.command("undo")
def undo(owner: Owner, request: RequestFile, key: Key, ceiling: Ceiling) -> None:
    """Preview or apply the guarded inverse of an identity repair."""
    emit(write("undo", owner, request, key, ceiling))


@app.command("forget")
def forget(person_id: UUID, owner: Owner, request: RequestFile, key: Key, ceiling: Ceiling) -> None:
    """Preview or apply erasure; original source messages remain separate."""
    emit(write("forget", owner, request, key, ceiling, person_id))


@app.command("import")
def import_history(owner: Owner, request: RequestFile, key: Key, ceiling: Ceiling) -> None:
    """Preview or apply a date-bounded import with a finite budget."""
    emit(write("import", owner, request, key, ceiling))


@app.command("import-status")
def import_status(job_id: UUID, ceiling: Ceiling) -> None:
    async def inspect() -> object:
        async with build(storage="postgres") as composition:
            if composition.services.people is None:
                raise NotFoundError("People is disabled")
            result = await composition.services.people.get_import(
                composition.principal, job_id, ceiling=ceiling
            )
            return result.model_dump(mode="json")

    emit(inspect())


async def export_erasure_receipt(owner: str, receipt_id: UUID, output: Path) -> object:
    async with build(storage="postgres") as composition:
        principal = composition.principal
        if owner != f"{principal.tenant_id}/{principal.principal_id}":
            raise AuthorizationError("--owner must match the configured tenant/principal")
        root, parts = await composition.people_erasure.export(principal, receipt_id)
        raw = (
            json.dumps(
                {
                    "receipt": root.model_dump(mode="json"),
                    "parts": [part.model_dump(mode="json") for part in parts],
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if len(raw) > 64 * 1024 * 1024:
            raise ToolValidationError("erasure archive exceeds the 64 MiB operator limit")
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return {
            "path": str(output.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "receipt_id": str(root.id),
            "state": root.state,
        }


@app.command("export-erasure")
def export_erasure(
    receipt_id: UUID,
    owner: Owner,
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Export one applied, owner-bound erasure receipt for signed restore recovery."""
    emit(export_erasure_receipt(owner, receipt_id, output))


async def restore_erasure_receipt(
    owner: str,
    path: Path,
    sha256: str,
    session_id: UUID,
) -> object:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ToolValidationError("--sha256 must be the digest from the verified signed manifest")
    with path.open("rb") as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha256:
        raise ToolValidationError("erasure archive differs from the verified digest or size limit")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or set(data) != {"receipt", "parts"}
        or not isinstance(data["parts"], list)
    ):
        raise ToolValidationError(
            "erasure archive requires an exact receipt and its complete pages"
        )
    receipt = PeopleErasure.model_validate(data["receipt"])
    parts = [PeopleErasure.model_validate(part) for part in data["parts"]]
    async with build(storage="postgres") as composition:
        principal = composition.principal
        if owner != f"{principal.tenant_id}/{principal.principal_id}":
            raise AuthorizationError("--owner must match the configured tenant/principal")
        service = composition.people_erasure
        result = await service.reapply(principal, receipt, parts, session_id=session_id)
        if result.state == "cleanup_pending":
            result = await service.get(principal, result.id, ceiling=Sensitivity.RESTRICTED)
        return result.model_dump(mode="json")


@app.command("restore-erasure")
def restore_erasure(
    owner: Owner,
    receipt: Annotated[Path, typer.Option("--receipt")],
    sha256: Annotated[str, typer.Option("--sha256")],
    audit_session: Annotated[UUID, typer.Option("--audit-session")],
    offline: Annotated[
        bool,
        typer.Option(
            "--offline", help="Confirm the restored database has no serving ingress or writers."
        ),
    ] = False,
) -> None:
    """Reapply a newer, signature-verified erasure before reopening an older restore."""
    if not offline:
        raise typer.BadParameter(
            "--offline is required; keep the restored database closed to readers and writers"
        )
    emit(restore_erasure_receipt(owner, receipt, sha256, audit_session))
