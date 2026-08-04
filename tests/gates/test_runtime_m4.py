from __future__ import annotations

from pathlib import Path

import pytest

import agent_core.bootstrap as bootstrap_module
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import FailureReason, RunStatus


def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
        artifact_root=tmp_path / "artifacts",
    )


async def test_waiting_holds_nothing(tmp_path: Path) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "hello"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    async with build(settings=settings(tmp_path), script=script) as app:
        run_id = await app.runs.submit("request an external write")
        parked = await app.runs.get(run_id)
        assert parked.status is RunStatus.WAITING_FOR_APPROVAL
        assert parked.lease_owner is None
        assert parked.lease_expires_at is None
        assert not app.uow_factory.is_open()


async def test_three_identical_denials_fail_the_run(tmp_path: Path) -> None:
    denied = ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name="workspace.write_text",
                arguments={"path": "/etc/passwd", "content": "blocked"},
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    script = FakeModelScript(turns=[denied, denied, denied])
    async with build(settings=settings(tmp_path), script=script) as app:
        run_id = await app.runs.submit("repeat a denied write")
        failed = await app.runs.get(run_id)
        assert failed.status is RunStatus.FAILED
        assert failed.failure is not None
        assert failed.failure.reason is FailureReason.REPEATED_DENIAL
        assert failed.failure.error_class == "ToolPolicyDenied"


async def test_completed_run_releases_its_active_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_slot = bootstrap_module._ActiveToken()
    monkeypatch.setattr(bootstrap_module, "_ActiveToken", lambda: token_slot)
    script = FakeModelScript(turns=[ScriptedTurn(text="done", stop_reason=StopReason.END_TURN)])
    async with build(settings=settings(tmp_path), script=script) as app:
        run_id = await app.runs.submit("complete and release the token")
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED

    assert run_id not in token_slot._tokens
