"""Owner-added MCP servers from Settings, and their sign-in (ADR-0255).

Settings, never the model, adds, reclassifies, removes and signs in to a
server. Every secret is sealed before it reaches a repository, and no
upstream text reaches a response, an event or a log.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlencode, urlsplit

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import (
    ConflictError,
    MCPGrantRejectedError,
    MCPServerSettingsError,
    MCPServerUnavailableError,
    NotFoundError,
)
from agent_core.domain.events import ProcessEvent
from agent_core.domain.mcp import (
    MAXIMUM_OWNER_MCP_SERVERS,
    OWNER_ACCESS_CLASSIFICATIONS,
    OWNER_MCP_SERVER_ID,
    RESERVED_MCP_SERVER_PREFIXES,
    MCPAuthScheme,
    MCPClientRegistration,
    MCPServerAccess,
    MCPServerAuthorization,
    MCPServerConfig,
    MCPServerList,
    MCPServerStatus,
    MCPServerView,
    MCPSignInStart,
    MCPTokenGrant,
    MCPTransport,
    server_access,
    validate_owner_server_url,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.mcp import MCPOAuthGateway, SecretSealer
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

SIGN_IN_WINDOW = timedelta(minutes=10)
# A token this close to expiry is refreshed before it is used (ADR-0255).
REFRESH_SKEW = timedelta(seconds=60)
CALLBACK_SCHEME: Literal["veetbot"] = "veetbot"
type SignInOutcome = Literal["signed_in", "failed", "expired"]


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("ascii")).hexdigest()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class MCPServerSettingsService:
    """The Settings routes' service, and the runtime's source of access tokens."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        gateway: MCPOAuthGateway,
        sealer: SecretSealer,
        callback_url: str,
        tenant_id: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._gateway = gateway
        self._sealer = sealer
        self._callback_url = callback_url
        # The callback carries no principal; the runtime's tenant scopes its lookup.
        self._tenant_id = tenant_id

    # Reading

    async def list(self, principal: Principal) -> MCPServerList:
        require_scope(principal, "settings.read")
        async with self._uow_factory() as uow:
            configs = await uow.mcp_servers.list_enabled(principal.tenant_id)
            views = [
                self._view(
                    config,
                    await uow.mcp_servers.get_authorization(config.tenant_id, config.server_id),
                )
                for config in configs
            ]
        return MCPServerList(servers=views)

    def _view(
        self, config: MCPServerConfig, authorization: MCPServerAuthorization | None
    ) -> MCPServerView:
        if not config.owner_managed:
            return MCPServerView(
                server_id=config.server_id,
                url=None,
                managed_by="operator",
                access=server_access(config),
                authentication="operator",
                status=MCPServerStatus.READY,
            )
        if config.auth_scheme is MCPAuthScheme.NONE:
            return MCPServerView(
                server_id=config.server_id,
                url=config.endpoint,
                managed_by="owner",
                access=server_access(config),
                authentication="none",
                status=MCPServerStatus.READY,
            )
        signed_in = authorization is not None and authorization.signed_in
        return MCPServerView(
            server_id=config.server_id,
            url=config.endpoint,
            managed_by="owner",
            access=server_access(config),
            authentication="sign_in",
            status=MCPServerStatus.READY if signed_in else MCPServerStatus.SIGN_IN_REQUIRED,
            sign_in_host=(
                None
                if authorization is None
                else urlsplit(authorization.metadata.authorization_endpoint).hostname
            ),
            signed_in_at=None if authorization is None else authorization.signed_in_at,
        )

    # Adding, reclassifying and removing

    async def add(
        self, principal: Principal, server_id: str, url: str, access: MCPServerAccess
    ) -> tuple[MCPServerView, bool]:
        """Probe and store a server; an identical replay returns it unchanged."""
        require_scope(principal, "settings.write")
        if OWNER_MCP_SERVER_ID.fullmatch(server_id) is None:
            raise MCPServerSettingsError("MCP server name is invalid")
        try:
            validate_owner_server_url(url)
        except ValueError as exc:
            raise MCPServerSettingsError(str(exc)) from exc
        if server_id.startswith(RESERVED_MCP_SERVER_PREFIXES):
            raise ConflictError("MCP server name is reserved", reason="mcp_server_reserved")
        async with self._uow_factory() as uow:
            existing = await uow.mcp_servers.get(principal.tenant_id, server_id)
            owned = await self._owned_count(uow, principal.tenant_id)
        if existing is not None:
            return await self._current_view(self._replayed(existing, url, access)), False
        if owned >= MAXIMUM_OWNER_MCP_SERVERS:
            raise ConflictError("MCP server limit reached", reason="mcp_server_limit")
        # The probe runs outside any transaction: it is network work.
        probe = await self._gateway.probe(url)
        side_effect, risk, idempotency = OWNER_ACCESS_CLASSIFICATIONS[access]
        config = MCPServerConfig(
            tenant_id=principal.tenant_id,
            server_id=server_id,
            transport=MCPTransport.HTTP,
            endpoint=url,
            owner_managed=True,
            auth_scheme=(
                MCPAuthScheme.NONE
                if probe.authorization is None
                else MCPAuthScheme.OAUTH2_AUTHORIZATION
            ),
            side_effect=side_effect,
            risk=risk,
            idempotency=idempotency,
        )
        now = self._clock.now()
        async with self._uow_factory() as uow:
            raced = await uow.mcp_servers.get(principal.tenant_id, server_id)
            if raced is not None:
                replayed = self._replayed(raced, url, access)
                return self._view(
                    replayed,
                    await uow.mcp_servers.get_authorization(principal.tenant_id, server_id),
                ), False
            if await self._owned_count(uow, principal.tenant_id) >= MAXIMUM_OWNER_MCP_SERVERS:
                raise ConflictError("MCP server limit reached", reason="mcp_server_limit")
            await uow.mcp_servers.put(config)
            authorization = None
            if probe.authorization is not None:
                authorization = MCPServerAuthorization(
                    tenant_id=principal.tenant_id,
                    server_id=server_id,
                    metadata=probe.authorization,
                    updated_at=now,
                )
                await uow.mcp_servers.put_authorization(authorization)
            await self._audit(uow, principal, "settings.mcp_servers.added", config)
        return self._view(config, authorization), True

    @staticmethod
    def _replayed(existing: MCPServerConfig, url: str, access: MCPServerAccess) -> MCPServerConfig:
        """An identical add returns the stored server; any other is a conflict."""
        if not existing.owner_managed:
            raise ConflictError(
                "MCP server is managed by the operator", reason="mcp_server_operator"
            )
        if existing.endpoint != url or server_access(existing) is not access:
            raise ConflictError("MCP server name is taken", reason="mcp_server_exists")
        return existing

    async def _current_view(self, config: MCPServerConfig) -> MCPServerView:
        async with self._uow_factory() as uow:
            authorization = await uow.mcp_servers.get_authorization(
                config.tenant_id, config.server_id
            )
        return self._view(config, authorization)

    @staticmethod
    async def _owned_count(uow: RepositoryUnitOfWork, tenant_id: str) -> int:
        configs = await uow.mcp_servers.list_enabled(tenant_id)
        return sum(1 for config in configs if config.owner_managed)

    async def _owner_server(
        self, uow: RepositoryUnitOfWork, principal: Principal, server_id: str
    ) -> MCPServerConfig:
        config = await uow.mcp_servers.get(principal.tenant_id, server_id)
        if config is None:
            raise NotFoundError("MCP server not found")
        if not config.owner_managed:
            raise ConflictError(
                "MCP server is managed by the operator", reason="mcp_server_operator"
            )
        return config

    async def update_access(
        self, principal: Principal, server_id: str, access: MCPServerAccess
    ) -> MCPServerView:
        require_scope(principal, "settings.write")
        async with self._uow_factory() as uow:
            config = await self._owner_server(uow, principal, server_id)
            if server_access(config) is not access:
                side_effect, risk, idempotency = OWNER_ACCESS_CLASSIFICATIONS[access]
                config = config.model_copy(
                    update={"side_effect": side_effect, "risk": risk, "idempotency": idempotency}
                )
                await uow.mcp_servers.put(config)
                await self._audit(uow, principal, "settings.mcp_servers.updated", config)
            authorization = await uow.mcp_servers.get_authorization(principal.tenant_id, server_id)
        return self._view(config, authorization)

    async def remove(self, principal: Principal, server_id: str) -> None:
        """Remove a server; a server already gone is removed, so a retry is safe."""
        require_scope(principal, "settings.write")
        async with self._uow_factory() as uow:
            config = await uow.mcp_servers.get(principal.tenant_id, server_id)
            if config is None:
                return
            if not config.owner_managed:
                raise ConflictError(
                    "MCP server is managed by the operator", reason="mcp_server_operator"
                )
            await uow.mcp_servers.delete(principal.tenant_id, server_id)
            await self._audit(uow, principal, "settings.mcp_servers.removed", config)

    # Signing in

    async def begin_sign_in(self, principal: Principal, server_id: str) -> MCPSignInStart:
        require_scope(principal, "settings.write")
        async with self._uow_factory() as uow:
            config = await self._owner_server(uow, principal, server_id)
            authorization = await uow.mcp_servers.get_authorization(principal.tenant_id, server_id)
        if config.auth_scheme is not MCPAuthScheme.OAUTH2_AUTHORIZATION or authorization is None:
            raise ConflictError("MCP server needs no sign-in", reason="mcp_sign_in_not_needed")
        if authorization.client_id is None or authorization.redirect_uri != self._callback_url:
            registration = await self._gateway.register(authorization.metadata, self._callback_url)
            authorization = authorization.model_copy(
                update={
                    "redirect_uri": self._callback_url,
                    "client_id": registration.client_id,
                    "client_secret_sealed": (
                        None
                        if registration.client_secret is None
                        else self._sealer.seal(
                            registration.client_secret,
                            self._context(authorization, "client_secret"),
                        )
                    ),
                    "client_auth_method": registration.auth_method,
                }
            )
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce()
        now = self._clock.now()
        expires_at = now + SIGN_IN_WINDOW
        pending = authorization.model_copy(
            update={
                "pending_state_hash": _state_hash(state),
                "pending_verifier_sealed": self._sealer.seal(
                    verifier, self._context(authorization, "verifier")
                ),
                "pending_expires_at": expires_at,
                "pending_principal_id": principal.principal_id,
                "updated_at": now,
            }
        )
        async with self._uow_factory() as uow:
            # The server may have been removed while the client registered.
            await self._owner_server(uow, principal, server_id)
            current = await uow.mcp_servers.get_authorization(
                principal.tenant_id, server_id, for_update=True
            )
            if current is None:
                raise NotFoundError("MCP server not found")
            # Overlay only what this sign-in owns: a refresh may have rotated the
            # tokens while the client registered.
            pending = current.model_copy(
                update={
                    field: getattr(pending, field)
                    for field in (
                        "redirect_uri",
                        "client_id",
                        "client_secret_sealed",
                        "client_auth_method",
                        "pending_state_hash",
                        "pending_verifier_sealed",
                        "pending_expires_at",
                        "pending_principal_id",
                        "updated_at",
                    )
                }
            )
            await uow.mcp_servers.put_authorization(pending)
        query = {
            "response_type": "code",
            "client_id": pending.client_id,
            "redirect_uri": self._callback_url,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": pending.metadata.resource,
        }
        if pending.metadata.scopes:
            query["scope"] = " ".join(pending.metadata.scopes)
        separator = "&" if urlsplit(pending.metadata.authorization_endpoint).query else "?"
        return MCPSignInStart(
            authorization_url=(
                f"{pending.metadata.authorization_endpoint}{separator}{urlencode(query)}"
            ),
            callback_scheme=CALLBACK_SCHEME,
            expires_at=expires_at,
        )

    async def complete_sign_in(
        self, state: str | None, code: str | None, error: str | None
    ) -> tuple[str | None, SignInOutcome]:
        """Consume a callback's state once; failures carry no upstream text."""
        if state is None or not 1 <= len(state) <= 512 or not state.isascii():
            return None, "expired"
        attempt = _state_hash(state)
        now = self._clock.now()
        refused: SignInOutcome | None = None
        async with self._uow_factory() as uow:
            pending = await uow.mcp_servers.find_authorization_by_state(self._tenant_id, attempt)
            if pending is None:
                return None, "expired"
            if (
                pending.pending_expires_at is None
                or pending.pending_expires_at <= now
                or pending.pending_verifier_sealed is None
                or pending.client_id is None
                or pending.redirect_uri is None
            ):
                refused = "expired"
            elif error is not None or code is None or not 1 <= len(code) <= 4096:
                refused = "failed"
            # Single use: the state is spent before anything else can fail, and
            # an exchange that goes ahead is recorded so a sign-out can withdraw it.
            await uow.mcp_servers.put_authorization(
                pending.model_copy(
                    update={
                        "pending_state_hash": None,
                        "pending_verifier_sealed": None,
                        "pending_expires_at": None,
                        "pending_principal_id": None,
                        "exchanging_state_hash": attempt if refused is None else None,
                        "updated_at": now,
                    }
                )
            )
        server_id = pending.server_id
        if refused is not None:
            return server_id, refused
        assert pending.pending_verifier_sealed is not None and pending.redirect_uri is not None
        assert code is not None
        verifier = self._sealer.open(
            pending.pending_verifier_sealed, self._context(pending, "verifier")
        )
        try:
            grant = await self._gateway.exchange(
                pending.metadata,
                self._registration(pending),
                code=code,
                verifier=verifier.reveal(),
                redirect_uri=pending.redirect_uri,
            )
        except (MCPGrantRejectedError, MCPServerUnavailableError):
            return server_id, "failed"
        completed = self._clock.now()
        async with self._uow_factory() as uow:
            current = await uow.mcp_servers.get_authorization(
                pending.tenant_id, server_id, for_update=True
            )
            # A removal or sign-out during the exchange withdrew this attempt.
            if current is None or current.exchanging_state_hash != attempt:
                return server_id, "failed"
            signed_in = current.model_copy(
                update={
                    **self._sealed_grant(current, grant, completed),
                    "signed_in_at": completed,
                    "signed_in_by": pending.pending_principal_id,
                    "exchanging_state_hash": None,
                    "updated_at": completed,
                }
            )
            await uow.mcp_servers.put_authorization(signed_in)
            event_id = self._ids.new_id()
            await uow.process_events.append(
                ProcessEvent(
                    id=event_id,
                    event_type="settings.mcp_servers.signed_in",
                    actor_type="principal",
                    actor_id=pending.pending_principal_id,
                    payload={"tenant_id": pending.tenant_id, "server_id": server_id},
                    derivation_key=(
                        f"settings.mcp_servers.signed_in:{pending.tenant_id}:{server_id}:{event_id}"
                    ),
                    created_at=completed,
                )
            )
        return server_id, "signed_in"

    async def sign_out(self, principal: Principal, server_id: str) -> None:
        require_scope(principal, "settings.write")
        async with self._uow_factory() as uow:
            config = await self._owner_server(uow, principal, server_id)
            authorization = await uow.mcp_servers.get_authorization(
                principal.tenant_id, server_id, for_update=True
            )
            if authorization is None or not (
                authorization.signed_in
                or authorization.pending_state_hash is not None
                or authorization.exchanging_state_hash is not None
            ):
                return
            # Signing out also withdraws a sign-in in progress, so a callback still
            # exchanging its code stores nothing.
            await uow.mcp_servers.put_authorization(
                self._signed_out(authorization).model_copy(
                    update={
                        "pending_state_hash": None,
                        "pending_verifier_sealed": None,
                        "pending_expires_at": None,
                        "pending_principal_id": None,
                        "exchanging_state_hash": None,
                        "updated_at": self._clock.now(),
                    }
                )
            )
            await self._audit(uow, principal, "settings.mcp_servers.signed_out", config)

    # The runtime's token source

    async def access_token(
        self, tenant_id: str, server_id: str, *, force_refresh: bool = False
    ) -> SecretValue:
        """A current token; PermissionError when there is no sign-in to use."""
        signed_out: str | None = None
        async with self._uow_factory() as uow:
            authorization = await uow.mcp_servers.get_authorization(
                tenant_id, server_id, for_update=True
            )
            if authorization is None or not authorization.signed_in:
                raise PermissionError("MCP server is not signed in")
            now = self._clock.now()
            fresh = authorization.access_token_sealed is not None and (
                authorization.access_expires_at is None
                or authorization.access_expires_at - REFRESH_SKEW > now
            )
            if fresh and not force_refresh:
                assert authorization.access_token_sealed is not None
                return self._sealer.open(
                    authorization.access_token_sealed,
                    self._context(authorization, "access_token"),
                )
            if authorization.refresh_token_sealed is None:
                # An expired or rejected token with nothing to refresh it is a sign-out.
                await uow.mcp_servers.put_authorization(self._signed_out(authorization))
                signed_out = "MCP sign-in expired"
            else:
                refresh_token = self._sealer.open(
                    authorization.refresh_token_sealed,
                    self._context(authorization, "refresh_token"),
                )
                try:
                    # The row stays locked, so no other process spends this token.
                    grant = await self._gateway.refresh(
                        authorization.metadata,
                        self._registration(authorization),
                        refresh_token=refresh_token.reveal(),
                    )
                except MCPGrantRejectedError:
                    await uow.mcp_servers.put_authorization(self._signed_out(authorization))
                    signed_out = "MCP sign-in was rejected"
                except MCPServerUnavailableError:
                    raise PermissionError("MCP authorization server is unavailable") from None
                else:
                    refreshed = authorization.model_copy(
                        update={
                            **self._sealed_grant(authorization, grant, self._clock.now()),
                            "updated_at": self._clock.now(),
                        }
                    )
                    await uow.mcp_servers.put_authorization(refreshed)
                    return SecretValue(grant.access_token)
        # Raised only once the sign-out has committed; raising inside the unit of
        # work would roll it back and leave a rejected token to be spent again.
        assert signed_out is not None
        raise PermissionError(signed_out)

    # Helpers

    @staticmethod
    def _context(authorization: MCPServerAuthorization, field: str) -> str:
        return f"{authorization.tenant_id}|{authorization.server_id}|{field}"

    def _registration(self, authorization: MCPServerAuthorization) -> MCPClientRegistration:
        if authorization.client_id is None:
            raise PermissionError("MCP server has no client registration")
        return MCPClientRegistration(
            client_id=authorization.client_id,
            client_secret=(
                None
                if authorization.client_secret_sealed is None
                else self._sealer.open(
                    authorization.client_secret_sealed,
                    self._context(authorization, "client_secret"),
                ).reveal()
            ),
            auth_method=authorization.client_auth_method or "none",
        )

    def _sealed_grant(
        self,
        authorization: MCPServerAuthorization,
        grant: MCPTokenGrant,
        now: datetime,
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "access_token_sealed": self._sealer.seal(
                grant.access_token, self._context(authorization, "access_token")
            ),
            "access_expires_at": (
                None if grant.expires_in is None else now + timedelta(seconds=grant.expires_in)
            ),
        }
        if grant.refresh_token is not None:
            # A rotated refresh token replaces the old one in the same write.
            values["refresh_token_sealed"] = self._sealer.seal(
                grant.refresh_token, self._context(authorization, "refresh_token")
            )
        return values

    @staticmethod
    def _signed_out(authorization: MCPServerAuthorization) -> MCPServerAuthorization:
        return authorization.model_copy(
            update={
                "access_token_sealed": None,
                "access_expires_at": None,
                "refresh_token_sealed": None,
                "signed_in_at": None,
                "signed_in_by": None,
            }
        )

    async def _audit(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        event_type: str,
        config: MCPServerConfig,
    ) -> None:
        now = self._clock.now()
        event_id = self._ids.new_id()
        await uow.process_events.append(
            ProcessEvent(
                id=event_id,
                event_type=event_type,
                actor_type="principal",
                actor_id=principal.principal_id,
                payload={
                    "tenant_id": principal.tenant_id,
                    "server_id": config.server_id,
                    "access": server_access(config).value,
                },
                # Keyed by the event's own id: two changes in one clock tick are two events.
                derivation_key=f"{event_type}:{principal.tenant_id}:{config.server_id}:{event_id}",
                created_at=now,
            )
        )
