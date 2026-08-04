"""MCP server configuration and catalog-history contract."""

from uuid import UUID

import pytest

from agent_core.adapters.mcp.memory import InMemoryMCPServerRepository
from agent_core.domain.mcp import (
    MCPServerConfig,
    MCPToolCatalogRecord,
    MCPTransport,
)
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
