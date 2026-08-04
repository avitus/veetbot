"""PostgreSQL persistence for MCP server configuration and catalog history."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import (
    MCPServerRow,
    MCPToolCatalogRow,
)
from agent_core.domain.mcp import (
    MCPAuthScheme,
    MCPServerConfig,
    MCPToolCatalogRecord,
    MCPTransport,
)
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.ports.determinism import Clock


class PostgresMCPServerRepository:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def put(self, config: MCPServerConfig) -> None:
        values = {
            "id": uuid5(NAMESPACE_URL, f"mcp:{config.tenant_id}:{config.server_id}"),
            "tenant_id": config.tenant_id,
            "server_id": config.server_id,
            "transport": config.transport.value,
            "endpoint": config.endpoint,
            "operator_configured": config.operator_configured,
            "auth_scheme": config.auth_scheme.value,
            "auth_name": config.auth_name,
            "credential_ref": config.credential_ref,
            "token_endpoint": config.token_endpoint,
            "token_scopes": list(config.token_scopes),
            "side_effect": config.side_effect.value,
            "risk": config.risk.value,
            "idempotency": config.idempotency.value,
            "required_scopes": sorted(config.required_scopes),
            "timeout_seconds": config.timeout_seconds,
            "maximum_output_bytes": config.maximum_output_bytes,
            "enabled": config.enabled,
            "created_at": self._clock.now(),
        }
        statement = pg_insert(MCPServerRow).values(**values)
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_mcp_servers_tenant_server",
                set_={
                    key: value for key, value in values.items() if key not in {"id", "created_at"}
                },
            )
        )

    async def list_enabled(self, tenant_id: str) -> list[MCPServerConfig]:
        rows = (
            await self._session.scalars(
                select(MCPServerRow)
                .where(MCPServerRow.tenant_id == tenant_id, MCPServerRow.enabled.is_(True))
                .order_by(MCPServerRow.server_id)
            )
        ).all()
        return [
            MCPServerConfig(
                tenant_id=row.tenant_id,
                server_id=row.server_id,
                transport=MCPTransport(row.transport),
                endpoint=row.endpoint,
                operator_configured=row.operator_configured,
                auth_scheme=MCPAuthScheme(row.auth_scheme),
                auth_name=row.auth_name,
                credential_ref=row.credential_ref,
                token_endpoint=row.token_endpoint,
                token_scopes=tuple(row.token_scopes),
                side_effect=SideEffectClass(row.side_effect),
                risk=RiskLevel(row.risk),
                idempotency=IdempotencyClass(row.idempotency),
                required_scopes=frozenset(row.required_scopes),
                timeout_seconds=row.timeout_seconds,
                maximum_output_bytes=row.maximum_output_bytes,
                enabled=row.enabled,
            )
            for row in rows
        ]

    async def record_catalog(
        self,
        tenant_id: str,
        server_id: str,
        catalog_hash: str,
        records: tuple[MCPToolCatalogRecord, ...],
    ) -> None:
        for record in records:
            if (
                record.tenant_id != tenant_id
                or record.server_id != server_id
                or record.catalog_hash != catalog_hash
            ):
                raise ValueError("MCP catalog record does not match its generation")
        names = {record.remote_name for record in records}
        await self._session.execute(
            update(MCPToolCatalogRow)
            .where(
                MCPToolCatalogRow.tenant_id == tenant_id,
                MCPToolCatalogRow.server_id == server_id,
                MCPToolCatalogRow.withdrawn_at.is_(None),
                MCPToolCatalogRow.remote_name.not_in(names),
            )
            .values(withdrawn_at=self._clock.now())
        )
        for record in records:
            await self._session.execute(
                pg_insert(MCPToolCatalogRow)
                .values(**record.model_dump(mode="python"))
                .on_conflict_do_nothing(constraint="uq_mcp_catalog_generation_tool")
            )
