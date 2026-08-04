"""Deterministic in-memory MCP configuration and catalog history."""

from __future__ import annotations

import asyncio

from agent_core.domain.mcp import MCPServerConfig, MCPToolCatalogRecord


class InMemoryMCPServerRepository:
    def __init__(self) -> None:
        self._servers: dict[tuple[str, str], MCPServerConfig] = {}
        self._catalog: dict[tuple[str, str, str, str], MCPToolCatalogRecord] = {}
        self._lock = asyncio.Lock()

    async def put(self, config: MCPServerConfig) -> None:
        async with self._lock:
            self._servers[(config.tenant_id, config.server_id)] = config.model_copy(deep=True)

    async def list_enabled(self, tenant_id: str) -> list[MCPServerConfig]:
        async with self._lock:
            return [
                config.model_copy(deep=True)
                for (candidate_tenant, _), config in sorted(self._servers.items())
                if candidate_tenant == tenant_id and config.enabled
            ]

    async def record_catalog(
        self,
        tenant_id: str,
        server_id: str,
        catalog_hash: str,
        records: tuple[MCPToolCatalogRecord, ...],
    ) -> None:
        async with self._lock:
            for record in records:
                if (
                    record.tenant_id != tenant_id
                    or record.server_id != server_id
                    or record.catalog_hash != catalog_hash
                ):
                    raise ValueError("MCP catalog record does not match its generation")
                key = (tenant_id, server_id, catalog_hash, record.remote_name)
                existing = self._catalog.get(key)
                if existing is not None and existing != record:
                    raise ValueError("MCP catalog generation is immutable")
            for record in records:
                key = (tenant_id, server_id, catalog_hash, record.remote_name)
                self._catalog[key] = record.model_copy(deep=True)

    def catalog_records(self) -> tuple[MCPToolCatalogRecord, ...]:
        return tuple(item.model_copy(deep=True) for _, item in sorted(self._catalog.items()))
