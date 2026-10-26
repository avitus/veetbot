"""PostgreSQL persistence for MCP server configuration and catalog history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import (
    MCPServerAuthorizationRow,
    MCPServerRow,
    MCPToolCatalogRow,
)
from agent_core.domain.mcp import (
    MCPAuthScheme,
    MCPServerAuthorization,
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
            "owner_managed": config.owner_managed,
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
            await self._session.execute(
                select(MCPServerRow.__table__)
                .where(MCPServerRow.tenant_id == tenant_id, MCPServerRow.enabled.is_(True))
                .order_by(MCPServerRow.server_id)
            )
        ).mappings()
        return [_config(dict(row)) for row in rows]

    async def get(self, tenant_id: str, server_id: str) -> MCPServerConfig | None:
        row = (
            (
                await self._session.execute(
                    select(MCPServerRow.__table__).where(
                        MCPServerRow.tenant_id == tenant_id, MCPServerRow.server_id == server_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _config(dict(row))

    async def delete(self, tenant_id: str, server_id: str) -> bool:
        """The catalog history and the sign-in cascade with the server row."""
        removed = await self._session.scalar(
            delete(MCPServerRow)
            .where(MCPServerRow.tenant_id == tenant_id, MCPServerRow.server_id == server_id)
            .returning(MCPServerRow.server_id)
        )
        return removed is not None

    async def get_authorization(
        self, tenant_id: str, server_id: str, *, for_update: bool = False
    ) -> MCPServerAuthorization | None:
        statement = select(MCPServerAuthorizationRow.__table__).where(
            MCPServerAuthorizationRow.tenant_id == tenant_id,
            MCPServerAuthorizationRow.server_id == server_id,
        )
        if for_update:
            # The API and the worker refresh one rotating token in turn (ADR-0255).
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).mappings().one_or_none()
        return None if row is None else MCPServerAuthorization.model_validate(dict(row))

    async def find_authorization_by_state(
        self, tenant_id: str, state_hash: str
    ) -> MCPServerAuthorization | None:
        row = (
            (
                await self._session.execute(
                    select(MCPServerAuthorizationRow.__table__)
                    .where(
                        MCPServerAuthorizationRow.tenant_id == tenant_id,
                        MCPServerAuthorizationRow.pending_state_hash == state_hash,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        # Column names are the domain field names, "metadata" included.
        return None if row is None else MCPServerAuthorization.model_validate(dict(row))

    async def put_authorization(self, authorization: MCPServerAuthorization) -> None:
        server = await self._session.scalar(
            select(MCPServerRow.id).where(
                MCPServerRow.tenant_id == authorization.tenant_id,
                MCPServerRow.server_id == authorization.server_id,
            )
        )
        if server is None:
            raise ValueError("an MCP sign-in requires its server")
        values = {
            "tenant_id": authorization.tenant_id,
            "server_id": authorization.server_id,
            "metadata_document": authorization.metadata.model_dump(mode="json"),
            "redirect_uri": authorization.redirect_uri,
            "client_id": authorization.client_id,
            "client_secret_sealed": authorization.client_secret_sealed,
            "client_auth_method": authorization.client_auth_method,
            "access_token_sealed": authorization.access_token_sealed,
            "access_expires_at": authorization.access_expires_at,
            "refresh_token_sealed": authorization.refresh_token_sealed,
            "signed_in_at": authorization.signed_in_at,
            "signed_in_by": authorization.signed_in_by,
            "pending_state_hash": authorization.pending_state_hash,
            "pending_verifier_sealed": authorization.pending_verifier_sealed,
            "pending_expires_at": authorization.pending_expires_at,
            "pending_principal_id": authorization.pending_principal_id,
            "updated_at": authorization.updated_at,
        }
        statement = pg_insert(MCPServerAuthorizationRow).values(**values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "server_id"],
                # Keyed by column, since the metadata attribute and column names differ.
                set_={
                    getattr(MCPServerAuthorizationRow, key): value
                    for key, value in values.items()
                    if key not in {"tenant_id", "server_id"}
                },
            )
        )

    async def record_catalog(
        self,
        tenant_id: str,
        server_id: str,
        catalog_hash: str,
        records: tuple[MCPToolCatalogRecord, ...],
    ) -> None:
        """Validate and record an immutable generation with one bulk tool insertion."""
        normalized: dict[str, MCPToolCatalogRecord] = {}
        for record in records:
            if (
                record.tenant_id != tenant_id
                or record.server_id != server_id
                or record.catalog_hash != catalog_hash
            ):
                raise ValueError("MCP catalog record does not match its generation")
            duplicate = normalized.get(record.remote_name)
            if duplicate is not None and not self._same_discovery(duplicate, record):
                raise ValueError("MCP catalog generation is immutable")
            normalized.setdefault(record.remote_name, record)
        names = set(normalized)
        existing_rows = (
            await self._session.scalars(
                select(MCPToolCatalogRow).where(
                    MCPToolCatalogRow.tenant_id == tenant_id,
                    MCPToolCatalogRow.server_id == server_id,
                    MCPToolCatalogRow.catalog_hash == catalog_hash,
                    MCPToolCatalogRow.remote_name.in_(names),
                )
            )
        ).all()
        existing = {row.remote_name: row for row in existing_rows}
        for remote_name, record in normalized.items():
            row = existing.get(remote_name)
            if row is not None and (
                row.registry_name != record.registry_name or row.input_schema != record.input_schema
            ):
                raise ValueError("MCP catalog generation is immutable")
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
        if normalized:
            # One statement for the whole generation rather than one per tool.
            await self._session.execute(
                pg_insert(MCPToolCatalogRow)
                .values([record.model_dump(mode="python") for record in normalized.values()])
                .on_conflict_do_nothing(constraint="uq_mcp_catalog_generation_tool")
            )
        if normalized:
            persisted_rows = (
                await self._session.scalars(
                    select(MCPToolCatalogRow).where(
                        MCPToolCatalogRow.tenant_id == tenant_id,
                        MCPToolCatalogRow.server_id == server_id,
                        MCPToolCatalogRow.catalog_hash == catalog_hash,
                        MCPToolCatalogRow.remote_name.in_(names),
                    )
                )
            ).all()
            persisted = {row.remote_name: row for row in persisted_rows}
            for remote_name, record in normalized.items():
                row = persisted[remote_name]
                if (
                    row.registry_name != record.registry_name
                    or row.input_schema != record.input_schema
                ):
                    raise ValueError("MCP catalog generation is immutable")

    @staticmethod
    def _same_discovery(
        left: MCPToolCatalogRecord,
        right: MCPToolCatalogRecord,
    ) -> bool:
        return left.registry_name == right.registry_name and left.input_schema == right.input_schema


def _config(row: Mapping[str, Any]) -> MCPServerConfig:
    """Rebuild a configuration from its row's columns."""
    return MCPServerConfig(
        tenant_id=row["tenant_id"],
        server_id=row["server_id"],
        transport=MCPTransport(row["transport"]),
        endpoint=row["endpoint"],
        operator_configured=row["operator_configured"],
        auth_scheme=MCPAuthScheme(row["auth_scheme"]),
        auth_name=row["auth_name"],
        credential_ref=row["credential_ref"],
        token_endpoint=row["token_endpoint"],
        token_scopes=tuple(row["token_scopes"]),
        side_effect=SideEffectClass(row["side_effect"]),
        risk=RiskLevel(row["risk"]),
        idempotency=IdempotencyClass(row["idempotency"]),
        required_scopes=frozenset(row["required_scopes"]),
        timeout_seconds=row["timeout_seconds"],
        maximum_output_bytes=row["maximum_output_bytes"],
        enabled=row["enabled"],
        owner_managed=row["owner_managed"],
    )
