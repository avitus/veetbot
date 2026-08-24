"""Composition of the three first-party Gmail MCP servers (Milestone 18)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.bootstrap import build
from agent_core.config import ConfigurationError, load_settings
from agent_core.domain.mcp import MCPAuthScheme, MCPTransport
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from tests.unit.test_config import base_environment

EMAIL_SCOPES = {"mcp.gmail_read.use", "mcp.gmail_write.use", "mcp.gmail_send.use"}

_CLASSIFICATION = {
    "gmail_read": (SideEffectClass.NETWORK_READ, RiskLevel.LOW, IdempotencyClass.READ_ONLY),
    "gmail_write": (
        SideEffectClass.EXTERNAL_WRITE,
        RiskLevel.MEDIUM,
        IdempotencyClass.NON_IDEMPOTENT,
    ),
    "gmail_send": (
        SideEffectClass.EXTERNAL_MESSAGE,
        RiskLevel.HIGH,
        IdempotencyClass.NON_IDEMPOTENT,
    ),
}


def credential_files(tmp_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for server_id in ("gmail_read", "gmail_write", "gmail_send"):
        path = tmp_path / f"{server_id}.json"
        path.write_text(
            json.dumps(
                {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "refresh_token": f"refresh-{server_id}",
                    "scope": f"https://www.googleapis.com/auth/{server_id}",
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        values[f"{server_id.upper()}_CREDENTIAL_FILE"] = str(path)
    return values


def email_environment(tmp_path: Path) -> dict[str, str]:
    return {
        **base_environment(),
        "SANDBOX_MECHANISM": "fake",
        "AGENT_EMAIL_ENABLED": "1",
        **credential_files(tmp_path),
    }


async def test_email_flag_composes_three_operator_rows(tmp_path: Path) -> None:
    settings = load_settings(email_environment(tmp_path))

    async with build(settings=settings) as composition, composition.uow_factory() as uow:
        rows = {
            config.server_id: config
            for config in await uow.mcp_servers.list_enabled("local")
            if config.server_id.startswith("gmail_")
        }

    assert set(rows) == set(_CLASSIFICATION)
    for server_id, (side_effect, risk, idempotency) in _CLASSIFICATION.items():
        config = rows[server_id]
        assert config.transport is MCPTransport.STDIO
        assert config.operator_configured
        assert config.auth_scheme is MCPAuthScheme.ENV
        assert config.auth_name == "GMAIL_MCP_CREDENTIAL"
        assert config.credential_ref == server_id
        assert config.side_effect is side_effect
        assert config.risk is risk
        assert config.idempotency is idempotency
        assert config.required_scopes == frozenset({f"mcp.{server_id}.use"})
        assert "-m gmail_mcp" in config.endpoint
        assert f"--mode {server_id.removeprefix('gmail_')}" in config.endpoint


async def test_email_default_off_composes_nothing(tmp_path: Path) -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(settings=settings) as composition:
        assert not EMAIL_SCOPES & composition.principal.scopes
        async with composition.uow_factory() as uow:
            rows = [
                config
                for config in await uow.mcp_servers.list_enabled("local")
                if config.server_id.startswith("gmail_")
            ]
    assert rows == []


async def test_scope_grants_follow_the_flag(tmp_path: Path) -> None:
    settings = load_settings(email_environment(tmp_path))

    async with build(settings=settings) as composition:
        assert composition.principal.scopes >= EMAIL_SCOPES


def test_a_missing_credential_file_is_a_configuration_error(tmp_path: Path) -> None:
    environment = email_environment(tmp_path)
    del environment["GMAIL_SEND_CREDENTIAL_FILE"]
    with pytest.raises(ConfigurationError, match="GMAIL_SEND_CREDENTIAL_FILE"):
        load_settings(environment)
