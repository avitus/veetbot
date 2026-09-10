"""Ports for paired inbound messaging surfaces."""

from __future__ import annotations

import builtins
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.surfaces import (
    InboundReceipt,
    Pairing,
    PairingCode,
    SenderLockout,
    SurfaceAdmissionDecision,
    SurfaceReply,
    SurfaceReplyStatus,
    SurfaceSession,
)


class SurfaceAdmissionController(Protocol):
    async def check(
        self,
        tenant_id: str,
        reservation: Decimal | None,
        now: datetime,
    ) -> SurfaceAdmissionDecision: ...


class SurfacePairingRepository(Protocol):
    async def create_code(self, code: PairingCode, principal: Principal) -> PairingCode: ...

    async def active_codes(
        self, surface_id: UUID, now: datetime, principal: Principal
    ) -> builtins.list[PairingCode]: ...

    async def active_codes_for_surface(
        self, surface_id: UUID, now: datetime
    ) -> builtins.list[PairingCode]: ...

    async def record_code_attempt(self, code_id: UUID, principal: Principal) -> PairingCode: ...

    async def consume_code(
        self, code_id: UUID, consumed_at: datetime, principal: Principal
    ) -> PairingCode: ...

    async def create_pairing(self, pairing: Pairing) -> Pairing: ...

    async def live_pairing(self, surface_id: UUID, sender_id: str) -> Pairing | None: ...

    async def get_pairing(self, pairing_id: UUID, principal: Principal) -> Pairing: ...

    async def list_pairings(
        self, surface_id: UUID, principal: Principal
    ) -> builtins.list[Pairing]: ...

    async def touch_pairing(self, pairing_id: UUID, at: datetime) -> Pairing: ...

    async def revoke_pairing(
        self, pairing_id: UUID, principal: Principal, at: datetime
    ) -> Pairing: ...

    async def delete_pairing(self, pairing_id: UUID, principal: Principal) -> None: ...

    async def get_lockout(self, surface_id: UUID, sender_id: str) -> SenderLockout | None: ...

    async def record_failed_pairing(
        self,
        surface_id: UUID,
        sender_id: str,
        *,
        at: datetime,
        max_attempts: int,
        lockout_seconds: int,
    ) -> SenderLockout: ...

    async def clear_lockout(self, surface_id: UUID, sender_id: str) -> None: ...


class SurfaceSessionRepository(Protocol):
    async def create(self, mapping: SurfaceSession) -> SurfaceSession: ...

    async def live(self, surface_id: UUID, external_key: str) -> SurfaceSession | None: ...

    async def for_session(self, session_id: UUID) -> SurfaceSession | None: ...

    async def touch_inbound(self, mapping_id: UUID, at: datetime) -> SurfaceSession: ...

    async def rotate(self, mapping_id: UUID, at: datetime) -> SurfaceSession: ...

    async def rotate_for_sender(self, surface_id: UUID, external_key: str, at: datetime) -> int: ...


class SurfaceReceiptRepository(Protocol):
    async def create(self, receipt: InboundReceipt) -> bool: ...

    async def get(self, surface_id: UUID, external_update_id: str) -> InboundReceipt | None: ...

    async def replace(self, receipt: InboundReceipt) -> InboundReceipt: ...

    async def latest_numeric_update_id(self, surface_id: UUID) -> int | None: ...


class SurfaceReplyOutbox(Protocol):
    async def enqueue(self, reply: SurfaceReply) -> bool: ...

    async def claim_due(
        self,
        now: datetime,
        limit: int,
        worker_id: str,
        lease_seconds: int,
    ) -> builtins.list[SurfaceReply]: ...

    async def record_chunk(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        chunks_total: int,
        at: datetime,
    ) -> SurfaceReply: ...

    async def settle(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        status: SurfaceReplyStatus,
        at: datetime,
    ) -> SurfaceReply: ...

    async def retry(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        next_attempt_at: datetime,
    ) -> SurfaceReply: ...


class SurfaceRepositories(Protocol):
    @property
    def admission(self) -> SurfaceAdmissionController: ...

    @property
    def pairings(self) -> SurfacePairingRepository: ...

    @property
    def sessions(self) -> SurfaceSessionRepository: ...

    @property
    def receipts(self) -> SurfaceReceiptRepository: ...

    @property
    def replies(self) -> SurfaceReplyOutbox: ...
