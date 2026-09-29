"""People operator commands expose the same revisioned services and explicit owner."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli.main import app


def test_people_help_lists_every_operator_command() -> None:
    result = CliRunner().invoke(app, ["people", "--help"])
    assert result.exit_code == 0, result.output
    for command in [
        "list",
        "get",
        "history",
        "diagnose",
        "merge",
        "split",
        "undo",
        "forget",
        "import",
        "import-status",
        "link-existing",
        "repair-directory",
        "dedupe",
        "export-erasure",
        "restore-erasure",
    ]:
        assert command in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["merge", "--request", "missing.json", "--key", "merge", "--ceiling", "sensitive"],
        ["repair-directory", "--confirm"],
        ["dedupe", "--confirm"],
    ],
    ids=["merge", "repair-directory", "dedupe"],
)
def test_destructive_people_commands_require_the_owner(args: list[str]) -> None:
    refused = CliRunner().invoke(app, ["people", *args])
    assert refused.exit_code == 2
    assert "--owner" in refused.output


async def test_directory_repair_previews_by_default_and_audits_a_confirmed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from agent_core.cli import people
    from agent_core.domain.errors import AuthorizationError, NotFoundError
    from agent_core.domain.people_views import PeopleRepairReport
    from tests.contract.support import principal

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    audit = UUID(int=91)
    sessions: list[dict[str, object]] = []
    runs: list[tuple[bool, UUID | None]] = []

    class Sessions:
        async def create(
            self, who: object, agent_id: str, metadata: dict[str, object]
        ) -> SimpleNamespace:
            sessions.append(metadata)
            return SimpleNamespace(id=audit)

    class Repair:
        async def run(
            self, who: object, *, confirm: bool, session_id: UUID | None = None
        ) -> PeopleRepairReport:
            runs.append((confirm, session_id))
            return PeopleRepairReport(
                confirmed=confirm,
                retained_mail=0,
                mail_projected=0,
                mail_skipped=0,
                aliases_added=[],
                candidates=[],
                beliefs_deleted=0,
                beliefs_unlinked=0,
                mail_threads_reset=0,
                note="",
            )

    repair: Repair | None = Repair()

    @asynccontextmanager
    async def build(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(
            principal=owner, people_repair=repair, services=SimpleNamespace(sessions=Sessions())
        )

    monkeypatch.setattr(people, "build", build)
    identity = f"{owner.tenant_id}/{owner.principal_id}"
    with pytest.raises(AuthorizationError):
        await people.repair_directory_report("other/owner", True)
    preview = await people.repair_directory_report(identity, False)
    assert isinstance(preview, dict) and preview["confirmed"] is False
    assert sessions == [] and runs == [(False, None)]
    await people.repair_directory_report(identity, True)
    assert sessions == [{"purpose": "people-management"}] and runs[-1] == (True, audit)
    repair = None
    with pytest.raises(NotFoundError, match="disabled"):
        await people.repair_directory_report(identity, False)


async def test_duplicate_pass_previews_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from agent_core.cli import people
    from agent_core.domain.errors import AuthorizationError
    from agent_core.domain.people_views import PeopleDedupeReport
    from tests.contract.support import principal

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    calls: list[bool] = []

    class Service:
        async def dedupe(self, who: object, *, apply: bool) -> PeopleDedupeReport:
            calls.append(apply)
            return PeopleDedupeReport(applied=apply, merges=[], suggestions=[], withdrawn=0)

    @asynccontextmanager
    async def build(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(principal=owner, services=SimpleNamespace(people=Service()))

    monkeypatch.setattr(people, "build", build)
    identity = f"{owner.tenant_id}/{owner.principal_id}"
    with pytest.raises(AuthorizationError):
        await people.dedupe_report("other/owner", True)
    preview = await people.dedupe_report(identity, False)
    assert isinstance(preview, dict) and preview["applied"] is False
    await people.dedupe_report(identity, True)
    assert calls == [False, True]


def test_erasure_replay_requires_offline_assertion_before_opening_receipt() -> None:
    result = CliRunner().invoke(
        app,
        [
            "people",
            "restore-erasure",
            "--owner",
            "tenant/owner",
            "--receipt",
            "missing.json",
            "--sha256",
            "0" * 64,
            "--audit-session",
            "00000000-0000-0000-0000-000000000020",
        ],
    )
    assert result.exit_code != 0
    assert "--offline" in result.output


async def test_erasure_archive_is_private_exact_owner_checked_and_digest_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json
    import stat
    from contextlib import asynccontextmanager
    from datetime import timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    from agent_core.application.people_erasure import PeopleErasureService
    from agent_core.cli import people
    from agent_core.domain.errors import AuthorizationError, ToolValidationError
    from agent_core.domain.people import PeopleErasure
    from tests.contract.support import NOW, memory_uow_factory, principal, session

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    target = uuid4()
    root = PeopleErasure(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
        target_id=target,
        expected_revisions={},
        state="completed",
        expires_at=NOW + timedelta(minutes=1),
        request_hash="0" * 64,
        blocked_record_ids=[target],
        blocked_source_ids=[uuid4()],
    )
    async with factory() as uow:
        await uow.people.put(root, expected_revision=0)
    builds = []

    @asynccontextmanager
    async def build(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        builds.append(kwargs)
        yield SimpleNamespace(
            principal=owner,
            uow_factory=factory,
            clock=clock,
            people_erasure=PeopleErasureService(factory, clock),
        )

    monkeypatch.setattr(people, "build", build)
    identity = f"{owner.tenant_id}/{owner.principal_id}"
    path = tmp_path / "receipt.json"
    exported = await people.export_erasure_receipt(identity, root.id, path)
    assert isinstance(exported, dict)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert exported["sha256"] == digest
    with pytest.raises(FileExistsError):
        await people.export_erasure_receipt(identity, root.id, path)
    with pytest.raises(AuthorizationError):
        await people.export_erasure_receipt("other/owner", root.id, tmp_path / "foreign.json")
    assert not (tmp_path / "foreign.json").exists()
    count = len(builds)
    with pytest.raises(ToolValidationError, match="digest"):
        await people.restore_erasure_receipt(identity, path, "f" * 64, session().id)
    assert len(builds) == count
    result = await people.restore_erasure_receipt(identity, path, digest, session().id)
    assert isinstance(result, dict) and result["state"] == "completed"
    assert await people.restore_erasure_receipt(identity, path, digest, session().id) == result
    foreign = json.loads(path.read_text())
    foreign["receipt"]["principal_id"] = "other"
    path.write_text(json.dumps(foreign))
    with pytest.raises(ToolValidationError, match="exact owner"):
        await people.restore_erasure_receipt(
            identity, path, hashlib.sha256(path.read_bytes()).hexdigest(), session().id
        )


def _people_build(
    monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True, names: tuple[str, ...] = ()
) -> list[str]:
    """Route the People commands to a fresh in-memory owner composition per invocation.

    Each invocation re-creates the named people; sequential ids keep their ids
    stable across invocations, and the returned list carries them in order.
    """

    import asyncio
    from contextlib import asynccontextmanager
    from dataclasses import replace

    from agent_core.bootstrap import build
    from agent_core.cli import people
    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.people_views import CreatePerson
    from tests.contract.support import principal, session
    from tests.integration.m2_support import memory_settings

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    created: list[str] = []

    @asynccontextmanager
    async def people_build(**_kwargs: Any) -> AsyncIterator[Any]:
        async with build(
            settings=replace(memory_settings(), people_enabled=enabled),
            storage="memory",
            principal=owner,
            sequential_ids=True,
        ) as composition:
            async with composition.uow_factory() as uow:
                await uow.sessions.create(session())
            ids = []
            service = composition.services.people
            for index, name in enumerate(names):
                assert service is not None
                person = await service.create(
                    owner,
                    CreatePerson(session_id=session().id, display_name=name),
                    key=f"create-{index}",
                    ceiling=Sensitivity.SENSITIVE,
                )
                ids.append(str(person.id))
            created[:] = ids
            yield composition

    async def prime() -> None:
        async with people_build():
            pass

    asyncio.run(prime())
    monkeypatch.setattr(people, "build", people_build)
    return created


def test_people_reads_print_the_same_projections_as_http(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    created = _people_build(monkeypatch, names=("Maya Chen",))
    runner = CliRunner()
    listed = runner.invoke(app, ["people", "list", "--ceiling", "sensitive"])
    assert listed.exit_code == 0, listed.output
    [row] = json.loads(listed.stdout)["items"]
    [person_id] = created
    assert (row["id"], row["display_name"]) == (person_id, "Maya Chen")

    fetched = runner.invoke(app, ["people", "get", person_id, "--ceiling", "sensitive"])
    assert fetched.exit_code == 0, fetched.output
    assert json.loads(fetched.stdout)["person"]["id"] == person_id
    history = runner.invoke(app, ["people", "history", person_id, "--ceiling", "sensitive"])
    assert history.exit_code == 0, history.output
    assert json.loads(history.stdout)["items"] == []


def test_people_diagnose_reports_counts_and_never_content(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    [person_id] = _people_build(monkeypatch, names=("Maya Chen",))
    result = CliRunner().invoke(app, ["people", "diagnose", person_id, "--ceiling", "sensitive"])
    assert result.exit_code == 0, result.output
    diagnosis = json.loads(result.stdout)
    assert diagnosis["person_id"] == person_id
    assert diagnosis["visible_aliases"] >= 0
    assert diagnosis["visible_facts"] == diagnosis["visible_interactions"] == 0
    assert "Maya" not in result.stdout, "a diagnosis carries counts, not the person's content"


@pytest.mark.parametrize(
    ("enabled", "args", "exit_code", "message"),
    [
        (False, ["list", "--ceiling", "sensitive"], 1, "People is disabled"),
        (True, ["get", "00000000-0000-0000-0000-00000000abcd", "--ceiling", "sensitive"], 1, ""),
        (True, ["list", "--ceiling", "top-secret"], 2, ""),
    ],
    ids=["disabled", "unknown-person", "unknown-ceiling"],
)
def test_people_reads_fail_closed(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, args: list[str], exit_code: int, message: str
) -> None:
    _people_build(monkeypatch, enabled=enabled)
    result = CliRunner().invoke(app, ["people", *args])
    assert result.exit_code == exit_code
    assert result.stdout == ""
    assert message in result.stderr


def test_people_merge_previews_through_the_identity_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from tests.contract.support import principal, session

    source, target = _people_build(monkeypatch, names=("Al", "Alex"))
    request = tmp_path / "merge.json"
    request.write_text(
        json.dumps(
            {
                "session_id": str(session().id),
                "operation": "merge",
                "source_id": source,
                "target_id": target,
                "expected_revisions": {source: 1, target: 1},
            }
        )
    )
    owner = principal()
    result = CliRunner().invoke(
        app,
        [
            "people",
            "merge",
            "--owner",
            f"{owner.tenant_id}/{owner.principal_id}",
            "--request",
            str(request),
            "--key",
            "merge-preview",
            "--ceiling",
            "sensitive",
        ],
    )
    assert result.exit_code == 0, result.output
    preview = json.loads(result.stdout)
    assert preview["operation"] == "merge"
    assert preview["state"] == "preview"
    assert set(preview["person_ids"]) == {source, target}
    assert not {"tenant_id", "principal_id", "request_hash"} & preview.keys()


@pytest.mark.parametrize(
    ("command", "owner", "operation", "message"),
    [
        ("merge", "someone/else", "merge", "--owner must match"),
        ("split", None, "merge", "differs from the selected command"),
        ("undo", None, "split", "differs from the selected command"),
    ],
    ids=["foreign-owner", "split-with-merge-request", "undo-with-split-request"],
)
def test_people_writes_refuse_before_the_identity_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    owner: str | None,
    operation: str,
    message: str,
) -> None:
    import json
    from uuid import UUID

    from agent_core.application.people import PublicPeopleService
    from tests.contract.support import principal, session

    _people_build(monkeypatch)
    called: list[object] = []

    async def identity_operation(*args: object, **kwargs: object) -> object:
        called.append(args)
        raise AssertionError("a refused write must not reach the identity service")

    monkeypatch.setattr(PublicPeopleService, "identity_operation", identity_operation)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "session_id": str(session().id),
                "operation": operation,
                "source_id": str(UUID(int=1)),
                "target_id": str(UUID(int=2)),
            }
        )
    )
    configured = principal()
    result = CliRunner().invoke(
        app,
        [
            "people",
            command,
            "--owner",
            owner or f"{configured.tenant_id}/{configured.principal_id}",
            "--request",
            str(request),
            "--key",
            "refused",
            "--ceiling",
            "sensitive",
        ],
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert message in result.stderr
    assert called == []


def test_people_write_reports_an_unreadable_request_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _people_build(monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "people",
            "import",
            "--owner",
            "tenant-a/principal-a",
            "--request",
            str(tmp_path / "absent.json"),
            "--key",
            "import",
            "--ceiling",
            "sensitive",
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
