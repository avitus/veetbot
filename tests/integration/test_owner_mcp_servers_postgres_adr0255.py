"""ADR-0255 against PostgreSQL: the contract, isolation, and one refresh at a time."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text

from agent_core.adapters.persistence.sqlalchemy_models import MCPServerAuthorizationRow
from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import build
from agent_core.domain.errors import MCPGrantRejectedError
from agent_core.domain.mcp import (
    MCPAuthorizationMetadata,
    MCPClientRegistration,
    MCPServerAccess,
    MCPTokenGrant,
)
from tests.contract.test_m_c_p_server_repository_contract import (
    OWNER_ROW,
    assert_owner_server_repository_contract,
)
from tests.gates.test_owner_mcp_servers_adr0255 import FakeGateway
from tests.integration.m2_support import database_settings

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")


def _settings() -> Any:
    return replace(
        database_settings(),
        mcp_servers_enabled=True,
        mcp_authorization_key=SecretStr(KEY),
        mcp_oauth_callback_url="http://127.0.0.1:8000/v1/mcp-oauth/callback",
    )


async def test_postgres_owner_server_repository_contract() -> None:
    async with build(settings=database_settings(), storage="postgres") as app:
        async with app.uow_factory() as uow:
            await uow.mcp_servers.delete(OWNER_ROW.tenant_id, OWNER_ROW.server_id)
        async with app.uow_factory() as uow:
            await assert_owner_server_repository_contract(uow.mcp_servers)


async def test_another_tenant_sees_no_sign_in() -> None:
    gateway = FakeGateway()
    async with build(settings=_settings(), storage="postgres", mcp_oauth_gateway=gateway) as app:
        service = app.services.mcp_servers
        assert service is not None
        await service.remove(app.principal, "isolated")
        await service.add(
            app.principal, "isolated", "https://isolated.example.com/mcp", MCPServerAccess.CHANGES
        )
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            assert await uow.mcp_servers.get_authorization("local", "isolated") is not None
            # A superuser bypasses row-level security, so probe as an ordinary role.
            is_superuser = bool(
                await uow.session.scalar(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                )
            )
            if is_superuser:
                await uow.session.execute(
                    text("CREATE ROLE veetbot_mcp_authorization_rls_probe NOLOGIN NOSUPERUSER")
                )
                await uow.session.execute(
                    text(
                        "GRANT SELECT ON mcp_servers, mcp_server_authorizations "
                        "TO veetbot_mcp_authorization_rls_probe"
                    )
                )
                await uow.session.execute(
                    text("SET LOCAL ROLE veetbot_mcp_authorization_rls_probe")
                )
            await uow.session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'other', true)")
            )
            assert await uow.mcp_servers.get_authorization("local", "isolated") is None
            count = await uow.session.scalar(
                select(func.count()).select_from(MCPServerAuthorizationRow)
            )
            assert count == 0
            forced = (
                await uow.session.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'mcp_server_authorizations'"
                    )
                )
            ).one()
            assert tuple(forced) == (True, True)
            if is_superuser:
                await uow.session.execute(text("RESET ROLE"))
                await uow.session.execute(
                    text(
                        "REVOKE SELECT ON mcp_servers, mcp_server_authorizations "
                        "FROM veetbot_mcp_authorization_rls_probe"
                    )
                )
                await uow.session.execute(text("DROP ROLE veetbot_mcp_authorization_rls_probe"))
        await service.remove(app.principal, "isolated")
        async with app.uow_factory() as uow:
            assert await uow.mcp_servers.get_authorization("local", "isolated") is None


class _SlowRefresh(FakeGateway):
    """Hold each refresh open so a second caller must wait for the row lock."""

    def __init__(self) -> None:
        super().__init__(expires_in=600)
        self.in_flight = 0
        self.overlapped = False

    async def refresh(
        self,
        metadata: MCPAuthorizationMetadata,
        registration: MCPClientRegistration,
        *,
        refresh_token: str,
    ) -> MCPTokenGrant:
        self.in_flight += 1
        self.overlapped = self.overlapped or self.in_flight > 1
        await asyncio.sleep(0.2)
        self.in_flight -= 1
        return await super().refresh(metadata, registration, refresh_token=refresh_token)


async def test_concurrent_callers_spend_one_rotating_refresh_token() -> None:
    gateway = _SlowRefresh()
    async with build(settings=_settings(), storage="postgres", mcp_oauth_gateway=gateway) as app:
        service = app.services.mcp_servers
        assert service is not None
        await service.remove(app.principal, "rotating")
        await service.add(
            app.principal, "rotating", "https://rotating.example.com/mcp", MCPServerAccess.CHANGES
        )
        started = await service.begin_sign_in(app.principal, "rotating")
        state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
        assert await service.complete_sign_in(state, "code", None) == ("rotating", "signed_in")
        first, second = await asyncio.gather(
            service.access_token("local", "rotating", force_refresh=True),
            service.access_token("local", "rotating", force_refresh=True),
        )
        await service.remove(app.principal, "rotating")
    assert not gateway.overlapped
    # The second caller refreshed with the token the first one stored.
    assert [call[1] for call in gateway.calls if call[0] == "refresh"] == [
        "refresh-1",
        "refresh-2",
    ]
    assert {first.reveal(), second.reveal()} == {"access-2", "access-3"}


async def _signed_in_service(app: Any, server_id: str) -> Any:
    service = app.services.mcp_servers
    assert service is not None
    await service.remove(app.principal, server_id)
    await service.add(
        app.principal, server_id, f"https://{server_id}.example.com/mcp", MCPServerAccess.CHANGES
    )
    started = await service.begin_sign_in(app.principal, server_id)
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    assert await service.complete_sign_in(state, "code", None) == (server_id, "signed_in")
    return service


async def test_a_rejected_refresh_signs_out_durably() -> None:
    """PR 201 review: the sign-out must commit, not roll back with the PermissionError."""
    gateway = FakeGateway(refresh_error=MCPGrantRejectedError())
    async with build(settings=_settings(), storage="postgres", mcp_oauth_gateway=gateway) as app:
        service = await _signed_in_service(app, "rejected")
        with pytest.raises(PermissionError):
            await service.access_token("local", "rejected", force_refresh=True)
        listed = await service.list(app.principal)
        async with app.uow_factory() as uow:
            authorization = await uow.mcp_servers.get_authorization("local", "rejected")
        await service.remove(app.principal, "rejected")
    assert authorization is not None and not authorization.signed_in
    assert [server.status.value for server in listed.servers] == ["sign_in_required"]
    # The rejected token is not spent again.
    assert [call[0] for call in gateway.calls].count("refresh") == 1


async def test_a_token_without_refresh_signs_out_durably() -> None:
    gateway = FakeGateway()

    async def exchange_without_refresh(*args: Any, **kwargs: Any) -> MCPTokenGrant:
        return MCPTokenGrant(access_token="access-1", expires_in=600)

    gateway.exchange = exchange_without_refresh  # type: ignore[method-assign]
    async with build(settings=_settings(), storage="postgres", mcp_oauth_gateway=gateway) as app:
        service = await _signed_in_service(app, "short")
        with pytest.raises(PermissionError):
            await service.access_token("local", "short", force_refresh=True)
        async with app.uow_factory() as uow:
            authorization = await uow.mcp_servers.get_authorization("local", "short")
        await service.remove(app.principal, "short")
    assert authorization is not None and not authorization.signed_in
