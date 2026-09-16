"""People operator commands expose the same revisioned services and explicit owner."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli.main import app


def test_people_operator_commands_and_explicit_write_owner() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["people", "--help"])
    assert result.exit_code == 0, result.output
    for command in [
        "list",
        "get",
        "history",
        "diagnose",
        "merge",
        "split",
        "forget",
        "import",
        "link-existing",
        "export-erasure",
        "restore-erasure",
    ]:
        assert command in result.output
    refused = runner.invoke(app, ["people", "merge", "--request", "missing.json", "--key", "merge"])
    assert refused.exit_code != 0
    assert "--owner" in refused.output


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
