"""Owner-bound call dispatch, authenticated correspondence intake, and reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.calls import (
    MAX_CALLBACK_BYTES,
    CallConfiguration,
    CallRecord,
    ProviderCall,
    call_identifier,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.mcp import MCPCallResult
from agent_core.domain.notifications import NewNotification, NotificationKind, NotificationPayload
from agent_core.domain.tools import ToolExecutionContext
from agent_core.ports.calls import CallStore
from agent_core.ports.determinism import Clock
from agent_core.ports.dispatch import CancellationToken
from agent_core.ports.persistence import UnitOfWorkFactory

type CallOperation = Callable[[dict[str, Any]], Awaitable[MCPCallResult]]
type CallProvider = Callable[[str, str, dict[str, Any]], Awaitable[MCPCallResult]]

RETENTION = timedelta(days=30)


def pending_content(payload: dict[str, Any]) -> bool:
    return payload.get("status") == "completed" and any(
        not payload.get(field) and payload.get(field + "_complete") is False
        for field in ("summary", "transcript")
    )


def result(value: dict[str, Any]) -> MCPCallResult:
    return MCPCallResult(content=(json.dumps(value, separators=(",", ":")),), structured=value)


def failure(code: str, *, uncertain: bool = False) -> MCPCallResult:
    return MCPCallResult(
        content=(code,),
        is_error=True,
        structured={"effect_status": "unknown" if uncertain else "not_applied"},
    )


class CallService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        owner: Principal,
        configuration: CallConfiguration,
        provider: CallProvider | None = None,
        *,
        notifications: bool = False,
    ) -> None:
        self.uow_factory = uow_factory
        self.clock = clock
        self.owner = owner
        self.configuration = configuration
        self.provider = provider
        self.notifications = notifications

    async def invoke(
        self,
        context: ToolExecutionContext,
        server: str,
        name: str,
        arguments: dict[str, Any],
        operation: CallOperation,
    ) -> MCPCallResult:
        self._owner(context.principal)
        if context.tenant_id != self.owner.tenant_id:
            raise NotFoundError("call not found")
        if server == "bland_read":
            try:
                if name == "list_calls":
                    return result(await self.list_calls(context.principal, **arguments))
                if name == "get_call":
                    return result(await self.get_call(context.principal, **arguments))
            except (ValueError, TypeError, NotFoundError):
                return failure("bland.call_not_found")
            return failure("bland.tool_unavailable")
        if server != "bland_call" or name != "start_call":
            return failure("bland.tool_unavailable")
        cancellation = cast(CancellationToken, context.cancellation)
        cancellation.raise_if_cancelled()
        if (
            arguments.get("config_revision") != self.configuration.revision
            or arguments.get("from_number") != self.configuration.phone_number
            or arguments.get("record", False) is not False
            or type(arguments.get("max_duration_minutes", 5)) is not int
            or not 1
            <= arguments.get("max_duration_minutes", 5)
            <= self.configuration.max_duration_minutes
        ):
            return failure("bland.configuration_changed")
        key = str(context.invocation_id)
        fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
        async with self.uow_factory() as uow, uow.calls.lock(self.owner):
            previous = await uow.calls.get(self.owner, "dispatch", key)
            if previous is not None:
                if previous.payload.get("fingerprint") != fingerprint:
                    return failure("bland.approval_changed")
                provider_id = previous.payload.get("provider_call_id")
                if isinstance(provider_id, str):
                    return result(
                        {"provider_call_id": provider_id, "status": previous.payload["status"]}
                    )
                return failure("bland.outcome_unknown", uncertain=True)
            if await uow.calls.list(self.owner, "dispatch", limit=1, active=True):
                return failure("bland.busy")
            cancellation.raise_if_cancelled()
            # Commit ambiguity before network I/O. A crashed sender cannot lose its reservation.
            await self._put(
                uow.calls,
                "dispatch",
                key,
                {
                    "active": True,
                    "status": "uncertain",
                    "fingerprint": fingerprint,
                    "account_id": self.configuration.account_id,
                    "config_revision": self.configuration.revision,
                    "recipient_hash": hashlib.sha256(
                        str(arguments.get("phone_number")).encode()
                    ).hexdigest(),
                    "run_id": str(context.run_id),
                    "session_id": str(context.session_id),
                },
            )
        # Keep the executor watermark and reservation durable before network I/O.
        try:
            cancellation.raise_if_cancelled()
            await context.mark_effect_sent()
            cancellation.raise_if_cancelled()
        except BaseException:
            async with self.uow_factory() as uow, uow.calls.lock(self.owner):
                row = await uow.calls.get(self.owner, "dispatch", key)
                assert row is not None
                await self._put(
                    uow.calls,
                    "dispatch",
                    key,
                    {**row.payload, "active": False, "status": "not_dispatched"},
                )
            raise
        try:
            response = await operation({**arguments, "request_id": key})
        except asyncio.CancelledError:
            # The provider may have accepted the call. The durable worker reconciles/terminates it.
            raise
        except Exception:
            return failure("bland.outcome_unknown", uncertain=True)
        value = response.structured or {}
        if response.is_error:
            if value.get("effect_status") == "not_applied":
                async with self.uow_factory() as uow, uow.calls.lock(self.owner):
                    row = await uow.calls.get(self.owner, "dispatch", key)
                    assert row is not None
                    await self._put(
                        uow.calls,
                        "dispatch",
                        key,
                        {**row.payload, "active": False, "status": "not_dispatched"},
                    )
                return failure("bland.dispatch_rejected")
            return failure("bland.outcome_unknown", uncertain=True)
        try:
            provider_id = call_identifier(value.get("provider_call_id"))
            if value.get("status") != "accepted":
                raise ValueError
        except ValueError:
            return failure("bland.outcome_unknown", uncertain=True)
        async with self.uow_factory() as uow, uow.calls.lock(self.owner):
            row = await uow.calls.get(self.owner, "dispatch", key)
            assert row is not None
            if row.payload.get("provider_call_id") == provider_id and row.payload.get(
                "status"
            ) not in {"uncertain", "accepted"}:
                return result({"provider_call_id": provider_id, "status": row.payload["status"]})
            await self._put(
                uow.calls,
                "dispatch",
                key,
                {**row.payload, "status": "accepted", "provider_call_id": provider_id},
            )
            await self._enqueue(uow.calls, provider_id)
        return result({"provider_call_id": provider_id, "status": "accepted"})

    async def receive(self, body: bytes, signature: str, secret: str) -> bool:
        if (
            not secret
            or len(body) > MAX_CALLBACK_BYTES
            or len(signature) != 64
            or any(ch not in "0123456789abcdef" for ch in signature)
        ):
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                return False
            key = call_identifier(payload.get("call_id"))
        except (ValueError, UnicodeError, RecursionError):
            return False
        async with self.uow_factory() as uow, uow.calls.lock(self.owner):
            await self._enqueue(uow.calls, key)
        return True

    async def reconcile(self) -> int:
        await self.expire()
        if self.provider is None:
            return 0
        await self._scan()
        async with self.uow_factory() as uow:
            receipts = await uow.calls.list(
                self.owner, "receipt", active=True, limit=25, due_at=self.clock.now()
            )
        count = 0
        for receipt in receipts:
            response = await self._provider(
                "bland_read", "provider_get_call", {"provider_call_id": receipt.key}
            )
            if response.is_error:
                await self._retry(receipt.key)
                continue
            try:
                call = ProviderCall.model_validate(response.structured)
                created = datetime.fromisoformat(call.created_at)
                if (
                    call.provider_call_id != receipt.key
                    or call.number != self.configuration.phone_number
                    or not self.clock.now() - RETENTION
                    <= created
                    <= self.clock.now() + timedelta(minutes=5)
                ):
                    raise ValueError("call binding or retention mismatch")
            except ValueError:
                async with self.uow_factory() as uow, uow.calls.lock(self.owner):
                    await self._put(
                        uow.calls, "receipt", receipt.key, {"active": False, "status": "rejected"}
                    )
                continue
            async with self.uow_factory() as uow, uow.calls.lock(self.owner):
                previous = await uow.calls.get(self.owner, "call", receipt.key)
                if previous is not None and previous.payload.get("status") != "active":
                    if not pending_content(previous.payload):
                        continue
                    # Fill only content that had not arrived. Preserve accepted source text,
                    # identity and terminal state even if a later provider view changes them.
                    merged = {key: previous.payload[key] for key in call.model_dump()}
                    for field in ("summary", "transcript"):
                        if not merged[field] and merged[field + "_complete"] is False:
                            merged[field] = getattr(call, field)
                            merged[field + "_complete"] = getattr(call, field + "_complete")
                    call = ProviderCall.model_validate(merged)
                dispatch = (
                    None
                    if call.request_id is None
                    else await uow.calls.get(self.owner, "dispatch", call.request_id)
                )
                if call.direction == "outbound" and (
                    dispatch is None
                    or dispatch.payload.get("account_id") != self.configuration.account_id
                    or dispatch.payload.get("recipient_hash")
                    != hashlib.sha256(call.counterparty.encode()).hexdigest()
                ):
                    await self._put(
                        uow.calls, "receipt", receipt.key, {"active": False, "status": "rejected"}
                    )
                    continue
                terminal = call.status != "active"
                await self._put(
                    uow.calls,
                    "call",
                    receipt.key,
                    {
                        **call.model_dump(),
                        "call_id": receipt.key,
                        "trust": "EXTERNAL_UNTRUSTED",
                        "account_id": self.configuration.account_id,
                        "source": "bland",
                        "caller_identity_verified": False,
                    },
                )
                await self._put(
                    uow.calls,
                    "receipt",
                    receipt.key,
                    {
                        "active": not terminal or pending_content(call.model_dump()),
                        "status": "received"
                        if terminal and not pending_content(call.model_dump())
                        else "pending",
                        "retry_at": (self.clock.now() + timedelta(seconds=60)).isoformat(),
                    },
                )
                if dispatch is not None:
                    await self._put(
                        uow.calls,
                        "dispatch",
                        dispatch.key,
                        {
                            **dispatch.payload,
                            "active": not terminal,
                            "status": call.status,
                            "provider_call_id": call.provider_call_id,
                        },
                    )
                notification_time = self.clock.now()
                notification_expiry = datetime.fromisoformat(call.created_at) + RETENTION
                if terminal and self.notifications and notification_time < notification_expiry:
                    notification_id = uuid5(
                        NAMESPACE_URL,
                        json.dumps(
                            [
                                "veetbot.call.finished",
                                self.owner.tenant_id,
                                self.owner.principal_id,
                                self.configuration.account_id,
                                receipt.key,
                            ]
                        ),
                    )
                    await uow.notification_outbox.enqueue(
                        NewNotification(
                            id=notification_id,
                            tenant_id=self.owner.tenant_id,
                            principal_id=self.owner.principal_id,
                            kind=NotificationKind.CALL_FINISHED,
                            dedupe_key="call.finished:" + str(notification_id),
                            payload=NotificationPayload(
                                kind=NotificationKind.CALL_FINISHED,
                                title="New call result",
                                notification_id=notification_id,
                                call_id=UUID(receipt.key),
                            ),
                            priority=10,
                            next_attempt_at=notification_time,
                            created_at=notification_time,
                            expires_at=notification_expiry,
                        )
                    )
                count += int(previous is None)
        await self._cancel_requested()
        return count

    async def list_calls(
        self, principal: Principal, *, limit: int = 10, cursor: str | None = None
    ) -> dict[str, Any]:
        self._owner(principal)
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("call page limit must be between 1 and 25")
        if cursor is not None:
            cursor = call_identifier(cursor)
        async with self.uow_factory() as uow:
            rows = await uow.calls.list(principal, "call", after=cursor, limit=limit)
        # Stable identifier ordering and a cursor, without embedding private data in the cursor.
        return {
            "calls": [
                {k: v for k, v in self._visible(row).items() if k != "transcript"} for row in rows
            ],
            "next_cursor": rows[-1].key if len(rows) == limit else None,
        }

    async def get_call(self, principal: Principal, call_id: str) -> dict[str, Any]:
        self._owner(principal)
        key = call_identifier(call_id)
        async with self.uow_factory() as uow:
            row = await uow.calls.get(principal, "call", key)
        if row is None:
            raise NotFoundError("call not found")
        return self._visible(row)

    async def stop(self, principal: Principal, call_id: str) -> dict[str, Any]:
        self._owner(principal)
        key = call_identifier(call_id)
        async with self.uow_factory() as uow:
            row = await uow.calls.get(principal, "call", key)
            reservations = await uow.calls.list(principal, "dispatch", active=True, limit=1)
        if row is None and not any(r.payload.get("provider_call_id") == key for r in reservations):
            raise NotFoundError("call not found")
        response = await self._provider(
            "bland_call", "provider_stop_call", {"provider_call_id": key}
        )
        confirmed = not response.is_error and (response.structured or {}).get("stopped") is True
        return {"call_id": key, "termination": "confirmed" if confirmed else "uncertain"}

    async def delete(self, principal: Principal, call_id: str) -> dict[str, Any]:
        self._owner(principal)
        key = call_identifier(call_id)
        async with self.uow_factory() as uow, uow.calls.lock(self.owner):
            row = await uow.calls.get(principal, "call", key)
            if row is None:
                raise NotFoundError("call not found")
            if row.payload.get("status") == "active":
                raise ConflictError("the call must finish before its content can be erased")
            counts = await uow.session_deletions.erase_call_source(principal, key, self.clock.now())
            await self._put(
                uow.calls,
                "call",
                key,
                {
                    "erased": True,
                    "call_id": key,
                    "status": "erased",
                    "provider_deleted": False,
                    "cleanup": counts,
                },
            )
            await self._put(uow.calls, "receipt", key, {"active": False, "status": "erased"})
        return {"call_id": key, "erased": True, "provider_deleted": False}

    async def expire(self) -> int:
        async with self.uow_factory() as uow:
            cursor = await uow.calls.get(self.owner, "cursor", "retention")
            after = None if cursor is None else cast(str | None, cursor.payload.get("after"))
            rows = await uow.calls.list(self.owner, "call", after=after, limit=25)
        count = 0
        for row in rows:
            created = row.payload.get("created_at")
            if (
                isinstance(created, str)
                and datetime.fromisoformat(created) < self.clock.now() - RETENTION
            ):
                try:
                    await self.delete(self.owner, row.key)
                    count += 1
                except ConflictError:
                    continue
        async with self.uow_factory() as uow, uow.calls.lock(self.owner):
            await self._put(
                uow.calls,
                "cursor",
                "retention",
                {"after": rows[-1].key if len(rows) == 25 else None},
            )
        return count

    def _owner(self, principal: Principal) -> None:
        if (principal.tenant_id, principal.principal_id) != (
            self.owner.tenant_id,
            self.owner.principal_id,
        ):
            raise NotFoundError("call not found")

    def _visible(self, row: CallRecord) -> dict[str, Any]:
        created = row.payload.get("created_at")
        if row.payload.get("erased") or (
            isinstance(created, str)
            and datetime.fromisoformat(created) < self.clock.now() - RETENTION
        ):
            return {"call_id": row.key, "erased": True, "provider_deleted": False}
        return dict(row.payload)

    async def _put(
        self, store: CallStore, kind: str, key: str, payload: dict[str, Any]
    ) -> CallRecord:
        existing = await store.get(self.owner, kind, key)
        revision = 0 if existing is None else existing.revision
        return await store.put(
            CallRecord(
                tenant_id=self.owner.tenant_id,
                principal_id=self.owner.principal_id,
                kind=kind,
                key=key,
                revision=revision + 1,
                payload=payload,
                created_at=self.clock.now() if existing is None else existing.created_at,
                updated_at=self.clock.now(),
            ),
            expected_revision=revision,
        )

    async def _enqueue(self, store: CallStore, key: str) -> None:
        if await store.get(self.owner, "receipt", key) is not None:
            return
        if len(await store.list(self.owner, "receipt", active=True, limit=1000)) >= 1000:
            raise ConflictError("call callback queue is full")
        await self._put(store, "receipt", key, {"active": True, "status": "pending"})

    async def _cancel_requested(self) -> None:
        async with self.uow_factory() as uow:
            reservations = await uow.calls.list(self.owner, "dispatch", active=True, limit=1)
        for dispatch in reservations:
            provider_id = dispatch.payload.get("provider_call_id")
            if (
                not isinstance(provider_id, str)
                or dispatch.payload.get("termination") == "confirmed"
            ):
                continue
            async with self.uow_factory() as uow:
                try:
                    run = await uow.runs.get(UUID(str(dispatch.payload["run_id"])), self.owner)
                    requested = (
                        run.cancel_requested_at is not None or run.status.value == "CANCELLED"
                    )
                except NotFoundError:
                    requested = True
            if not requested:
                continue
            outcome = await self.stop(self.owner, provider_id)
            async with self.uow_factory() as uow, uow.calls.lock(self.owner):
                current = await uow.calls.get(self.owner, "dispatch", dispatch.key)
                if current is not None:
                    await self._put(
                        uow.calls,
                        "dispatch",
                        current.key,
                        {**current.payload, "termination": outcome["termination"]},
                    )

    async def _provider(self, server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if self.provider is None:
            return failure("bland.provider_unavailable", uncertain=name == "provider_stop_call")
        try:
            async with asyncio.timeout(30):
                return await self.provider(server, name, args)
        except Exception:
            return failure("bland.provider_unavailable", uncertain=name == "provider_stop_call")

    async def _retry(self, key: str) -> None:
        async with self.uow_factory() as uow, uow.calls.lock(self.owner):
            row = await uow.calls.get(self.owner, "receipt", key)
            if row is None or not row.payload.get("active"):
                return
            attempts = min(8, int(cast(int, row.payload.get("attempts", 0))) + 1)
            await self._put(
                uow.calls,
                "receipt",
                key,
                {
                    "active": row.created_at >= self.clock.now() - RETENTION,
                    "status": "pending",
                    "attempts": attempts,
                    "retry_at": (
                        self.clock.now() + timedelta(seconds=min(3600, 60 * 2**attempts))
                    ).isoformat(),
                },
            )

    async def _scan(self) -> None:
        # One 25-id page per direction each minute. Persist progress across process restarts.
        for inbound in (True, False):
            key = "inbound" if inbound else "outbound"
            async with self.uow_factory() as uow, uow.calls.lock(self.owner):
                cursor = await uow.calls.get(self.owner, "cursor", key)
                if (
                    cursor is not None
                    and str(cursor.payload.get("retry_at", "")) > self.clock.now().isoformat()
                ):
                    continue
                start = (
                    (self.clock.now() - RETENTION).isoformat()
                    if cursor is None
                    else str(cursor.payload["start"])
                )
                offset = 0 if cursor is None else int(cast(int, cursor.payload.get("offset", 0)))
                payload = {
                    "start": start,
                    "offset": offset,
                    "retry_at": (self.clock.now() + timedelta(seconds=60)).isoformat(),
                }
                await self._put(uow.calls, "cursor", key, payload)
            page = await self._provider(
                "bland_read",
                "provider_list_calls",
                {"inbound": inbound, "start_date": start, "offset": offset, "limit": 25},
            )
            if page.is_error:
                continue
            values = page.structured or {}
            ids = values.get("provider_call_ids")
            next_offset = values.get("next_offset")
            try:
                if not isinstance(ids, list) or len(ids) > 25:
                    raise ValueError
                validated = [call_identifier(value) for value in ids]
                if next_offset is not None and (
                    type(next_offset) is not int or next_offset != offset + 25
                ):
                    raise ValueError
            except ValueError:
                continue
            async with self.uow_factory() as uow, uow.calls.lock(self.owner):
                for provider_id in validated:
                    await self._enqueue(uow.calls, provider_id)
                # Keep an overlap after a completed scan for provider visibility delays.
                await self._put(
                    uow.calls,
                    "cursor",
                    key,
                    {
                        "offset": next_offset or 0,
                        "start": start
                        if next_offset is not None
                        else (self.clock.now() - timedelta(days=1)).isoformat(),
                        "retry_at": payload["retry_at"],
                    },
                )
