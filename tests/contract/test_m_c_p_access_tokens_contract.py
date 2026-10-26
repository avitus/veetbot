"""MCPAccessTokens contract (ADR-0255): a current token, or PermissionError."""

from __future__ import annotations

import base64
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.mcp.sealing import AESGCMSecretSealer
from agent_core.application.mcp_servers import MCPServerSettingsService
from agent_core.domain.mcp import MCPServerAccess
from agent_core.ports.mcp import MCPAccessTokens
from tests.contract.support import TENANT, memory_uow_factory, principal
from tests.gates.test_owner_mcp_servers_adr0255 import URL, FakeGateway

CALLBACK = "https://api.veetbot.example/v1/mcp-oauth/callback"


async def assert_access_tokens_contract(
    tokens: MCPAccessTokens, *, sign_in: object, clock: FixedClock, expires_in: int
) -> None:
    """``sign_in`` signs the "design" server in; tokens refresh within a minute of expiry."""
    with pytest.raises(PermissionError):
        await tokens.access_token(TENANT, "design")
    with pytest.raises(PermissionError):
        await tokens.access_token(TENANT, "missing")
    assert callable(sign_in)
    await sign_in()
    first = await tokens.access_token(TENANT, "design")
    assert (await tokens.access_token(TENANT, "design")).reveal() == first.reveal()
    clock.advance(timedelta(seconds=expires_in - 59))
    refreshed = await tokens.access_token(TENANT, "design")
    assert refreshed.reveal() != first.reveal()
    forced = await tokens.access_token(TENANT, "design", force_refresh=True)
    assert forced.reveal() not in {first.reveal(), refreshed.reveal()}


async def test_the_settings_service_is_an_access_token_source() -> None:
    clock, uow_factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"settings.read", "settings.write"}})
    service = MCPServerSettingsService(
        uow_factory=uow_factory,
        clock=clock,
        ids=SequenceIdFactory(),
        gateway=FakeGateway(expires_in=600),
        sealer=AESGCMSecretSealer.from_text(
            base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
        ),
        callback_url=CALLBACK,
        tenant_id=TENANT,
    )
    await service.add(owner, "design", URL, MCPServerAccess.CHANGES)

    async def sign_in() -> None:
        started = await service.begin_sign_in(owner, "design")
        state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
        assert await service.complete_sign_in(state, "code", None) == ("design", "signed_in")

    await assert_access_tokens_contract(service, sign_in=sign_in, clock=clock, expires_in=600)
