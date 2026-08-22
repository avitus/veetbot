"""Principal-scoped device identity persistence port."""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.devices import Device, DeviceCursor, PushTarget
from agent_core.domain.notifications import NotificationKind


class DeviceRegistry(Protocol):
    async def upsert(self, device: Device, principal: Principal) -> Device: ...

    async def get(self, device_id: UUID, principal: Principal) -> Device: ...

    async def get_by_client_device_id(
        self, client_device_id: str, principal: Principal
    ) -> Device | None: ...

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: DeviceCursor | None = None,
    ) -> builtins.list[Device]: ...

    async def revoke(self, device_id: UUID, principal: Principal, at: datetime) -> Device: ...

    async def delete(self, device_id: UUID, principal: Principal) -> None: ...

    async def invalidate_push_token(
        self, device_id: UUID, reason: str, at: datetime
    ) -> Device | None: ...

    async def push_targets(
        self,
        tenant_id: str,
        principal_id: str,
        kind: NotificationKind,
    ) -> builtins.list[PushTarget]: ...
