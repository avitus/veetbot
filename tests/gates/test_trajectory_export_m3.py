"""Milestone 3 redaction and prospective/withdrawn consent gates."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from agent_core.application.trajectory_service import TrajectoryRedactor
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.errors import (
    ArtifactSweepError,
    ExportConsentError,
    ExportRedactionError,
    ExportRedactionPatternError,
    ExportStateError,
    NotFoundError,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from agent_core.domain.trajectory import ArtifactRef, ExportConsent
from agent_core.ports.artifacts import TrajectoryArtifactStore
from tests.contract.support import NOW


def settings(root: Path, *, enabled: bool) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        trajectory_export_enabled=enabled,
        artifact_root=root,
    )


def sensitive_prompt() -> tuple[str, list[str]]:
    """Return one export prompt containing every governed sensitive shape."""

    values = [
        "sk-" + "x" * 16,
        "-----BEGIN " + "PRIVATE KEY-----",
        "Authorization:" + " Bearer " + "opaque-value-123",
        "Bearer " + "a1b2c3d4",
        "postgresql" + "://user:password@database.example/app",
        "api_key" + "='opaque-value-123'",
        "customer-73921",
    ]
    return " redact ".join(values), values


@pytest.mark.parametrize("pattern", ["(", "(a+)+", ".*secret"])
def test_tenant_redaction_patterns_fail_during_construction_when_unsafe(
    pattern: str,
) -> None:
    with pytest.raises(ExportRedactionPatternError, match="tenant-rule"):
        TrajectoryRedactor([("tenant-rule", pattern)])


async def test_export_replaces_all_rules_and_fails_closed(tmp_path: Path) -> None:
    """Replace each mandatory secret family and refuse an unverifiable export."""

    prompt, source_values = sensitive_prompt()
    redactor = TrajectoryRedactor([("tenant_pattern", r"customer-\d+")])
    async with build(
        settings=settings(tmp_path / "clean", enabled=True),
        fixed_clock_at=NOW,
        sequential_ids=True,
        trajectory_redactor=redactor,
    ) as composition:
        await composition.trajectories.grant_consent()
        run_id = await composition.runs.submit(prompt)
        await composition.runs.wait_terminal(run_id)
        artifact = await composition.trajectories.export(run_id)
        document = json.loads((await composition.trajectories.read(run_id)).decode())

    rendered = json.dumps(document, sort_keys=True)
    for value in source_values:
        assert value not in rendered
    replacements = document["redaction"]["replacements"]
    assert document["redaction"]["ruleset_version"] == "secrets@2"
    assert set(replacements) >= {
        "provider_key",
        "private_key",
        "bearer_literal",
        "dsn_password",
        "assigned_secret",
        "tenant_pattern",
    }
    assert artifact.origin == "trajectory_export"
    assert document["outcome"] == "COMPLETED"
    assert all(
        "source_event_sequence" not in message and "principal_id" not in message
        for message in document["messages"]
    )
    assert "usage" not in rendered
    assert "provider_metadata" not in rendered

    rejecting = TrajectoryRedactor(replacement_enabled=False)
    async with build(
        settings=settings(tmp_path / "refused", enabled=True),
        fixed_clock_at=NOW,
        sequential_ids=True,
        trajectory_redactor=rejecting,
    ) as composition:
        await composition.trajectories.grant_consent()
        refused_run = await composition.runs.submit(source_values[0])
        await composition.runs.wait_terminal(refused_run)
        with pytest.raises(ExportRedactionError) as captured:
            await composition.trajectories.export(refused_run)
    assert captured.value.rule == "provider_key"
    assert source_values[0] not in str(captured.value)
    assert list((tmp_path / "refused").rglob("*.json")) == []


async def test_consent_is_stamped_forward_and_withdrawn_backward(tmp_path: Path) -> None:
    async with build(
        settings=settings(tmp_path / "disabled", enabled=False),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await composition.trajectories.grant_consent()
        disabled_run = await composition.runs.submit("disabled tenant")
        await composition.runs.wait_terminal(disabled_run)
        with pytest.raises(ExportConsentError):
            await composition.trajectories.export(disabled_run)

    async with build(
        settings=settings(tmp_path / "prospective", enabled=True),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        old_run = await composition.runs.submit("created before consent")
        await composition.runs.wait_terminal(old_run)
        with pytest.raises(ExportConsentError):
            await composition.trajectories.export(old_run)
        await composition.trajectories.grant_consent()
        with pytest.raises(ExportConsentError):
            await composition.trajectories.export(old_run)

        withdrawn_run = await composition.runs.submit("withdraw before export")
        await composition.runs.wait_terminal(withdrawn_run)
        await composition.trajectories.withdraw_consent()
        with pytest.raises(ExportConsentError):
            await composition.trajectories.export(withdrawn_run)

        await composition.trajectories.grant_consent()
        exportable = await composition.runs.submit("export then withdraw")
        await composition.runs.wait_terminal(exportable)
        artifact = await composition.trajectories.export(exportable)
        artifact_path = tmp_path / "prospective" / artifact.storage_uri
        assert artifact_path.is_file()
        await composition.trajectories.withdraw_consent()
        async with composition.uow_factory() as uow:
            expired = await uow.trajectory_exports.get_for_run(exportable)
        assert expired is not None
        assert expired.artifact.expires_at is not None
        assert expired.artifact.expires_at <= composition.clock.now()
        assert await composition.trajectories.sweep_once() == 1
        assert not artifact_path.exists()


async def test_export_rechecks_locked_consent_before_committing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with build(
        settings=settings(tmp_path / "race", enabled=True),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await composition.trajectories.grant_consent()
        run_id = await composition.runs.submit("withdraw while the export is materializing")
        await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            repository = uow.export_consent

        async def withdrawn(_tenant_id: str, _principal_id: str) -> ExportConsent:
            return ExportConsent(
                tenant_id="tenant-test",
                principal_id="principal-test",
                granted_at=NOW,
                withdrawn_at=NOW + timedelta(seconds=1),
            )

        monkeypatch.setattr(repository, "get_for_update", withdrawn)
        with pytest.raises(ExportConsentError, match="withdrawn before commit"):
            await composition.trajectories.export(run_id)
    assert list((tmp_path / "race").rglob("*.json")) == []


class _FailFirstDeleteStore:
    def __init__(self, delegate: TrajectoryArtifactStore) -> None:
        self.delegate = delegate
        self.attempts = 0

    async def write(self, artifact: ArtifactRef, content: bytes) -> ArtifactRef:
        return await self.delegate.write(artifact, content)

    async def read(self, artifact: ArtifactRef) -> bytes:
        return await self.delegate.read(artifact)

    def stream(self, artifact: ArtifactRef) -> AsyncIterator[bytes]:
        return self.delegate.stream(artifact)

    async def delete(self, artifact: ArtifactRef) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise OSError("synthetic deletion failure")
        await self.delegate.delete(artifact)


async def test_sweeper_attempts_every_artifact_when_one_delete_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with build(
        settings=settings(tmp_path / "sweep", enabled=True),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await composition.trajectories.grant_consent()
        first = await composition.runs.submit("first export")
        await composition.runs.wait_terminal(first)
        await composition.trajectories.export(first)
        second = await composition.runs.submit("second export")
        await composition.runs.wait_terminal(second)
        await composition.trajectories.export(second)
        await composition.trajectories.withdraw_consent()

        store = _FailFirstDeleteStore(composition.trajectories._artifacts)
        monkeypatch.setattr(composition.trajectories, "_artifacts", store)
        with pytest.raises(ArtifactSweepError) as captured:
            await composition.trajectories.sweep_once()
        assert captured.value.deleted == 1
        assert captured.value.failed == 1
        assert store.attempts == 2
        assert await composition.trajectories.sweep_once() == 1
        assert store.attempts == 3


async def test_an_export_is_reissued_once_and_expiry_withdraws_it_until_swept(
    tmp_path: Path,
) -> None:
    async with build(
        settings=settings(tmp_path / "lifecycle", enabled=True),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await composition.trajectories.grant_consent()
        run_id = await composition.runs.submit("export me twice")
        await composition.runs.wait_terminal(run_id)
        first = await composition.trajectories.export(run_id)
        again = await composition.trajectories.export(run_id)
        content = await composition.trajectories.read(run_id)

        await composition.trajectories.withdraw_consent()
        with pytest.raises(NotFoundError):
            await composition.trajectories.read(run_id)
        await composition.trajectories.grant_consent()
        with pytest.raises(ExportStateError, match="awaits its sweep"):
            await composition.trajectories.export(run_id)

        assert await composition.trajectories.sweep_once() == 1
        reissued = await composition.trajectories.export(run_id)

    assert again == first
    assert list((tmp_path / "lifecycle").rglob("*.json")) == [
        tmp_path / "lifecycle" / reissued.storage_uri
    ]
    assert reissued.id != first.id
    assert json.loads(content)["run_id"] == str(run_id)


async def test_only_a_terminal_run_can_be_exported(tmp_path: Path) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="conversation.ask_user",
                        arguments={"question": "Which region?"},
                        call_id="ask",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    async with build(
        settings=settings(tmp_path / "waiting", enabled=True),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await composition.trajectories.grant_consent()
        run_id = await composition.runs.submit("ask me first")
        assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_USER
        with pytest.raises(ExportStateError, match="terminal"):
            await composition.trajectories.export(run_id)
    assert list((tmp_path / "waiting").rglob("*.json")) == []


async def test_an_export_carries_each_tool_exchange_once_and_no_reasoning_state(
    tmp_path: Path,
) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                provider_reasoning_payload={"id": "rs_export", "encrypted_content": "opaque"},
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "17 * 23"},
                        call_id="calc",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="391"),
        ]
    )
    async with build(
        settings=settings(tmp_path / "exchange", enabled=True),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await composition.trajectories.grant_consent()
        run_id = await composition.runs.submit("multiply")
        await composition.runs.wait_terminal(run_id)
        await composition.trajectories.export(run_id)
        document = json.loads((await composition.trajectories.read(run_id)).decode())

    kinds = [message["kind"] for message in document["messages"]]
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert "provider_reasoning" not in kinds
    rendered = json.dumps(document)
    assert "rs_export" not in rendered
    assert "opaque" not in rendered
