"""An owner adds an MCP server and signs in to it over HTTP, on PostgreSQL.

Guards ADR-0255 (docs/adr/0255-owner-added-mcp-servers.md, decisions 2 to 6)
and the "Owner MCP servers" routes of docs/plan/http-api-and-streaming.md end
to end through the Settings routes, the public callback and the durable
store, with only the authorization server's answers authored:

- adding a server that answers 401 records its authorization server and reads
  ``sign_in_required``;
- the ten-minute, single-use sign-in state lives in PostgreSQL, so the
  callback completes in a different API process from the one that started it;
- the sealed tokens the callback stores are usable by another process, the
  way the worker asks for them when it connects;
- signing out forgets the tokens and keeps the client registration, and
  deleting the server deletes its authorization with it;
- writes need ``settings.write``, an unreachable server is
  ``502 mcp_server_unavailable`` and stores nothing.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.errors import MCPServerUnavailableError
from tests.gates.test_owner_mcp_servers_adr0255 import FakeGateway
from tests.integration.journey_support import api_client
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
TENANT = "local"
SERVER = "designs"
# The fake authorization server protects this resource.
URL = "https://mcp.example.com/mcp"
CALLBACK = "http://127.0.0.1:8000/v1/mcp-oauth/callback"
# What the fake authorization server issues; none may be stored readable.
ISSUED = ("client-secret-1", "access-1", "refresh-1")


def _settings() -> Settings:
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return replace(
        database_settings(),
        mcp_servers_enabled=True,
        mcp_authorization_key=SecretStr(key),
        mcp_oauth_callback_url=CALLBACK,
    )


def _reader() -> Principal:
    return Principal(
        tenant_id=TENANT, principal_id="local-user", roles={"user"}, scopes={"settings.read"}
    )


async def test_sign_in_started_in_one_process_completes_in_another_and_is_forgotten() -> None:
    """Add, sign in across a restart, use the token, sign out and remove the server."""
    gateway = FakeGateway()
    clock = FixedClock(NOW)
    settings = _settings()

    async with (
        build(settings=settings, storage="postgres", clock=clock, mcp_oauth_gateway=gateway) as app,
        api_client(app) as client,
    ):
        created = await client.post(
            "/v1/mcp-servers", json={"server_id": SERVER, "url": URL, "access": "changes"}
        )
        assert created.status_code == 201, created.text
        assert created.json() == {
            "server_id": SERVER,
            "url": URL,
            "managed_by": "owner",
            "access": "changes",
            "authentication": "sign_in",
            "status": "sign_in_required",
            "sign_in_host": "auth.example.com",
            "signed_in_at": None,
        }
        replayed = await client.post(
            "/v1/mcp-servers", json={"server_id": SERVER, "url": URL, "access": "changes"}
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == created.json()

        started = await client.post(f"/v1/mcp-servers/{SERVER}/sign-in")
        assert started.status_code == 200, started.text
        assert started.json()["callback_scheme"] == "veetbot"
        assert datetime.fromisoformat(started.json()["expires_at"]) == NOW + timedelta(minutes=10)
        authorization_url = urlsplit(started.json()["authorization_url"])
        assert authorization_url.netloc == "auth.example.com"
        query = parse_qs(authorization_url.query)
        assert query["redirect_uri"] == [CALLBACK]
        assert query["code_challenge_method"] == ["S256"]
        assert query["resource"] == [URL]
        state = query["state"][0]

    # The authorization server redirects to whichever API process is serving.
    async with (
        build(settings=settings, storage="postgres", clock=clock, mcp_oauth_gateway=gateway) as app,
        api_client(app) as client,
    ):
        callback = await client.get(
            "/v1/mcp-oauth/callback", params={"state": state, "code": "the-code"}
        )
        assert callback.status_code == 303, callback.text
        assert (
            callback.headers["Location"]
            == f"veetbot://mcp-sign-in?server_id={SERVER}&outcome=signed_in"
        )
        assert "the-code" not in callback.text
        # The state is single-use.
        spent = await client.get(
            "/v1/mcp-oauth/callback", params={"state": state, "code": "the-code"}
        )
        assert spent.status_code == 303
        assert spent.headers["Location"] == "veetbot://mcp-sign-in?outcome=expired"

        listed = await client.get("/v1/mcp-servers")
        assert listed.status_code == 200, listed.text
        [server] = listed.json()["servers"]
        assert server["status"] == "ready"
        assert datetime.fromisoformat(server["signed_in_at"]) == NOW
        async with app.uow_factory() as uow:
            stored = await uow.mcp_servers.get_authorization(TENANT, SERVER)
        assert stored is not None and stored.signed_in
        sealed = stored.model_dump_json()
        assert all(value not in sealed for value in ISSUED)

    exchanged = [call[1] for call in gateway.calls if call[0] == "exchange"]
    assert len(exchanged) == 1
    assert exchanged[0][0] == "the-code" and exchanged[0][2] == CALLBACK
    assert exchanged[0][3].client_secret == ISSUED[0]

    # A third process, as the worker is, connects with the stored token.
    async with (
        build(settings=settings, storage="postgres", clock=clock, mcp_oauth_gateway=gateway) as app,
        api_client(app) as client,
    ):
        service = app.services.mcp_servers
        assert service is not None
        token = await service.access_token(TENANT, SERVER)
        assert token.reveal() == ISSUED[1]

        patched = await client.patch(f"/v1/mcp-servers/{SERVER}", json={"access": "read_only"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["access"] == "read_only"
        assert patched.json()["status"] == "ready"

        signed_out = await client.delete(f"/v1/mcp-servers/{SERVER}/sign-in")
        assert signed_out.status_code == 204, signed_out.text
        listed = await client.get("/v1/mcp-servers")
        [server] = listed.json()["servers"]
        assert server["status"] == "sign_in_required" and server["signed_in_at"] is None
        assert server["access"] == "read_only"
        async with app.uow_factory() as uow:
            stored = await uow.mcp_servers.get_authorization(TENANT, SERVER)
        assert stored is not None and not stored.signed_in
        with pytest.raises(PermissionError):
            await service.access_token(TENANT, SERVER)

        # The registration is kept for the same callback URL.
        again = await client.post(f"/v1/mcp-servers/{SERVER}/sign-in")
        assert again.status_code == 200, again.text
        assert [call[0] for call in gateway.calls].count("register") == 1

        removed = await client.delete(f"/v1/mcp-servers/{SERVER}")
        retried = await client.delete(f"/v1/mcp-servers/{SERVER}")
        assert (removed.status_code, retried.status_code) == (204, 204)
        assert (await client.get("/v1/mcp-servers")).json() == {"servers": []}
        async with app.uow_factory() as uow:
            assert await uow.mcp_servers.get(TENANT, SERVER) is None
            assert await uow.mcp_servers.get_authorization(TENANT, SERVER) is None
            signed_in = await uow.process_events.list("settings.mcp_servers.signed_in")
        assert [event.payload for event in signed_in] == [
            {"tenant_id": TENANT, "server_id": SERVER}
        ]
        # A callback for the withdrawn sign-in finds nothing to complete.
        late_state = parse_qs(urlsplit(again.json()["authorization_url"]).query)["state"][0]
        late = await client.get("/v1/mcp-oauth/callback", params={"state": late_state, "code": "c"})
        assert late.status_code == 303
        assert late.headers["Location"].endswith("outcome=expired")
        assert (await client.get("/v1/mcp-servers")).json() == {"servers": []}


async def test_a_sign_in_state_expires_after_ten_minutes_across_a_restart() -> None:
    """The stored state is the authority, and it is refused once its ten minutes pass."""
    gateway = FakeGateway()
    clock = FixedClock(NOW)
    settings = _settings()
    async with (
        build(settings=settings, storage="postgres", clock=clock, mcp_oauth_gateway=gateway) as app,
        api_client(app) as client,
    ):
        created = await client.post(
            "/v1/mcp-servers", json={"server_id": SERVER, "url": URL, "access": "read_only"}
        )
        assert created.status_code == 201, created.text
        started = await client.post(f"/v1/mcp-servers/{SERVER}/sign-in")
        state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]

    clock.advance(timedelta(minutes=11))
    async with (
        build(settings=settings, storage="postgres", clock=clock, mcp_oauth_gateway=gateway) as app,
        api_client(app) as client,
    ):
        callback = await client.get("/v1/mcp-oauth/callback", params={"state": state, "code": "c"})
        assert callback.status_code == 303
        assert (
            callback.headers["Location"]
            == f"veetbot://mcp-sign-in?server_id={SERVER}&outcome=expired"
        )
        [server] = (await client.get("/v1/mcp-servers")).json()["servers"]
        assert server["status"] == "sign_in_required"
    assert "exchange" not in [call[0] for call in gateway.calls]


async def test_writes_need_the_write_scope_and_an_unreachable_server_stores_nothing() -> None:
    """A reader may list but not change; a failed probe leaves no row behind."""
    gateway = FakeGateway(probe_error=MCPServerUnavailableError())
    body = {"server_id": SERVER, "url": URL, "access": "changes"}
    async with build(
        settings=_settings(), storage="postgres", fixed_clock_at=NOW, mcp_oauth_gateway=gateway
    ) as app:
        async with api_client(app, principal=_reader()) as reader:
            assert (await reader.get("/v1/mcp-servers")).status_code == 200
            for method, path in (
                ("POST", "/v1/mcp-servers"),
                ("PATCH", f"/v1/mcp-servers/{SERVER}"),
                ("DELETE", f"/v1/mcp-servers/{SERVER}"),
                ("POST", f"/v1/mcp-servers/{SERVER}/sign-in"),
                ("DELETE", f"/v1/mcp-servers/{SERVER}/sign-in"),
            ):
                refused = await reader.request(method, path, json=body)
                assert refused.status_code == 403, (method, path, refused.text)
        assert gateway.calls == []

        async with api_client(app) as client:
            malformed = await client.post(
                "/v1/mcp-servers", json={**body, "url": "http://mcp.example.com/mcp"}
            )
            assert malformed.status_code == 400, malformed.text
            assert malformed.json()["error"]["code"] == "malformed_request"
            unavailable = await client.post("/v1/mcp-servers", json=body)
            assert unavailable.status_code == 502, unavailable.text
            assert unavailable.json()["error"]["code"] == "mcp_server_unavailable"
            missing = await client.post(f"/v1/mcp-servers/{SERVER}/sign-in")
            assert missing.status_code == 404, missing.text
            assert (await client.get("/v1/mcp-servers")).json() == {"servers": []}
        async with app.uow_factory() as uow:
            assert await uow.mcp_servers.get(TENANT, SERVER) is None
