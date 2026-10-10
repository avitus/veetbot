"""Owner inspection and guarded undo; processing activation is independent."""

import base64
import binascii
import hashlib
import json
from typing import Literal
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import DreamingSchedule, DreamingStatus
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.reconsolidation_views import (
    OperationCursor,
    OperationKind,
    OperationState,
    OperationView,
    ReconsolidationValidationError,
)
from agent_core.domain.views import Page
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory


def _binding(
    principal: Principal,
    ceiling: Sensitivity,
    kind: OperationKind | None,
    state: OperationState | None,
) -> str:
    return hashlib.sha256(
        json.dumps(
            [principal.tenant_id, principal.principal_id, ceiling.value, kind, state],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _decode(cursor: str | None, binding: str) -> OperationCursor | None:
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError("invalid cursor size")
        data = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(data, dict)
            or set(data) != {"v", "binding", "position"}
            or type(data["v"]) is not int
            or data["v"] != 1
            or data["binding"] != binding
        ):
            raise ValueError("invalid cursor binding")
        return OperationCursor.model_validate(data["position"])
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ReconsolidationValidationError("reconsolidation cursor is invalid") from exc


def _encode(view: OperationView, binding: str) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "v": 1,
                    "binding": binding,
                    "position": {
                        "created_at": view.created_at.isoformat(),
                        "id": str(view.id),
                    },
                },
                separators=(",", ":"),
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )


class PublicReconsolidationService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def dreaming_status(self, principal: Principal) -> DreamingStatus:
        require_scope(principal, "memory.read")
        async with self._uow_factory() as uow:
            return DreamingStatus(
                schedule=await uow.reconsolidation.dreaming_schedule(principal),
                runs=await uow.reconsolidation.dreaming_runs(principal, self._clock.now()),
            )

    async def pause_dreaming(
        self, principal: Principal, *, paused: bool, expected_revision: int
    ) -> DreamingSchedule:
        require_scope(principal, "memory.write")
        if type(paused) is not bool or type(expected_revision) is not int or expected_revision < 1:
            raise ReconsolidationValidationError("invalid dreaming schedule revision")
        async with self._uow_factory() as uow:
            return await uow.reconsolidation.pause_dreaming(principal, paused, expected_revision)

    async def decide(
        self,
        principal: Principal,
        operation_id: UUID,
        *,
        ceiling: Sensitivity,
        expected_revision: int,
        decision: Literal["approved", "rejected"],
        key: str,
    ) -> OperationView:
        require_scope(principal, "memory.write")
        require_scope(principal, "memory.read")
        if (
            type(expected_revision) is not int
            or expected_revision < 1
            or decision not in {"approved", "rejected"}
            or not key.strip()
            or len(key) > 128
        ):
            raise ReconsolidationValidationError(
                "decision requires a revision and bounded idempotency key"
            )
        async with self._uow_factory() as uow, uow.people.lock(principal):
            now = self._clock.now()
            view = await uow.reconsolidation.get_operation(
                principal, operation_id, now, ceiling=ceiling
            )
            if view is None:
                raise NotFoundError("reconsolidation operation not found")
            await uow.reconsolidation.decide_owner_review(
                principal, operation_id, expected_revision, decision, key, now
            )
            result = await uow.reconsolidation.get_operation(
                principal, operation_id, now, ceiling=ceiling
            )
            assert result is not None
            return result

    async def get(
        self, principal: Principal, operation_id: UUID, *, ceiling: Sensitivity
    ) -> OperationView:
        require_scope(principal, "memory.read")
        async with self._uow_factory() as uow, uow.people.lock(principal):
            value = await uow.reconsolidation.get_operation(
                principal, operation_id, self._clock.now(), ceiling=ceiling
            )
            if value is None:
                raise NotFoundError("reconsolidation operation not found")
            return value

    async def list(
        self,
        principal: Principal,
        *,
        ceiling: Sensitivity,
        kind: OperationKind | None = None,
        state: OperationState | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[OperationView]:
        require_scope(principal, "memory.read")
        if (
            type(limit) is not int
            or limit < 1
            or kind not in {None, "merge", "summary", "hypothesis", "conflict"}
            or state
            not in {None, "proposed", "committed", "rejected", "stale", "invalidated", "undone"}
        ):
            raise ReconsolidationValidationError("reconsolidation filters are invalid")
        limit = min(limit, 100)
        binding = _binding(principal, ceiling, kind, state)
        before = _decode(cursor, binding)
        items: list[OperationView] = []
        try:
            async with self._uow_factory() as uow, uow.people.lock(principal):
                now = self._clock.now()
                while len(items) <= limit:
                    rows = await uow.reconsolidation.operation_page(
                        principal, kind=kind, state=None, before=before, limit=100
                    )
                    for row in rows:
                        value = await uow.reconsolidation.get_operation(
                            principal, row.id, now, ceiling=ceiling
                        )
                        if value is not None and (state is None or value.state == state):
                            items.append(value)
                            if len(items) > limit:
                                break
                    if len(rows) < 100 or len(items) > limit:
                        break
                    before = OperationCursor(created_at=rows[-1].created_at, id=rows[-1].id)
        except NotFoundError:
            return Page[OperationView](items=[], next_cursor=None)
        page = items[:limit]
        return Page[OperationView](
            items=page, next_cursor=_encode(page[-1], binding) if len(items) > limit else None
        )

    async def undo(
        self,
        principal: Principal,
        operation_id: UUID,
        *,
        ceiling: Sensitivity,
        expected_revision: int,
        key: str,
    ) -> OperationView:
        require_scope(principal, "memory.write")
        if (
            type(expected_revision) is not int
            or expected_revision < 1
            or not key.strip()
            or len(key) > 128
        ):
            raise ReconsolidationValidationError(
                "undo requires a positive revision and a bounded idempotency key"
            )
        async with self._uow_factory() as uow, uow.people.lock(principal):
            now = self._clock.now()
            value = await uow.reconsolidation.get_operation(
                principal, operation_id, now, ceiling=ceiling
            )
            if value is None:
                raise NotFoundError("reconsolidation operation not found")
            if value.kind != "merge" or value.state not in {"committed", "undone"}:
                raise ConflictError("only a committed merge can be undone")
            await uow.reconsolidation.undo_merge(
                principal, operation_id, expected_revision, key, now
            )
            result = await uow.reconsolidation.get_operation(
                principal, operation_id, now, ceiling=ceiling
            )
            assert result is not None
            return result
