"""Persistence ports for device-scoped invocations and device message ingest."""

from __future__ import annotations

import builtins
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.devices import (
    DeviceIngestReceipt,
    DeviceInvocation,
    DeviceInvocationStatus,
    DeviceTriageMapping,
)


class DeviceInvocationStore(Protocol):
    """Durable rows for device-scoped tool calls awaiting one device result."""

    async def create(self, invocation: DeviceInvocation) -> DeviceInvocation | None:
        """Insert one pending invocation, returning ``None`` when the id already exists."""
        ...

    async def get(self, invocation_id: UUID) -> DeviceInvocation:
        """Read one invocation, raising ``NotFoundError`` when it does not exist."""
        ...

    async def list_pending_for_device(
        self,
        device_id: UUID,
        *,
        now: datetime,
    ) -> builtins.list[DeviceInvocation]:
        """List the device's pending invocations created at or before ``now``, oldest first."""
        ...

    async def record_result(
        self,
        invocation_id: UUID,
        *,
        device_id: UUID,
        status: DeviceInvocationStatus,
        at: datetime,
    ) -> DeviceInvocation:
        """Resolve a pending invocation with the device's terminal result.

        The first result wins: a pending row moves to the posted terminal status,
        and any later result — matching or not — returns the recorded row
        unchanged. An invocation the server already expired accepts no result and
        raises ``ConflictError``. An unknown id, or one owned by another device,
        raises ``NotFoundError``.
        """
        ...

    async def expire_overdue(self, *, now: datetime, timeout_seconds: int) -> int:
        """Expire pending invocations older than the timeout, returning how many moved."""
        ...


class DeviceIngestStore(Protocol):
    """Content-free receipts and the standing triage session for device ingest."""

    async def record(self, receipt: DeviceIngestReceipt) -> DeviceIngestReceipt | None:
        """Store one receipt keyed by ``(device_id, channel, digest)``.

        A replayed digest stores nothing and returns ``None``.
        """
        ...

    async def attach_routing(
        self,
        *,
        device_id: UUID,
        channel: str,
        digest: str,
        session_id: UUID,
        run_id: UUID,
    ) -> None:
        """Record where a stored receipt was routed, raising ``NotFoundError`` when absent."""
        ...

    async def count_for_utc_day(self, device_id: UUID, channel: str, *, day: date) -> int:
        """Count receipts received on one UTC calendar day, for the per-device daily cap."""
        ...

    async def get_triage_mapping(
        self,
        device_id: UUID,
        channel: str,
    ) -> DeviceTriageMapping | None:
        """Read the standing triage session pinned to this device channel, if any."""
        ...

    async def set_triage_mapping(self, mapping: DeviceTriageMapping) -> None:
        """Create or replace the standing triage session for this device channel."""
        ...
