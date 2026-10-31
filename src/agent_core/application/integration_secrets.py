"""Write-only owner key management and grant-bound resolution."""

from __future__ import annotations

import hmac
from dataclasses import replace
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.integration_secrets import (
    AuthScheme,
    IntegrationSecret,
    secret_id,
    validate_binding,
    validate_value,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.mcp import SecretSealer
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class IntegrationSecrets:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, sealer: SecretSealer, clock: Clock, ids: IdFactory
    ) -> None:
        self.uow_factory, self.sealer, self.clock, self.ids = uow_factory, sealer, clock, ids

    async def list(self, principal: Principal) -> list[dict[str, object]]:
        require_scope(principal, "settings.read")
        async with self.uow_factory() as uow:
            return [row.metadata() for row in await uow.integration_secrets.list(principal)]

    async def put(
        self,
        principal: Principal,
        key_id: UUID,
        *,
        label: str,
        origin: str,
        auth_scheme: AuthScheme,
        value: SecretValue,
        expected_revision: int,
    ) -> dict[str, object]:
        require_scope(principal, "settings.write")
        validate_binding(label, origin, auth_scheme)
        validate_value(value.reveal())
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid expected revision.")
        async with self.uow_factory() as uow, uow.integration_secrets.locked(principal):
            previous = await uow.integration_secrets.get(principal, key_id)
            if previous is None:
                if expected_revision != 0:
                    raise NotFoundError("Integration key not found.")
                if len(await uow.integration_secrets.list(principal)) >= 100:
                    raise ConflictError("Remove an integration key before adding another.")
            else:
                if previous.deleted_at is not None or (
                    previous.label,
                    previous.origin,
                    previous.auth_scheme,
                ) != (label, origin, auth_scheme):
                    raise ConflictError("Create a new key to change its binding.")
                # A misprovisioned host key must never overwrite a value it cannot open.
                if previous.sealed_value is None:
                    raise PermissionError("The integration key cannot be opened.")
                current_value = self.sealer.open(previous.sealed_value, previous.context())
                if previous.revision != expected_revision:
                    if previous.revision == expected_revision + 1 and hmac.compare_digest(
                        current_value.reveal(),
                        value.reveal(),
                    ):
                        return previous.metadata()
                    raise ConflictError("Integration key changed; reload Settings.")
            now = self.clock.now()
            row = IntegrationSecret(
                principal.tenant_id,
                principal.principal_id,
                key_id,
                label,
                origin,
                auth_scheme,
                expected_revision + 1,
                previous.created_at if previous else now,
                now,
                None,
            )
            row = replace(row, sealed_value=self.sealer.seal(value.reveal(), row.context()))
            await uow.integration_secrets.put(principal, row)
            await self._audit(uow, principal, row, "created" if previous is None else "replaced")
            return row.metadata()

    async def delete(self, principal: Principal, key_id: UUID, expected_revision: int) -> None:
        require_scope(principal, "settings.write")
        async with self.uow_factory() as uow, uow.integration_secrets.locked(principal):
            row = await uow.integration_secrets.get(principal, key_id)
            if row is None:
                raise NotFoundError("Integration key not found.")
            if row.deleted_at is not None:
                return
            if row.revision != expected_revision:
                raise ConflictError("Integration key changed; reload Settings.")
            now = self.clock.now()
            row = replace(
                row,
                sealed_value=None,
                deleted_at=now,
                updated_at=now,
                revision=row.revision + 1,
            )
            await uow.integration_secrets.put(principal, row)
            await self._audit(uow, principal, row, "deleted")

    async def binding(self, principal: Principal, reference: str, host: str) -> IntegrationSecret:
        """Internal capability: ownership and destination are checked even on a guessed ID."""
        key_id = secret_id(reference)
        async with self.uow_factory() as uow:
            row = await uow.integration_secrets.get(principal, key_id)
        if row is None or row.deleted_at is not None or row.origin != f"https://{host}":
            raise NotFoundError("Integration key unavailable; check Integration keys in Settings.")
        return row

    async def resolve(
        self, principal: Principal, reference: str, host: str
    ) -> tuple[str, SecretValue]:
        row = await self.binding(principal, reference, host)
        if row.sealed_value is None:
            raise NotFoundError("Integration key unavailable.")
        return row.auth_scheme, self.sealer.open(row.sealed_value, row.context())

    async def _audit(
        self, uow: RepositoryUnitOfWork, principal: Principal, row: IntegrationSecret, action: str
    ) -> None:
        key_id = self.ids.new_id()
        await uow.process_events.append(
            ProcessEvent(
                id=key_id,
                event_type=f"settings.integration_secrets.{action}",
                actor_type="principal",
                actor_id=principal.principal_id,
                payload={
                    "tenant_id": principal.tenant_id,
                    "principal_id": principal.principal_id,
                    "secret_id": str(row.id),
                    "revision": row.revision,
                },
                derivation_key=f"integration-secret:{key_id}",
                created_at=self.clock.now(),
            )
        )
