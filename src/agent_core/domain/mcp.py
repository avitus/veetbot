"""MCP configuration and adapter-neutral catalog/result values."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass

MCP_SERVER_ID = re.compile(r"^[a-z][a-z0-9_]*$")
MCP_AUTH_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class MCPTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class MCPAuthScheme(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    HEADER = "header"
    OAUTH2_CLIENT = "oauth2_client"
    ENV = "env"


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

    @model_validator(mode="after")
    def validate_shape(self) -> MCPServerConfig:
        if MCP_SERVER_ID.fullmatch(self.server_id) is None:
            raise ValueError("MCP server id is invalid")
        if not self.endpoint.strip():
            raise ValueError("MCP endpoint must not be empty")
        if self.transport is MCPTransport.STDIO and not self.operator_configured:
            raise ValueError("stdio MCP servers must be operator configured")
        if self.auth_scheme is MCPAuthScheme.NONE:
            if self.credential_ref is not None:
                raise ValueError("MCP auth scheme none requires no credential reference")
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
