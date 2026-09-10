"""In-memory surface persistence adapters; PostgreSQL adapters live beside the schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import (
    SurfaceInboundReceiptRow,
    SurfacePairingCodeRow,
    SurfacePairingRow,
    SurfaceReplyRow,
    SurfaceSenderLockoutRow,
    SurfaceSessionRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.surfaces import (
    InboundDisposition,
    InboundReceipt,
    Pairing,
    PairingCode,
    SenderLockout,
    SurfaceReply,
    SurfaceReplyStatus,
    SurfaceSession,
    rotate_surface_session,
)
from agent_core.ports.surfaces import (
    SurfaceAdmissionController,
    SurfacePairingRepository,
    SurfaceReceiptRepository,
    SurfaceReplyOutbox,
    SurfaceSessionRepository,
)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("surface repository instant must be aware")
    return value.astimezone(UTC)


def _owned(tenant_id: str, principal_id: str, principal: Principal) -> bool:
    return tenant_id == principal.tenant_id and principal_id == principal.principal_id


def _rowcount(result: Any) -> int:
    return int(result.rowcount or 0)


class InMemorySurfacePairingRepository:
    def __init__(self) -> None:
        self._codes: dict[UUID, PairingCode] = {}
        self._pairings: dict[UUID, Pairing] = {}
        self._lockouts: dict[tuple[UUID, str], SenderLockout] = {}

    async def create_code(self, code: PairingCode, principal: Principal) -> PairingCode:
        if not _owned(code.tenant_id, code.principal_id, principal):
            raise NotFoundError("surface not found")
        if code.id in self._codes:
            raise ConflictError("pairing code already exists")
        self._codes[code.id] = code.model_copy(deep=True)
        return code.model_copy(deep=True)

    async def active_codes(
        self, surface_id: UUID, now: datetime, principal: Principal
    ) -> list[PairingCode]:
        instant = _aware_utc(now)
        codes = [
            code
            for code in self._codes.values()
            if code.surface_id == surface_id
            and _owned(code.tenant_id, code.principal_id, principal)
            and code.consumed_at is None
            and code.attempts < code.max_attempts
            and code.expires_at > instant
        ]
        codes.sort(key=lambda item: (item.created_at, item.id))
        return [code.model_copy(deep=True) for code in codes]

    async def active_codes_for_surface(self, surface_id: UUID, now: datetime) -> list[PairingCode]:
        instant = _aware_utc(now)
        codes = [
            code
            for code in self._codes.values()
            if code.surface_id == surface_id
            and code.consumed_at is None
            and code.attempts < code.max_attempts
            and code.expires_at > instant
        ]
        codes.sort(key=lambda item: (item.created_at, item.id))
        return [code.model_copy(deep=True) for code in codes]

    async def _owned_code(self, code_id: UUID, principal: Principal) -> PairingCode:
        code = self._codes.get(code_id)
        if code is None or not _owned(code.tenant_id, code.principal_id, principal):
            raise NotFoundError("pairing code not found")
        return code

    async def record_code_attempt(self, code_id: UUID, principal: Principal) -> PairingCode:
        code = await self._owned_code(code_id, principal)
        if code.consumed_at is not None or code.attempts >= code.max_attempts:
            raise ConflictError("pairing code is no longer active")
        updated = code.model_copy(update={"attempts": code.attempts + 1})
        updated = PairingCode.model_validate(updated.model_dump())
        self._codes[code_id] = updated
        return updated.model_copy(deep=True)

    async def consume_code(
        self, code_id: UUID, consumed_at: datetime, principal: Principal
    ) -> PairingCode:
        code = await self._owned_code(code_id, principal)
        instant = _aware_utc(consumed_at)
        if code.consumed_at is not None or code.expires_at <= instant:
            raise ConflictError("pairing code is no longer active")
        updated = code.model_copy(update={"consumed_at": instant})
        updated = PairingCode.model_validate(updated.model_dump())
        self._codes[code_id] = updated
        return updated.model_copy(deep=True)

    async def create_pairing(self, pairing: Pairing) -> Pairing:
        if pairing.id in self._pairings:
            raise ConflictError("pairing already exists")
        if await self.live_pairing(pairing.surface_id, pairing.sender_id) is not None:
            raise ConflictError("sender is already paired")
        self._pairings[pairing.id] = pairing.model_copy(deep=True)
        return pairing.model_copy(deep=True)

    async def live_pairing(self, surface_id: UUID, sender_id: str) -> Pairing | None:
        pairing = next(
            (
                value
                for value in self._pairings.values()
                if value.surface_id == surface_id
                and value.sender_id == sender_id
                and value.revoked_at is None
            ),
            None,
        )
        return None if pairing is None else pairing.model_copy(deep=True)

    async def get_pairing(self, pairing_id: UUID, principal: Principal) -> Pairing:
        pairing = self._pairings.get(pairing_id)
        if pairing is None or not _owned(pairing.tenant_id, pairing.principal_id, principal):
            raise NotFoundError("pairing not found")
        return pairing.model_copy(deep=True)

    async def list_pairings(self, surface_id: UUID, principal: Principal) -> list[Pairing]:
        pairings = [
            pairing
            for pairing in self._pairings.values()
            if pairing.surface_id == surface_id
            and _owned(pairing.tenant_id, pairing.principal_id, principal)
        ]
        pairings.sort(key=lambda item: (item.paired_at, item.id), reverse=True)
        return [pairing.model_copy(deep=True) for pairing in pairings]

    async def touch_pairing(self, pairing_id: UUID, at: datetime) -> Pairing:
        pairing = self._pairings.get(pairing_id)
        if pairing is None:
            raise NotFoundError("pairing not found")
        updated = pairing.model_copy(update={"last_message_at": _aware_utc(at)})
        updated = Pairing.model_validate(updated.model_dump())
        self._pairings[pairing_id] = updated
        return updated.model_copy(deep=True)

    async def revoke_pairing(self, pairing_id: UUID, principal: Principal, at: datetime) -> Pairing:
        pairing = await self.get_pairing(pairing_id, principal)
        if pairing.revoked_at is not None:
            return pairing
        updated = pairing.model_copy(
            update={"revoked_at": _aware_utc(at), "revoked_by": principal.principal_id}
        )
        updated = Pairing.model_validate(updated.model_dump())
        self._pairings[pairing_id] = updated
        return updated.model_copy(deep=True)

    async def delete_pairing(self, pairing_id: UUID, principal: Principal) -> None:
        pairing = await self.get_pairing(pairing_id, principal)
        if pairing.revoked_at is None:
            raise ConflictError("pairing must be revoked before deletion")
        del self._pairings[pairing_id]

    async def get_lockout(self, surface_id: UUID, sender_id: str) -> SenderLockout | None:
        lockout = self._lockouts.get((surface_id, sender_id))
        return None if lockout is None else lockout.model_copy(deep=True)

    async def record_failed_pairing(
        self,
        surface_id: UUID,
        sender_id: str,
        *,
        at: datetime,
        max_attempts: int,
        lockout_seconds: int,
    ) -> SenderLockout:
        instant = _aware_utc(at)
        key = (surface_id, sender_id)
        current = self._lockouts.get(key)
        attempts = (
            1
            if current is None or current.locked_until is not None
            else current.failed_attempts + 1
        )
        lockout = SenderLockout(
            surface_id=surface_id,
            sender_id=sender_id,
            failed_attempts=attempts,
            window_started_at=instant if current is None else current.window_started_at,
            locked_until=(
                instant + timedelta(seconds=lockout_seconds) if attempts >= max_attempts else None
            ),
        )
        self._lockouts[key] = lockout
        return lockout.model_copy(deep=True)

    async def clear_lockout(self, surface_id: UUID, sender_id: str) -> None:
        self._lockouts.pop((surface_id, sender_id), None)


class InMemorySurfaceSessionRepository:
    def __init__(self) -> None:
        self._mappings: dict[UUID, SurfaceSession] = {}

    async def create(self, mapping: SurfaceSession) -> SurfaceSession:
        if mapping.id in self._mappings:
            raise ConflictError("surface session mapping already exists")
        if await self.live(mapping.surface_id, mapping.external_key) is not None:
            raise ConflictError("surface session key already has a live mapping")
        self._mappings[mapping.id] = mapping.model_copy(deep=True)
        return mapping.model_copy(deep=True)

    async def live(self, surface_id: UUID, external_key: str) -> SurfaceSession | None:
        mapping = next(
            (
                value
                for value in self._mappings.values()
                if value.surface_id == surface_id
                and value.external_key == external_key
                and value.rotated_at is None
            ),
            None,
        )
        return None if mapping is None else mapping.model_copy(deep=True)

    async def for_session(self, session_id: UUID) -> SurfaceSession | None:
        mapping = next(
            (value for value in self._mappings.values() if value.session_id == session_id),
            None,
        )
        return None if mapping is None else mapping.model_copy(deep=True)

    async def touch_inbound(self, mapping_id: UUID, at: datetime) -> SurfaceSession:
        mapping = self._mappings.get(mapping_id)
        if mapping is None:
            raise NotFoundError("surface session mapping not found")
        updated = SurfaceSession.model_validate(
            mapping.model_copy(update={"last_inbound_at": _aware_utc(at)}).model_dump()
        )
        self._mappings[mapping_id] = updated
        return updated.model_copy(deep=True)

    async def rotate(self, mapping_id: UUID, at: datetime) -> SurfaceSession:
        mapping = self._mappings.get(mapping_id)
        if mapping is None:
            raise NotFoundError("surface session mapping not found")
        rotated = rotate_surface_session(mapping, at)
        self._mappings[mapping_id] = rotated
        return rotated.model_copy(deep=True)

    async def rotate_for_sender(self, surface_id: UUID, external_key: str, at: datetime) -> int:
        mapping = await self.live(surface_id, external_key)
        if mapping is None:
            return 0
        await self.rotate(mapping.id, at)
        return 1


class InMemorySurfaceReceiptRepository:
    def __init__(self) -> None:
        self._receipts: dict[tuple[UUID, str], InboundReceipt] = {}

    async def create(self, receipt: InboundReceipt) -> bool:
        key = (receipt.surface_id, receipt.external_update_id)
        if key in self._receipts:
            return False
        self._receipts[key] = receipt.model_copy(deep=True)
        return True

    async def get(self, surface_id: UUID, external_update_id: str) -> InboundReceipt | None:
        receipt = self._receipts.get((surface_id, external_update_id))
        return None if receipt is None else receipt.model_copy(deep=True)

    async def replace(self, receipt: InboundReceipt) -> InboundReceipt:
        key = (receipt.surface_id, receipt.external_update_id)
        if key not in self._receipts:
            raise NotFoundError("surface receipt not found")
        self._receipts[key] = receipt.model_copy(deep=True)
        return receipt.model_copy(deep=True)

    async def latest_numeric_update_id(self, surface_id: UUID) -> int | None:
        identifiers = [
            int(external_update_id)
            for receipt_surface_id, external_update_id in self._receipts
            if receipt_surface_id == surface_id and external_update_id.isdecimal()
        ]
        return max(identifiers, default=None)


class InMemorySurfaceReplyOutbox:
    def __init__(self) -> None:
        self._replies: dict[UUID, SurfaceReply] = {}

    async def enqueue(self, reply: SurfaceReply) -> bool:
        if reply.id in self._replies or any(
            current.run_id == reply.run_id for current in self._replies.values()
        ):
            return False
        self._replies[reply.id] = reply.model_copy(deep=True)
        return True

    async def claim_due(
        self,
        now: datetime,
        limit: int,
        worker_id: str,
        lease_seconds: int,
    ) -> list[SurfaceReply]:
        if limit <= 0 or lease_seconds <= 0:
            raise ValueError("surface reply claim bounds must be positive")
        instant = _aware_utc(now)
        due = [
            reply
            for reply in self._replies.values()
            if reply.status is SurfaceReplyStatus.PENDING
            and reply.next_attempt_at <= instant
            and (reply.claimed_until is None or reply.claimed_until <= instant)
        ]
        due.sort(key=lambda item: (item.next_attempt_at, item.created_at, item.id))
        claimed: list[SurfaceReply] = []
        for reply in due[:limit]:
            updated = reply.model_copy(
                update={
                    "claimed_by": worker_id,
                    "claimed_until": instant + timedelta(seconds=lease_seconds),
                    "attempts": reply.attempts + 1,
                }
            )
            updated = SurfaceReply.model_validate(updated.model_dump())
            self._replies[reply.id] = updated
            claimed.append(updated.model_copy(deep=True))
        return claimed

    def _claimed(self, reply_id: UUID, worker_id: str) -> SurfaceReply:
        reply = self._replies.get(reply_id)
        if reply is None:
            raise NotFoundError("surface reply not found")
        if reply.claimed_by != worker_id:
            raise ConflictError("surface reply lease is not held")
        return reply

    async def record_chunk(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        chunks_total: int,
        at: datetime,
    ) -> SurfaceReply:
        del at
        reply = self._claimed(reply_id, worker_id)
        if chunks_total <= 0:
            raise ValueError("surface reply must contain a chunk")
        if reply.chunks_total is not None and reply.chunks_total != chunks_total:
            raise ConflictError("surface reply chunk total changed")
        updated = reply.model_copy(
            update={"chunks_total": chunks_total, "chunks_sent": reply.chunks_sent + 1}
        )
        updated = SurfaceReply.model_validate(updated.model_dump())
        self._replies[reply_id] = updated
        return updated.model_copy(deep=True)

    async def settle(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        status: SurfaceReplyStatus,
        at: datetime,
    ) -> SurfaceReply:
        if status is SurfaceReplyStatus.PENDING:
            raise ValueError("settlement status must be terminal")
        reply = self._claimed(reply_id, worker_id)
        updated = reply.model_copy(
            update={
                "status": status,
                "settled_at": _aware_utc(at),
                "claimed_by": None,
                "claimed_until": None,
            }
        )
        updated = SurfaceReply.model_validate(updated.model_dump())
        self._replies[reply_id] = updated
        return updated.model_copy(deep=True)

    async def retry(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        next_attempt_at: datetime,
    ) -> SurfaceReply:
        reply = self._claimed(reply_id, worker_id)
        updated = reply.model_copy(
            update={
                "next_attempt_at": _aware_utc(next_attempt_at),
                "claimed_by": None,
                "claimed_until": None,
            }
        )
        updated = SurfaceReply.model_validate(updated.model_dump())
        self._replies[reply_id] = updated
        return updated.model_copy(deep=True)


@dataclass(frozen=True, slots=True)
class InMemorySurfaceRepositories:
    admission: SurfaceAdmissionController
    pairings: SurfacePairingRepository
    sessions: SurfaceSessionRepository
    receipts: SurfaceReceiptRepository
    replies: SurfaceReplyOutbox


def _code_to_domain(row: SurfacePairingCodeRow) -> PairingCode:
    return PairingCode(
        id=row.id,
        surface_id=row.surface_id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        code_hash=row.code_hash,
        code_salt=row.code_salt,
        granted_scopes=frozenset(row.granted_scopes),
        label=row.label,
        expires_at=row.expires_at,
        max_attempts=row.max_attempts,
        attempts=row.attempts,
        consumed_at=row.consumed_at,
        created_by_principal_id=row.created_by_principal_id,
        created_at=row.created_at,
    )


def _pairing_to_domain(row: SurfacePairingRow) -> Pairing:
    return Pairing(
        id=row.id,
        surface_id=row.surface_id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        sender_id=row.sender_id,
        sender_label=row.sender_label,
        granted_scopes=frozenset(row.granted_scopes),
        paired_at=row.paired_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        last_message_at=row.last_message_at,
    )


def _lockout_to_domain(row: SurfaceSenderLockoutRow) -> SenderLockout:
    return SenderLockout(
        surface_id=row.surface_id,
        sender_id=row.sender_id,
        failed_attempts=row.failed_attempts,
        window_started_at=row.window_started_at,
        locked_until=row.locked_until,
    )


def _surface_session_to_domain(row: SurfaceSessionRow) -> SurfaceSession:
    return SurfaceSession(
        id=row.id,
        surface_id=row.surface_id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        external_key=row.external_key,
        session_id=row.session_id,
        created_at=row.created_at,
        last_inbound_at=row.last_inbound_at,
        rotated_at=row.rotated_at,
    )


def _receipt_to_domain(row: SurfaceInboundReceiptRow) -> InboundReceipt:
    return InboundReceipt(
        surface_id=row.surface_id,
        external_update_id=row.external_update_id,
        received_at=row.received_at,
        disposition=InboundDisposition(row.disposition),
        session_id=row.session_id,
        run_id=row.run_id,
        reason_code=row.reason_code,
    )


def _reply_to_domain(row: SurfaceReplyRow) -> SurfaceReply:
    return SurfaceReply(
        id=row.id,
        surface_id=row.surface_id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        run_id=row.run_id,
        chat_ref=row.chat_ref,
        status=SurfaceReplyStatus(row.status),
        chunks_total=row.chunks_total,
        chunks_sent=row.chunks_sent,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        claimed_by=row.claimed_by,
        claimed_until=row.claimed_until,
        created_at=row.created_at,
        settled_at=row.settled_at,
    )


class PostgresSurfacePairingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_code(self, code: PairingCode, principal: Principal) -> PairingCode:
        if not _owned(code.tenant_id, code.principal_id, principal):
            raise NotFoundError("surface not found")
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(SurfacePairingCodeRow).values(
                        id=code.id,
                        surface_id=code.surface_id,
                        tenant_id=code.tenant_id,
                        principal_id=code.principal_id,
                        code_hash=code.code_hash,
                        code_salt=code.code_salt,
                        granted_scopes=sorted(code.granted_scopes),
                        label=code.label,
                        expires_at=code.expires_at,
                        max_attempts=code.max_attempts,
                        attempts=code.attempts,
                        consumed_at=code.consumed_at,
                        created_by_principal_id=code.created_by_principal_id,
                        created_at=code.created_at,
                    )
                )
        except IntegrityError as exc:
            raise ConflictError("pairing code already exists") from exc
        return code

    async def active_codes(
        self, surface_id: UUID, now: datetime, principal: Principal
    ) -> list[PairingCode]:
        rows = (
            await self._session.scalars(
                select(SurfacePairingCodeRow)
                .where(
                    SurfacePairingCodeRow.surface_id == surface_id,
                    SurfacePairingCodeRow.tenant_id == principal.tenant_id,
                    SurfacePairingCodeRow.principal_id == principal.principal_id,
                    SurfacePairingCodeRow.consumed_at.is_(None),
                    SurfacePairingCodeRow.attempts < SurfacePairingCodeRow.max_attempts,
                    SurfacePairingCodeRow.expires_at > _aware_utc(now),
                )
                .order_by(SurfacePairingCodeRow.created_at, SurfacePairingCodeRow.id)
            )
        ).all()
        return [_code_to_domain(row) for row in rows]

    async def active_codes_for_surface(self, surface_id: UUID, now: datetime) -> list[PairingCode]:
        rows = (
            await self._session.scalars(
                select(SurfacePairingCodeRow)
                .where(
                    SurfacePairingCodeRow.surface_id == surface_id,
                    SurfacePairingCodeRow.consumed_at.is_(None),
                    SurfacePairingCodeRow.attempts < SurfacePairingCodeRow.max_attempts,
                    SurfacePairingCodeRow.expires_at > _aware_utc(now),
                )
                .order_by(SurfacePairingCodeRow.created_at, SurfacePairingCodeRow.id)
            )
        ).all()
        return [_code_to_domain(row) for row in rows]

    async def _owned_code(self, code_id: UUID, principal: Principal) -> SurfacePairingCodeRow:
        row = (
            await self._session.scalars(
                select(SurfacePairingCodeRow)
                .where(
                    SurfacePairingCodeRow.id == code_id,
                    SurfacePairingCodeRow.tenant_id == principal.tenant_id,
                    SurfacePairingCodeRow.principal_id == principal.principal_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("pairing code not found")
        return row

    async def record_code_attempt(self, code_id: UUID, principal: Principal) -> PairingCode:
        row = await self._owned_code(code_id, principal)
        if row.consumed_at is not None or row.attempts >= row.max_attempts:
            raise ConflictError("pairing code is no longer active")
        row.attempts += 1
        await self._session.flush()
        return _code_to_domain(row)

    async def consume_code(
        self, code_id: UUID, consumed_at: datetime, principal: Principal
    ) -> PairingCode:
        row = await self._owned_code(code_id, principal)
        instant = _aware_utc(consumed_at)
        if row.consumed_at is not None or row.expires_at <= instant:
            raise ConflictError("pairing code is no longer active")
        row.consumed_at = instant
        await self._session.flush()
        return _code_to_domain(row)

    async def create_pairing(self, pairing: Pairing) -> Pairing:
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(SurfacePairingRow).values(
                        id=pairing.id,
                        surface_id=pairing.surface_id,
                        tenant_id=pairing.tenant_id,
                        principal_id=pairing.principal_id,
                        sender_id=pairing.sender_id,
                        sender_label=pairing.sender_label,
                        granted_scopes=sorted(pairing.granted_scopes),
                        paired_at=pairing.paired_at,
                        revoked_at=pairing.revoked_at,
                        revoked_by=pairing.revoked_by,
                        last_message_at=pairing.last_message_at,
                    )
                )
        except IntegrityError as exc:
            raise ConflictError("sender is already paired") from exc
        return pairing

    async def live_pairing(self, surface_id: UUID, sender_id: str) -> Pairing | None:
        row = (
            await self._session.scalars(
                select(SurfacePairingRow).where(
                    SurfacePairingRow.surface_id == surface_id,
                    SurfacePairingRow.sender_id == sender_id,
                    SurfacePairingRow.revoked_at.is_(None),
                )
            )
        ).one_or_none()
        return None if row is None else _pairing_to_domain(row)

    async def get_pairing(self, pairing_id: UUID, principal: Principal) -> Pairing:
        row = (
            await self._session.scalars(
                select(SurfacePairingRow).where(
                    SurfacePairingRow.id == pairing_id,
                    SurfacePairingRow.tenant_id == principal.tenant_id,
                    SurfacePairingRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("pairing not found")
        return _pairing_to_domain(row)

    async def list_pairings(self, surface_id: UUID, principal: Principal) -> list[Pairing]:
        rows = (
            await self._session.scalars(
                select(SurfacePairingRow)
                .where(
                    SurfacePairingRow.surface_id == surface_id,
                    SurfacePairingRow.tenant_id == principal.tenant_id,
                    SurfacePairingRow.principal_id == principal.principal_id,
                )
                .order_by(SurfacePairingRow.paired_at.desc(), SurfacePairingRow.id.desc())
            )
        ).all()
        return [_pairing_to_domain(row) for row in rows]

    async def touch_pairing(self, pairing_id: UUID, at: datetime) -> Pairing:
        row = await self._session.get(SurfacePairingRow, pairing_id)
        if row is None:
            raise NotFoundError("pairing not found")
        row.last_message_at = _aware_utc(at)
        await self._session.flush()
        return _pairing_to_domain(row)

    async def revoke_pairing(self, pairing_id: UUID, principal: Principal, at: datetime) -> Pairing:
        row = (
            await self._session.scalars(
                select(SurfacePairingRow)
                .where(
                    SurfacePairingRow.id == pairing_id,
                    SurfacePairingRow.tenant_id == principal.tenant_id,
                    SurfacePairingRow.principal_id == principal.principal_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("pairing not found")
        if row.revoked_at is None:
            row.revoked_at = _aware_utc(at)
            row.revoked_by = principal.principal_id
            await self._session.flush()
        return _pairing_to_domain(row)

    async def delete_pairing(self, pairing_id: UUID, principal: Principal) -> None:
        pairing = await self.get_pairing(pairing_id, principal)
        if pairing.revoked_at is None:
            raise ConflictError("pairing must be revoked before deletion")
        await self._session.execute(
            delete(SurfacePairingRow).where(
                SurfacePairingRow.id == pairing_id,
                SurfacePairingRow.tenant_id == principal.tenant_id,
                SurfacePairingRow.principal_id == principal.principal_id,
            )
        )

    async def get_lockout(self, surface_id: UUID, sender_id: str) -> SenderLockout | None:
        row = await self._session.get(SurfaceSenderLockoutRow, (surface_id, sender_id))
        return None if row is None else _lockout_to_domain(row)

    async def record_failed_pairing(
        self,
        surface_id: UUID,
        sender_id: str,
        *,
        at: datetime,
        max_attempts: int,
        lockout_seconds: int,
    ) -> SenderLockout:
        if max_attempts <= 0 or lockout_seconds <= 0:
            raise ValueError("surface lockout bounds must be positive")
        instant = _aware_utc(at)
        lock_key = f"{surface_id}:{sender_id}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
        row = await self._session.get(SurfaceSenderLockoutRow, (surface_id, sender_id))
        attempts = 1 if row is None or row.locked_until is not None else row.failed_attempts + 1
        locked_until = (
            instant + timedelta(seconds=lockout_seconds) if attempts >= max_attempts else None
        )
        if row is None:
            await self._session.execute(
                pg_insert(SurfaceSenderLockoutRow).values(
                    surface_id=surface_id,
                    sender_id=sender_id,
                    failed_attempts=attempts,
                    window_started_at=instant,
                    locked_until=locked_until,
                )
            )
            row = await self._session.get(SurfaceSenderLockoutRow, (surface_id, sender_id))
            assert row is not None
        else:
            row.failed_attempts = attempts
            if attempts == 1:
                row.window_started_at = instant
            row.locked_until = locked_until
        await self._session.flush()
        return _lockout_to_domain(row)

    async def clear_lockout(self, surface_id: UUID, sender_id: str) -> None:
        await self._session.execute(
            delete(SurfaceSenderLockoutRow).where(
                SurfaceSenderLockoutRow.surface_id == surface_id,
                SurfaceSenderLockoutRow.sender_id == sender_id,
            )
        )


class PostgresSurfaceSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, mapping: SurfaceSession) -> SurfaceSession:
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(SurfaceSessionRow).values(
                        id=mapping.id,
                        surface_id=mapping.surface_id,
                        tenant_id=mapping.tenant_id,
                        principal_id=mapping.principal_id,
                        external_key=mapping.external_key,
                        session_id=mapping.session_id,
                        created_at=mapping.created_at,
                        last_inbound_at=mapping.last_inbound_at,
                        rotated_at=mapping.rotated_at,
                    )
                )
        except IntegrityError as exc:
            raise ConflictError("surface session key already has a live mapping") from exc
        return mapping

    async def live(self, surface_id: UUID, external_key: str) -> SurfaceSession | None:
        row = (
            await self._session.scalars(
                select(SurfaceSessionRow).where(
                    SurfaceSessionRow.surface_id == surface_id,
                    SurfaceSessionRow.external_key == external_key,
                    SurfaceSessionRow.rotated_at.is_(None),
                )
            )
        ).one_or_none()
        return None if row is None else _surface_session_to_domain(row)

    async def for_session(self, session_id: UUID) -> SurfaceSession | None:
        row = (
            await self._session.scalars(
                select(SurfaceSessionRow)
                .where(SurfaceSessionRow.session_id == session_id)
                .order_by(SurfaceSessionRow.created_at.desc())
                .limit(1)
            )
        ).one_or_none()
        return None if row is None else _surface_session_to_domain(row)

    async def _get(self, mapping_id: UUID) -> SurfaceSessionRow:
        row = await self._session.get(SurfaceSessionRow, mapping_id)
        if row is None:
            raise NotFoundError("surface session mapping not found")
        return row

    async def touch_inbound(self, mapping_id: UUID, at: datetime) -> SurfaceSession:
        row = await self._get(mapping_id)
        row.last_inbound_at = _aware_utc(at)
        await self._session.flush()
        return _surface_session_to_domain(row)

    async def rotate(self, mapping_id: UUID, at: datetime) -> SurfaceSession:
        row = await self._get(mapping_id)
        if row.rotated_at is not None:
            raise ValueError("surface session mapping is already rotated")
        row.rotated_at = _aware_utc(at)
        await self._session.flush()
        return _surface_session_to_domain(row)

    async def rotate_for_sender(self, surface_id: UUID, external_key: str, at: datetime) -> int:
        mapping = await self.live(surface_id, external_key)
        if mapping is None:
            return 0
        await self.rotate(mapping.id, at)
        return 1


class PostgresSurfaceReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, receipt: InboundReceipt) -> bool:
        statement = (
            pg_insert(SurfaceInboundReceiptRow)
            .values(
                surface_id=receipt.surface_id,
                external_update_id=receipt.external_update_id,
                received_at=receipt.received_at,
                disposition=receipt.disposition.value,
                session_id=receipt.session_id,
                run_id=receipt.run_id,
                reason_code=receipt.reason_code,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    SurfaceInboundReceiptRow.surface_id,
                    SurfaceInboundReceiptRow.external_update_id,
                ]
            )
        )
        return bool(_rowcount(await self._session.execute(statement)))

    async def get(self, surface_id: UUID, external_update_id: str) -> InboundReceipt | None:
        row = await self._session.get(
            SurfaceInboundReceiptRow,
            (surface_id, external_update_id),
        )
        return None if row is None else _receipt_to_domain(row)

    async def replace(self, receipt: InboundReceipt) -> InboundReceipt:
        row = await self._session.get(
            SurfaceInboundReceiptRow,
            (receipt.surface_id, receipt.external_update_id),
        )
        if row is None:
            raise NotFoundError("surface receipt not found")
        row.received_at = receipt.received_at
        row.disposition = receipt.disposition.value
        row.session_id = receipt.session_id
        row.run_id = receipt.run_id
        row.reason_code = receipt.reason_code
        await self._session.flush()
        return _receipt_to_domain(row)

    async def latest_numeric_update_id(self, surface_id: UUID) -> int | None:
        value = await self._session.scalar(
            select(func.max(cast(SurfaceInboundReceiptRow.external_update_id, BigInteger))).where(
                SurfaceInboundReceiptRow.surface_id == surface_id,
                SurfaceInboundReceiptRow.external_update_id.op("~")(r"^[0-9]+$"),
            )
        )
        return None if value is None else int(value)


class PostgresSurfaceReplyOutbox:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, reply: SurfaceReply) -> bool:
        statement = (
            pg_insert(SurfaceReplyRow)
            .values(
                id=reply.id,
                surface_id=reply.surface_id,
                tenant_id=reply.tenant_id,
                principal_id=reply.principal_id,
                run_id=reply.run_id,
                chat_ref=reply.chat_ref,
                status=reply.status.value,
                chunks_total=reply.chunks_total,
                chunks_sent=reply.chunks_sent,
                attempts=reply.attempts,
                next_attempt_at=reply.next_attempt_at,
                claimed_by=reply.claimed_by,
                claimed_until=reply.claimed_until,
                created_at=reply.created_at,
                settled_at=reply.settled_at,
            )
            .on_conflict_do_nothing()
        )
        return bool(_rowcount(await self._session.execute(statement)))

    async def claim_due(
        self,
        now: datetime,
        limit: int,
        worker_id: str,
        lease_seconds: int,
    ) -> list[SurfaceReply]:
        if limit <= 0 or lease_seconds <= 0:
            raise ValueError("surface reply claim bounds must be positive")
        instant = _aware_utc(now)
        rows = (
            await self._session.scalars(
                select(SurfaceReplyRow)
                .where(
                    SurfaceReplyRow.status == SurfaceReplyStatus.PENDING.value,
                    SurfaceReplyRow.next_attempt_at <= instant,
                    or_(
                        SurfaceReplyRow.claimed_until.is_(None),
                        SurfaceReplyRow.claimed_until <= instant,
                    ),
                )
                .order_by(
                    SurfaceReplyRow.next_attempt_at,
                    SurfaceReplyRow.created_at,
                    SurfaceReplyRow.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            row.claimed_by = worker_id
            row.claimed_until = instant + timedelta(seconds=lease_seconds)
            row.attempts += 1
        await self._session.flush()
        return [_reply_to_domain(row) for row in rows]

    async def _claimed(self, reply_id: UUID, worker_id: str) -> SurfaceReplyRow:
        row = (
            await self._session.scalars(
                select(SurfaceReplyRow).where(
                    SurfaceReplyRow.id == reply_id,
                    SurfaceReplyRow.claimed_by == worker_id,
                )
            )
        ).one_or_none()
        if row is None:
            exists = await self._session.get(SurfaceReplyRow, reply_id)
            if exists is None:
                raise NotFoundError("surface reply not found")
            raise ConflictError("surface reply lease is not held")
        return row

    async def record_chunk(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        chunks_total: int,
        at: datetime,
    ) -> SurfaceReply:
        _aware_utc(at)
        if chunks_total <= 0:
            raise ValueError("surface reply must contain a chunk")
        row = await self._claimed(reply_id, worker_id)
        if row.chunks_total is not None and row.chunks_total != chunks_total:
            raise ConflictError("surface reply chunk total changed")
        row.chunks_total = chunks_total
        row.chunks_sent += 1
        await self._session.flush()
        return _reply_to_domain(row)

    async def settle(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        status: SurfaceReplyStatus,
        at: datetime,
    ) -> SurfaceReply:
        if status is SurfaceReplyStatus.PENDING:
            raise ValueError("settlement status must be terminal")
        row = await self._claimed(reply_id, worker_id)
        row.status = status.value
        row.settled_at = _aware_utc(at)
        row.claimed_by = None
        row.claimed_until = None
        await self._session.flush()
        return _reply_to_domain(row)

    async def retry(
        self,
        reply_id: UUID,
        *,
        worker_id: str,
        next_attempt_at: datetime,
    ) -> SurfaceReply:
        row = await self._claimed(reply_id, worker_id)
        row.next_attempt_at = _aware_utc(next_attempt_at)
        row.claimed_by = None
        row.claimed_until = None
        await self._session.flush()
        return _reply_to_domain(row)


@dataclass(frozen=True, slots=True)
class PostgresSurfaceRepositories:
    admission: SurfaceAdmissionController
    pairings: SurfacePairingRepository
    sessions: SurfaceSessionRepository
    receipts: SurfaceReceiptRepository
    replies: SurfaceReplyOutbox
