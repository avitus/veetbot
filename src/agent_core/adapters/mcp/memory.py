"""Deterministic in-memory MCP configuration and catalog history."""

from __future__ import annotations

import asyncio

from agent_core.domain.mcp import MCPServerAuthorization, MCPServerConfig, MCPToolCatalogRecord


def _same_discovery(
    left: MCPToolCatalogRecord,
    right: MCPToolCatalogRecord,
) -> bool:
    return left.registry_name == right.registry_name and left.input_schema == right.input_schema


class InMemoryMCPServerRepository:
    def __init__(self) -> None:
        self._servers: dict[tuple[str, str], MCPServerConfig] = {}
        self._catalog: dict[tuple[str, str, str, str], MCPToolCatalogRecord] = {}
        self._authorizations: dict[tuple[str, str], MCPServerAuthorization] = {}
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

    async def get(self, tenant_id: str, server_id: str) -> MCPServerConfig | None:
        async with self._lock:
            config = self._servers.get((tenant_id, server_id))
            return None if config is None else config.model_copy(deep=True)

    async def delete(self, tenant_id: str, server_id: str) -> bool:
        async with self._lock:
            removed = self._servers.pop((tenant_id, server_id), None) is not None
            # The catalog and the sign-in cascade with their server, as in PostgreSQL.
            self._authorizations.pop((tenant_id, server_id), None)
            for key in [key for key in self._catalog if key[:2] == (tenant_id, server_id)]:
                del self._catalog[key]
            return removed

    async def get_authorization(
        self, tenant_id: str, server_id: str, *, for_update: bool = False
    ) -> MCPServerAuthorization | None:
        del for_update
        async with self._lock:
            return self._authorizations.get((tenant_id, server_id))

    async def find_authorization_by_state(
        self, tenant_id: str, state_hash: str
    ) -> MCPServerAuthorization | None:
        async with self._lock:
            return next(
                (
                    authorization
                    for (candidate_tenant, _), authorization in sorted(self._authorizations.items())
                    if candidate_tenant == tenant_id
                    and authorization.pending_state_hash == state_hash
                ),
                None,
            )

    async def put_authorization(self, authorization: MCPServerAuthorization) -> None:
        async with self._lock:
            key = (authorization.tenant_id, authorization.server_id)
            if key not in self._servers:
                raise ValueError("an MCP sign-in requires its server")
            self._authorizations[key] = authorization

    async def record_catalog(
        self,
        tenant_id: str,
        server_id: str,
        catalog_hash: str,
        records: tuple[MCPToolCatalogRecord, ...],
    ) -> None:
        async with self._lock:
            normalized: dict[str, MCPToolCatalogRecord] = {}
            for record in records:
                if (
                    record.tenant_id != tenant_id
                    or record.server_id != server_id
                    or record.catalog_hash != catalog_hash
                ):
                    raise ValueError("MCP catalog record does not match its generation")
                duplicate = normalized.get(record.remote_name)
                if duplicate is not None and not _same_discovery(duplicate, record):
                    raise ValueError("MCP catalog generation is immutable")
                normalized.setdefault(record.remote_name, record)
                key = (tenant_id, server_id, catalog_hash, record.remote_name)
                existing = self._catalog.get(key)
                if existing is not None and not _same_discovery(existing, record):
                    raise ValueError("MCP catalog generation is immutable")
            for record in normalized.values():
                key = (tenant_id, server_id, catalog_hash, record.remote_name)
                self._catalog.setdefault(key, record.model_copy(deep=True))

    def catalog_records(self) -> tuple[MCPToolCatalogRecord, ...]:
        return tuple(item.model_copy(deep=True) for _, item in sorted(self._catalog.items()))
