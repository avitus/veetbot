"""MCP configuration and adapter-neutral catalog/result values."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass

MCP_SERVER_ID = re.compile(r"^[a-z][a-z0-9_]*$")
MCP_AUTH_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# ADR-0255: an owner-added server's name, and the first-party prefixes it may not take.
OWNER_MCP_SERVER_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RESERVED_MCP_SERVER_PREFIXES = ("gmail_", "bland_", "svp_")
MAXIMUM_OWNER_MCP_SERVERS = 16
MAXIMUM_OWNER_MCP_URL_LENGTH = 2048


def validate_owner_server_url(url: str) -> str:
    """An owner-added server is public HTTPS on port 443 (ADR-0255 decision 2)."""

    if len(url) > MAXIMUM_OWNER_MCP_URL_LENGTH or not url.isascii() or not url.isprintable():
        raise ValueError("MCP server URL is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP server URL is invalid") from exc
    hostname = parsed.hostname or ""
    labels = hostname.rstrip(".").split(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
        or len(labels) < 2
        or labels[-1].isdigit()
        or ":" in hostname
        or hostname.endswith((".internal", ".local", ".localhost", ".home", ".lan"))
    ):
        raise ValueError("MCP server URL must be public HTTPS on port 443")
    return url


class MCPTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class MCPAuthScheme(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    HEADER = "header"
    OAUTH2_CLIENT = "oauth2_client"
    ENV = "env"
    # ADR-0255: the owner's own sign-in; tokens come from the sealed store.
    OAUTH2_AUTHORIZATION = "oauth2_authorization"


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    server_id: str
    transport: MCPTransport
    endpoint: str
    operator_configured: bool = False
    auth_scheme: MCPAuthScheme = MCPAuthScheme.NONE
    auth_name: str | None = None
    credential_ref: str | None = None
    token_endpoint: str | None = None
    token_scopes: tuple[str, ...] = ()
    side_effect: SideEffectClass = SideEffectClass.EXTERNAL_WRITE
    risk: RiskLevel = RiskLevel.HIGH
    idempotency: IdempotencyClass = IdempotencyClass.NON_IDEMPOTENT
    required_scopes: frozenset[str] = frozenset()
    timeout_seconds: int = Field(default=30, gt=0)
    maximum_output_bytes: int = Field(default=1_048_576, gt=0)
    enabled: bool = True
    # ADR-0255: added by the owner in Settings rather than by deployment.
    owner_managed: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> MCPServerConfig:
        if MCP_SERVER_ID.fullmatch(self.server_id) is None:
            raise ValueError("MCP server id is invalid")
        if not self.endpoint.strip():
            raise ValueError("MCP endpoint must not be empty")
        if self.transport is MCPTransport.STDIO and not self.operator_configured:
            raise ValueError("stdio MCP servers must be operator configured")
        if self.owner_managed:
            validate_owner_server_url(self.endpoint)
            if self.transport is not MCPTransport.HTTP or self.operator_configured:
                raise ValueError("owner-added MCP servers are tenant HTTP servers")
            if self.auth_scheme not in {MCPAuthScheme.NONE, MCPAuthScheme.OAUTH2_AUTHORIZATION}:
                raise ValueError("owner-added MCP servers use no credential or a sign-in")
        elif self.auth_scheme is MCPAuthScheme.OAUTH2_AUTHORIZATION:
            raise ValueError("MCP sign-in authentication is only for owner-added servers")
        if self.auth_scheme in {MCPAuthScheme.NONE, MCPAuthScheme.OAUTH2_AUTHORIZATION}:
            if self.credential_ref is not None:
                raise ValueError(
                    f"MCP auth scheme {self.auth_scheme.value} requires no credential reference"
                )
        elif not self.credential_ref:
            raise ValueError("MCP authenticated schemes require a credential reference")
        if self.auth_scheme in {MCPAuthScheme.HEADER, MCPAuthScheme.ENV} and (
            self.auth_name is None or MCP_AUTH_NAME.fullmatch(self.auth_name) is None
        ):
            raise ValueError("MCP header and env authentication require a valid name")
        if (
            self.auth_scheme is MCPAuthScheme.HEADER
            and self.auth_name is not None
            and self.auth_name.lower() == "authorization"
        ):
            raise ValueError("MCP header authentication may not name Authorization")
        if self.auth_scheme is MCPAuthScheme.OAUTH2_CLIENT and not self.token_endpoint:
            raise ValueError("MCP OAuth client authentication requires a token endpoint")
        if self.transport is MCPTransport.HTTP and self.auth_scheme in {
            MCPAuthScheme.BEARER,
            MCPAuthScheme.HEADER,
            MCPAuthScheme.OAUTH2_CLIENT,
        }:
            endpoint = urlsplit(self.endpoint)
            if endpoint.scheme != "https" or endpoint.hostname is None:
                raise ValueError("authenticated MCP HTTP endpoints require absolute HTTPS URLs")
        if self.auth_scheme is MCPAuthScheme.OAUTH2_CLIENT:
            token_endpoint = urlsplit(self.token_endpoint or "")
            if token_endpoint.scheme != "https" or token_endpoint.hostname is None:
                raise ValueError("MCP OAuth token endpoints require absolute HTTPS URLs")
        if self.transport is MCPTransport.HTTP and self.auth_scheme is MCPAuthScheme.ENV:
            raise ValueError("MCP env authentication is valid only for stdio")
        if self.transport is MCPTransport.STDIO and self.auth_scheme in {
            MCPAuthScheme.BEARER,
            MCPAuthScheme.HEADER,
            MCPAuthScheme.OAUTH2_CLIENT,
        }:
            raise ValueError("MCP HTTP authentication schemes are invalid for stdio")
        return self


class MCPRemoteTool(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    input_schema: dict[str, Any]
    application_only: bool = False


class MCPRemotePrompt(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    body: str


class MCPRemoteResource(BaseModel):
    model_config = ConfigDict(frozen=True)

    uri: str
    name: str
    description: str = ""


class MCPDiscovery(BaseModel):
    model_config = ConfigDict(frozen=True)

    tools: tuple[MCPRemoteTool, ...] = ()
    prompts: tuple[MCPRemotePrompt, ...] = ()
    resources: tuple[MCPRemoteResource, ...] = ()


class MCPCallResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: tuple[str, ...] = ()
    structured: dict[str, Any] | None = None
    is_error: bool = False


class ScriptedMCPResponse(BaseModel):
    """Authored deterministic response used by the no-socket harness adapter."""

    model_config = ConfigDict(frozen=True)

    operation: Literal["call_tool", "read_resource"] = "call_tool"
    name: str | None = None
    outcome: Literal["result", "disconnect", "unauthorized"] = "result"
    result: MCPCallResult = Field(default_factory=MCPCallResult)


class ScriptedMCPServer(BaseModel):
    """Adapter-neutral server script loaded from an authored fixture."""

    model_config = ConfigDict(frozen=True)

    name: str
    discovery: MCPDiscovery = Field(default_factory=MCPDiscovery)
    responses: tuple[ScriptedMCPResponse, ...] = ()
    connect_outcome: Literal["connected", "disconnect", "unauthorized"] = "connected"


class MCPToolCatalogRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: str
    server_id: str
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    remote_name: str
    registry_name: str
    input_schema: dict[str, Any]
    discovered_at: datetime
    withdrawn_at: datetime | None = None


class MCPServerAccess(StrEnum):
    """The owner's classification of every tool on one server (ADR-0255 decision 3)."""

    READ_ONLY = "read_only"
    CHANGES = "changes"


OWNER_ACCESS_CLASSIFICATIONS: dict[
    MCPServerAccess, tuple[SideEffectClass, RiskLevel, IdempotencyClass]
] = {
    MCPServerAccess.READ_ONLY: (
        SideEffectClass.NETWORK_READ,
        RiskLevel.LOW,
        IdempotencyClass.READ_ONLY,
    ),
    MCPServerAccess.CHANGES: (
        SideEffectClass.EXTERNAL_WRITE,
        RiskLevel.HIGH,
        IdempotencyClass.NON_IDEMPOTENT,
    ),
}


def server_access(config: MCPServerConfig) -> MCPServerAccess:
    """Read a row's classification back as the access level Settings shows."""

    reads = {SideEffectClass.NONE, SideEffectClass.NETWORK_READ}
    if config.side_effect in reads and config.idempotency is IdempotencyClass.READ_ONLY:
        return MCPServerAccess.READ_ONLY
    return MCPServerAccess.CHANGES


class MCPServerStatus(StrEnum):
    READY = "ready"
    SIGN_IN_REQUIRED = "sign_in_required"


class MCPServerView(BaseModel):
    """One server as Settings shows it; operator rows are read-only (ADR-0255)."""

    model_config = ConfigDict(frozen=True)

    server_id: str
    url: str | None
    managed_by: Literal["owner", "operator"]
    access: MCPServerAccess
    authentication: Literal["none", "sign_in", "operator"]
    status: MCPServerStatus
    sign_in_host: str | None = None
    signed_in_at: datetime | None = None


class MCPServerList(BaseModel):
    model_config = ConfigDict(frozen=True)

    servers: list[MCPServerView]


class MCPSignInStart(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorization_url: str
    callback_scheme: Literal["veetbot"] = "veetbot"
    expires_at: datetime


class MCPAuthorizationMetadata(BaseModel):
    """RFC 9728 and RFC 8414 discovery, validated before it is stored (ADR-0255)."""

    model_config = ConfigDict(frozen=True)

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    scopes: tuple[str, ...] = ()
    token_endpoint_auth_methods: tuple[str, ...] = ()


class MCPClientRegistration(BaseModel):
    """An RFC 7591 registration; the secret is sealed before it is stored."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    client_secret: str | None = None
    auth_method: Literal["none", "client_secret_post", "client_secret_basic"] = "none"


class MCPTokenGrant(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None


class MCPServerAuthorization(BaseModel):
    """One server's sealed sign-in state; every secret field is AES-GCM sealed."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    server_id: str
    metadata: MCPAuthorizationMetadata
    redirect_uri: str | None = None
    client_id: str | None = None
    client_secret_sealed: str | None = None
    client_auth_method: Literal["none", "client_secret_post", "client_secret_basic"] | None = None
    access_token_sealed: str | None = None
    access_expires_at: datetime | None = None
    refresh_token_sealed: str | None = None
    signed_in_at: datetime | None = None
    signed_in_by: str | None = None
    pending_state_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pending_verifier_sealed: str | None = None
    pending_expires_at: datetime | None = None
    pending_principal_id: str | None = None
    # The consumed state whose code is being exchanged; a sign-out clears it.
    exchanging_state_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime

    @property
    def signed_in(self) -> bool:
        return self.refresh_token_sealed is not None or self.access_token_sealed is not None


class MCPProbeResult(BaseModel):
    """What an unauthenticated probe found: an open server, or how to sign in."""

    model_config = ConfigDict(frozen=True)

    authorization: MCPAuthorizationMetadata | None = None
