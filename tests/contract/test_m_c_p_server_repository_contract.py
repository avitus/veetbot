"""MCP server configuration and catalog-history contract."""

from uuid import UUID

import pytest

from agent_core.adapters.mcp.memory import InMemoryMCPServerRepository
from agent_core.domain.mcp import (
    MCPAuthorizationMetadata,
    MCPAuthScheme,
    MCPServerAuthorization,
    MCPServerConfig,
    MCPToolCatalogRecord,
    MCPTransport,
)
from agent_core.ports.mcp import MCPServerRepository
from tests.contract.support import NOW


async def test_mcp_server_repository_scopes_servers_and_records_catalogs() -> None:
    repository = InMemoryMCPServerRepository()
    config = MCPServerConfig(
        tenant_id="tenant-a",
        server_id="docs",
        transport=MCPTransport.STDIO,
        endpoint="server",
        operator_configured=True,
    )
    await repository.put(config)
    assert await repository.list_enabled("tenant-a") == [config]
    assert await repository.list_enabled("tenant-b") == []
    record = MCPToolCatalogRecord(
        id=UUID(int=1),
        tenant_id="tenant-a",
        server_id="docs",
        catalog_hash="a" * 64,
        remote_name="search",
        registry_name="mcp.docs.search",
        input_schema={"type": "object"},
        discovered_at=NOW,
    )
    await repository.record_catalog("tenant-a", "docs", "a" * 64, (record,))
    assert repository.catalog_records() == (record,)

    equivalent = record.model_copy(update={"id": UUID(int=2)})
    await repository.record_catalog("tenant-a", "docs", "a" * 64, (equivalent,))
    assert repository.catalog_records() == (record,)

    conflicting = record.model_copy(
        update={
            "id": UUID(int=3),
            "registry_name": "mcp.docs.different",
        }
    )
    with pytest.raises(ValueError, match="immutable"):
        await repository.record_catalog(
            "tenant-a",
            "docs",
            "a" * 64,
            (equivalent, conflicting),
        )
    assert repository.catalog_records() == (record,)

    with pytest.raises(ValueError, match="immutable"):
        await repository.record_catalog("tenant-a", "docs", "a" * 64, (conflicting,))
    assert repository.catalog_records() == (record,)


AUTHORIZATION_SERVER = "https://auth.example.com"


def _sealed(field: str) -> str:
    """A placeholder in the sealed-value format; the repository never opens it."""
    return f"v1.{field}"


OWNER_ROW = MCPServerConfig(
    tenant_id="local",
    server_id="design",
    transport=MCPTransport.HTTP,
    endpoint="https://mcp.example.com/mcp",
    owner_managed=True,
    auth_scheme=MCPAuthScheme.OAUTH2_AUTHORIZATION,
)
SIGN_IN_METADATA = MCPAuthorizationMetadata(
    resource="https://mcp.example.com/mcp",
    issuer="https://auth.example.com/",
    authorization_endpoint=AUTHORIZATION_SERVER + "/authorize",
    token_endpoint=AUTHORIZATION_SERVER + "/token",
    registration_endpoint="https://auth.example.com/register",
    scopes=("read",),
)


async def assert_owner_server_repository_contract(repository: MCPServerRepository) -> None:
    """ADR-0255: owner rows, their sealed sign-in, the callback's lookup and the cascade.

    The rows use the "local" tenant, which a PostgreSQL runtime's row-level security admits.
    """
    tenant = OWNER_ROW.tenant_id
    await repository.put(OWNER_ROW)
    assert await repository.get(tenant, "design") == OWNER_ROW
    assert await repository.get(tenant, "missing") is None
    pending = MCPServerAuthorization(
        tenant_id=tenant,
        server_id="design",
        metadata=SIGN_IN_METADATA,
        redirect_uri="https://api.example.com/v1/mcp-oauth/callback",
        client_id="client-1",
        client_secret_sealed=_sealed("client"),
        client_auth_method="client_secret_post",
        pending_state_hash="b" * 64,
        pending_verifier_sealed=_sealed("verifier"),
        pending_expires_at=NOW,
        pending_principal_id="owner",
        exchanging_state_hash="d" * 64,
        updated_at=NOW,
    )
    await repository.put_authorization(pending)
    assert await repository.get_authorization(tenant, "design") == pending
    assert await repository.get_authorization(tenant, "design", for_update=True) == pending
    assert await repository.find_authorization_by_state(tenant, "b" * 64) == pending
    assert await repository.find_authorization_by_state(tenant, "c" * 64) is None
    signed_in = pending.model_copy(
        update={
            "pending_state_hash": None,
            "pending_verifier_sealed": None,
            "pending_expires_at": None,
            "pending_principal_id": None,
            "exchanging_state_hash": None,
            "access_token_sealed": _sealed("access"),
            "access_expires_at": NOW,
            "refresh_token_sealed": _sealed("refresh"),
            "signed_in_at": NOW,
            "signed_in_by": "owner",
        }
    )
    await repository.put_authorization(signed_in)
    assert await repository.get_authorization(tenant, "design") == signed_in
    assert await repository.find_authorization_by_state(tenant, "b" * 64) is None
    assert await repository.delete(tenant, "design") is True
    assert await repository.delete(tenant, "design") is False
    assert await repository.get_authorization(tenant, "design") is None
    with pytest.raises(ValueError, match="requires its server"):
        await repository.put_authorization(signed_in)


async def test_in_memory_owner_server_repository_contract() -> None:
    await assert_owner_server_repository_contract(InMemoryMCPServerRepository())
