"""The operator commands keep the CLI contract over the shared services.

bootstrap-and-composition.md fixes the contract every command below keeps: the
result goes to stdout and only the result, a refused configuration exits 4
where the command says so, a usage error exits 2, and a missing or conflicting
resource exits 1. The surface and persona groups come from inbound-surfaces.md
and persona-surface.md. Each invocation builds its own in-memory composition,
as the real command builds its own PostgreSQL one; sequential identifiers make
a seeded resource's id the same in every invocation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

import agent_core.cli.main as cli_main
from agent_core.application.surfaces import SurfaceManagementService
from agent_core.bootstrap import Composition, build
from agent_core.config import ConfigurationError, Settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.devices import DeviceKind, DeviceRegistration, PushProvider
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.surfaces import SurfaceChatKind, SurfaceInboundMessage, SurfaceMessageKind
from agent_core.domain.views import ApprovalFilters, TextContentBlock
from tests.integration.m2_support import memory_settings

runner = CliRunner()
Seed = Callable[[Composition, dict[str, Any]], Awaitable[None]]


def _use(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: Settings | None = None,
    script: Callable[[], FakeModelScript] | None = None,
    seed: Seed | None = None,
) -> dict[str, Any]:
    """Route the CLI's composition to a seeded in-memory one; return what the seed saw."""

    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_build(**_kwargs: Any) -> AsyncIterator[Composition]:
        async with build(
            settings=settings or memory_settings(),
            storage="memory",
            sequential_ids=True,
            script=None if script is None else script(),
        ) as composition:
            if seed is not None:
                await seed(composition, seen)
            yield composition

    async def prime() -> None:
        async with fake_build():
            pass

    if seed is not None:
        # Sequential ids give every invocation's seeded resources these same ids.
        asyncio.run(prime())
    monkeypatch.setattr(cli_main, "build", fake_build)
    return seen


def _refuse_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(**_kwargs: Any) -> Any:
        raise ConfigurationError("startup refused: fixture")

    monkeypatch.setattr(cli_main, "build", refused)


def _external_write() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "fixture", "content": "held"},
                        call_id="needs-approval",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The write was not performed."),
        ]
    )


async def _suspended_run(composition: Composition, seen: dict[str, Any]) -> None:
    session = await composition.services.sessions.create(composition.principal, "general", {})
    submitted = await composition.services.runs.submit(
        composition.principal,
        session.id,
        [TextContentBlock(text="write the fixture")],
        None,
        None,
    )
    page = await composition.services.approvals.list(
        composition.principal, ApprovalFilters(), 200, None
    )
    seen["run_id"] = submitted.run_id
    seen["approval_id"] = page.items[0].id


@pytest.mark.parametrize(
    ("args", "exit_code"),
    [
        (["session", "create"], 4),
        (["run", "cancel", str(UUID(int=1))], 4),
        (["approval", "list"], 4),
        (["approval", "approve", str(UUID(int=1))], 4),
        (["approval", "deny", str(UUID(int=1))], 4),
        (["persona", "history"], 4),
        (["persona", "nominations"], 4),
        (["surface", "list"], 4),
        (["surface", "pair", str(UUID(int=1))], 1),
        (["surface", "pairings", str(UUID(int=1))], 1),
        (["surface", "revoke", str(UUID(int=1))], 1),
        (["session", "export-consent", "grant"], 1),
        (["email", "exclude-bulk"], 1),
    ],
)
def test_refused_configuration_exits_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, args: list[str], exit_code: int
) -> None:
    _refuse_configuration(monkeypatch)
    result = runner.invoke(cli_main.app, args)
    assert result.exit_code == exit_code, result.output
    assert result.stdout == ""
    assert "startup refused: fixture" in result.stderr
    assert "Traceback" not in result.output


def test_session_create_prints_only_the_session_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch)
    result = runner.invoke(cli_main.app, ["session", "create"])
    assert result.exit_code == 0, result.output
    [line] = result.stdout.splitlines()
    assert str(UUID(line)) == line
    assert result.stderr == ""


def test_run_cancel_prints_the_cancelled_run_and_refuses_an_unknown_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _use(monkeypatch, script=_external_write, seed=_suspended_run)
    missing = runner.invoke(cli_main.app, ["run", "cancel", str(uuid4())])
    assert missing.exit_code == 1
    assert missing.stdout == ""
    assert "not found" in missing.stderr

    cancelled = runner.invoke(cli_main.app, ["run", "cancel", str(seen["run_id"])])
    assert cancelled.exit_code == 0, cancelled.output
    body = json.loads(cancelled.stdout)
    assert body["id"] == str(seen["run_id"])
    assert body["status"] == RunStatus.CANCELLED.value


def test_approval_list_prints_every_pending_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _use(monkeypatch, script=_external_write, seed=_suspended_run)
    result = runner.invoke(cli_main.app, ["approval", "list"])
    assert result.exit_code == 0, result.output
    [approval] = json.loads(result.stdout)
    assert approval["id"] == str(seen["approval_id"])
    assert approval["run_id"] == str(seen["run_id"])
    assert approval["status"] == "PENDING"
    assert approval["tool_name"] == "demo.external_write"


@pytest.mark.parametrize(
    ("command", "status"),
    [("approve", "APPROVED"), ("deny", "DENIED")],
)
def test_approval_resolution_prints_the_resolution(
    monkeypatch: pytest.MonkeyPatch, command: str, status: str
) -> None:
    seen = _use(monkeypatch, script=_external_write, seed=_suspended_run)
    result = runner.invoke(
        cli_main.app,
        ["approval", command, str(seen["approval_id"]), "--reason", "operator decision"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["id"] == str(seen["approval_id"])
    assert body["status"] == status


def test_approval_resolution_refuses_unknown_and_already_resolved_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolved(composition: Composition, seen: dict[str, Any]) -> None:
        await _suspended_run(composition, seen)
        await composition.services.approvals.resolve(
            composition.principal, seen["approval_id"], ApprovalResolutionType.DENY, None
        )

    seen = _use(monkeypatch, script=_external_write, seed=resolved)
    missing = runner.invoke(cli_main.app, ["approval", "approve", str(uuid4())])
    assert missing.exit_code == 1
    assert missing.stdout == ""
    again = runner.invoke(cli_main.app, ["approval", "approve", str(seen["approval_id"])])
    assert again.exit_code == 1
    assert again.stdout == ""
    assert again.stderr.strip()


def test_approval_resolution_rejects_a_malformed_identifier() -> None:
    result = runner.invoke(cli_main.app, ["approval", "deny", "not-a-uuid"])
    assert result.exit_code == 2


def test_export_consent_grants_withdraws_and_rejects_unknown_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def granted(composition: Composition, _seen: dict[str, Any]) -> None:
        await composition.trajectories.grant_consent()

    _use(monkeypatch)
    grant = runner.invoke(cli_main.app, ["session", "export-consent", "grant"])
    assert grant.exit_code == 0, grant.output
    assert json.loads(grant.stdout)["withdrawn_at"] is None
    never_granted = runner.invoke(cli_main.app, ["session", "export-consent", "withdraw"])
    assert never_granted.exit_code == 1
    assert never_granted.stdout == ""

    _use(monkeypatch, seed=granted)
    withdraw = runner.invoke(cli_main.app, ["session", "export-consent", "withdraw"])
    assert withdraw.exit_code == 0, withdraw.output
    assert json.loads(withdraw.stdout)["withdrawn_at"] is not None

    unknown = runner.invoke(cli_main.app, ["session", "export-consent", "maybe"])
    assert unknown.exit_code == 2


def test_email_exclude_bulk_previews_unless_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    async def exclude(confirm: bool) -> Any:
        calls.append(confirm)
        from agent_core.domain.email import EmailBulkExclusionReport

        return EmailBulkExclusionReport(confirmed=confirm, candidates=[], excluded=[])

    monkeypatch.setattr(cli_main, "_email_exclude_bulk", exclude)
    preview = runner.invoke(cli_main.app, ["email", "exclude-bulk"])
    applied = runner.invoke(cli_main.app, ["email", "exclude-bulk", "--confirm"])
    assert preview.exit_code == applied.exit_code == 0
    assert calls == [False, True]
    assert json.loads(preview.stdout)["confirmed"] is False
    assert json.loads(applied.stdout)["confirmed"] is True


def test_persona_history_and_nominations_print_json_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def edited(composition: Composition, _seen: dict[str, Any]) -> None:
        from agent_core.domain.persona import PersonaEntryDraft

        await composition.services.persona.update(
            composition.principal,
            expected_version=0,
            entries=[PersonaEntryDraft(text="User values honesty.")],
        )

    _use(monkeypatch, seed=edited)
    history = runner.invoke(cli_main.app, ["persona", "history", "--limit", "5"])
    assert history.exit_code == 0, history.output
    versions = json.loads(history.stdout)
    assert [row["version"] for row in versions] == [1]
    nominations = runner.invoke(cli_main.app, ["persona", "nominations", "--state", "nominated"])
    assert nominations.exit_code == 0, nominations.output
    assert json.loads(nominations.stdout) == []


@pytest.mark.parametrize(
    "args",
    [
        ["persona", "history", "--limit", "0"],
        ["persona", "history", "--limit", "201"],
        ["persona", "nominations", "--state", "pending"],
    ],
)
def test_persona_listing_rejects_out_of_contract_options(args: list[str]) -> None:
    assert runner.invoke(cli_main.app, args).exit_code == 2


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _surface_settings() -> Settings:
    return replace(memory_settings(), surface_api_enabled=True, surface_worker_enabled=True)


async def _surface(composition: Composition, seen: dict[str, Any]) -> None:
    registered = await composition.services.devices.register(
        composition.principal,
        DeviceRegistration(
            client_device_id="whatsapp:15551234567",
            name="Veetbot WhatsApp",
            kind=DeviceKind.SURFACE,
            platform="whatsapp",
            push_provider=PushProvider.WHATSAPP,
            push_token=SecretStr("15551234567"),
        ),
    )
    seen["surface_id"] = registered.device.id


async def _paired_surface(composition: Composition, seen: dict[str, Any]) -> None:
    await _surface(composition, seen)
    issued = await composition.services.surfaces.issue_code(
        composition.principal,
        seen["surface_id"],
        granted_scopes=frozenset({"run.write"}),
        label="Owner",
        idempotency_key="seed",
    )
    seen["code"] = issued.code.get_secret_value()
    paired = await composition.surface_ingress.ingest(
        SurfaceInboundMessage(
            surface_id=seen["surface_id"],
            provider=PushProvider.WHATSAPP,
            external_update_id="wamid.seed",
            sender_id="15550001111",
            sender_label="Owner",
            chat_ref="15550001111",
            chat_kind=SurfaceChatKind.DIRECT,
            message_kind=SurfaceMessageKind.TEXT,
            text=f"/pair {seen['code']}",
            received_at=NOW,
        )
    )
    assert paired.reason_code == "surface.paired"
    [pairing] = await composition.services.surfaces.list_pairings(
        composition.principal, seen["surface_id"]
    )
    seen["pairing_id"] = pairing.id


def test_surface_list_prints_one_json_line_per_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _use(monkeypatch, settings=_surface_settings(), seed=_surface)
    result = runner.invoke(cli_main.app, ["surface", "list"])
    assert result.exit_code == 0, result.output
    [line] = result.stdout.splitlines()
    surface = json.loads(line)
    assert surface["id"] == str(seen["surface_id"])
    assert "push_token" not in surface, "only the token's fingerprint is ever printed"
    assert surface["push_token_fingerprint"]


def test_surface_pair_prints_only_the_code_with_run_scopes_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _use(monkeypatch, settings=_surface_settings(), seed=_surface)
    minted: list[dict[str, Any]] = []
    original = SurfaceManagementService.issue_code

    async def recording(self: SurfaceManagementService, *args: Any, **kwargs: Any) -> Any:
        minted.append(kwargs)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(SurfaceManagementService, "issue_code", recording)
    defaulted = runner.invoke(cli_main.app, ["surface", "pair", str(seen["surface_id"])])
    assert defaulted.exit_code == 0, defaulted.output
    [code] = defaulted.stdout.splitlines()
    assert code and "*" not in code
    scoped = runner.invoke(
        cli_main.app,
        [
            "surface",
            "pair",
            str(seen["surface_id"]),
            "--scope",
            "run.read",
            "--label",
            "Partner",
            "--idempotency-key",
            "pair-partner",
        ],
    )
    assert scoped.exit_code == 0, scoped.output
    assert minted[0]["granted_scopes"] == frozenset({"run.read", "run.write"})
    assert minted[0]["idempotency_key"].strip()
    assert minted[1]["granted_scopes"] == frozenset({"run.read"})
    assert (minted[1]["label"], minted[1]["idempotency_key"]) == ("Partner", "pair-partner")


def test_surface_pair_refuses_unknown_surfaces_and_scopes_beyond_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _use(monkeypatch, settings=_surface_settings(), seed=_surface)
    unknown = runner.invoke(cli_main.app, ["surface", "pair", str(uuid4())])
    assert unknown.exit_code == 1
    assert unknown.stdout == ""
    widened = runner.invoke(
        cli_main.app,
        ["surface", "pair", str(seen["surface_id"]), "--scope", "not.a.platform.scope"],
    )
    assert widened.exit_code == 1
    assert widened.stdout == ""
    assert "exceed" in widened.stderr


def test_surface_pairings_and_revoke_follow_the_pairing_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _use(monkeypatch, settings=_surface_settings(), seed=_paired_surface)
    listed = runner.invoke(cli_main.app, ["surface", "pairings", str(seen["surface_id"])])
    assert listed.exit_code == 0, listed.output
    [line] = listed.stdout.splitlines()
    pairing = json.loads(line)
    assert pairing["id"] == str(seen["pairing_id"])
    assert pairing["sender_id"] == "15550001111"
    assert seen["code"] not in listed.stdout

    revoked = runner.invoke(cli_main.app, ["surface", "revoke", str(seen["pairing_id"])])
    assert revoked.exit_code == 0, revoked.output
    assert json.loads(revoked.stdout)["revoked_at"] is not None

    unknown_surface = runner.invoke(cli_main.app, ["surface", "pairings", str(uuid4())])
    assert unknown_surface.exit_code == 1
    unknown_pairing = runner.invoke(cli_main.app, ["surface", "revoke", str(uuid4())])
    assert unknown_pairing.exit_code == 1
    assert unknown_pairing.stdout == ""


def test_worker_refuses_configuration_and_unknown_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(**_kwargs: Any) -> Any:
        raise ConfigurationError("schedule worker is disabled")

    monkeypatch.setattr(cli_main, "build_schedule_worker", refused)
    disabled = runner.invoke(cli_main.app, ["worker", "--role", "schedule"])
    assert disabled.exit_code == 4
    assert "schedule worker is disabled" in disabled.stderr
    assert runner.invoke(cli_main.app, ["worker", "--role", "janitor"]).exit_code == 2


def test_execution_service_requires_an_absolute_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[object] = []

    async def serve(path: object) -> None:
        served.append(path)

    monkeypatch.setattr(cli_main, "serve_execution_service", serve)
    relative = runner.invoke(cli_main.app, ["execution-service", "--socket", "run/exec.sock"])
    assert relative.exit_code == 2
    assert served == []
    absolute = runner.invoke(cli_main.app, ["execution-service", "--socket", "/run/exec.sock"])
    assert absolute.exit_code == 0, absolute.output
    assert [str(path) for path in served] == ["/run/exec.sock"]
