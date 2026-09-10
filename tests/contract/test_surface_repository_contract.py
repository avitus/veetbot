"""Shared contracts for Milestone 14 surface persistence ports."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.surfaces import (
    InboundDisposition,
    InboundReceipt,
    Pairing,
    SurfaceReply,
    SurfaceSession,
    issue_pairing_code,
)

NOW = datetime(2026, 9, 10, 21, 0, tzinfo=UTC)
SURFACE_ID = UUID("00000000-0000-4000-8000-000000001400")
SESSION_ID = UUID("00000000-0000-4000-8000-000000001401")
RUN_ID = UUID("00000000-0000-4000-8000-000000001402")


class FixedClock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        del seconds


def owner() -> Principal:
    return Principal(
        tenant_id="tenant-a",
        principal_id="owner",
        scopes={"run.read", "run.write", "surface.read", "surface.write"},
    )


def stranger() -> Principal:
    return Principal(
        tenant_id="tenant-b",
        principal_id="stranger",
        scopes={"surface.read", "surface.write"},
    )


async def assert_surface_repositories_contract(repositories: object) -> None:
    admission = repositories.admission  # type: ignore[attr-defined]
    pairings = repositories.pairings  # type: ignore[attr-defined]
    sessions = repositories.sessions  # type: ignore[attr-defined]
    receipts = repositories.receipts  # type: ignore[attr-defined]
    replies = repositories.replies  # type: ignore[attr-defined]

    admission_decision = await admission.check(owner().tenant_id, Decimal("1"), NOW)
    assert admission_decision.allowed is True
    assert admission_decision.reason_code is None

    issued = issue_pairing_code(
        pairing_id=UUID("00000000-0000-4000-8000-000000001410"),
        surface_id=SURFACE_ID,
        tenant_id=owner().tenant_id,
        principal_id=owner().principal_id,
        created_by_principal_id=owner().principal_id,
        granted_scopes=frozenset({"run.read", "run.write"}),
        label="Owner",
        now=NOW,
        expires_after=timedelta(minutes=10),
        max_attempts=5,
        code="B7zF4nQ2",
        salt=b"0123456789abcdef",
    )
    assert await pairings.create_code(issued.record, owner()) == issued.record
    assert await pairings.active_codes(SURFACE_ID, NOW, owner()) == [issued.record]
    assert await pairings.active_codes_for_surface(SURFACE_ID, NOW) == [issued.record]
    assert await pairings.active_codes(SURFACE_ID, NOW, stranger()) == []
    attempted = await pairings.record_code_attempt(issued.record.id, owner())
    assert attempted.attempts == 1
    consumed = await pairings.consume_code(attempted.id, NOW, owner())
    assert consumed.consumed_at == NOW
    assert await pairings.active_codes(SURFACE_ID, NOW, owner()) == []

    pairing = Pairing(
        id=UUID("00000000-0000-4000-8000-000000001411"),
        surface_id=SURFACE_ID,
        tenant_id=owner().tenant_id,
        principal_id=owner().principal_id,
        sender_id="sender-1",
        sender_label="Owner",
        granted_scopes=frozenset({"run.read", "run.write"}),
        paired_at=NOW,
    )
    assert await pairings.create_pairing(pairing) == pairing
    assert await pairings.live_pairing(SURFACE_ID, "sender-1") == pairing
    assert await pairings.list_pairings(SURFACE_ID, owner()) == [pairing]
    assert await pairings.list_pairings(SURFACE_ID, stranger()) == []
    touched = await pairings.touch_pairing(pairing.id, NOW + timedelta(minutes=1))
    assert touched.last_message_at == NOW + timedelta(minutes=1)

    mapping = SurfaceSession(
        id=UUID("00000000-0000-4000-8000-000000001412"),
        surface_id=SURFACE_ID,
        tenant_id=owner().tenant_id,
        principal_id=owner().principal_id,
        external_key="dm:sender-1",
        session_id=SESSION_ID,
        created_at=NOW,
    )
    assert await sessions.create(mapping) == mapping
    assert await sessions.live(SURFACE_ID, mapping.external_key) == mapping
    touched_mapping = await sessions.touch_inbound(mapping.id, NOW + timedelta(minutes=1))
    assert touched_mapping.last_inbound_at == NOW + timedelta(minutes=1)
    rotated = await sessions.rotate(mapping.id, NOW + timedelta(minutes=2))
    assert rotated.rotated_at == NOW + timedelta(minutes=2)
    assert await sessions.live(SURFACE_ID, mapping.external_key) is None

    receipt = InboundReceipt(
        surface_id=SURFACE_ID,
        external_update_id="wamid.1",
        received_at=NOW,
        disposition=InboundDisposition.REJECTED_UNPAIRED,
        reason_code="surface.unpaired",
    )
    assert await receipts.create(receipt) is True
    assert await receipts.create(receipt) is False
    assert await receipts.get(SURFACE_ID, "wamid.1") == receipt
    assert await receipts.latest_numeric_update_id(SURFACE_ID) is None
    numeric_receipt = receipt.model_copy(update={"external_update_id": "42"})
    assert await receipts.create(numeric_receipt) is True
    assert await receipts.latest_numeric_update_id(SURFACE_ID) == 42

    reply = SurfaceReply(
        id=UUID("00000000-0000-4000-8000-000000001413"),
        surface_id=SURFACE_ID,
        tenant_id=owner().tenant_id,
        principal_id=owner().principal_id,
        run_id=RUN_ID,
        chat_ref="sender-1",
        next_attempt_at=NOW,
        created_at=NOW,
    )
    assert await replies.enqueue(reply) is True
    assert await replies.enqueue(reply) is False
    assert await replies.claim_due(NOW, 10, "worker-1", 30) == [
        reply.model_copy(
            update={
                "claimed_by": "worker-1",
                "claimed_until": NOW + timedelta(seconds=30),
                "attempts": 1,
            }
        )
    ]
    progressed = await replies.record_chunk(
        reply.id,
        worker_id="worker-1",
        chunks_total=2,
        at=NOW,
    )
    assert progressed.chunks_sent == 1
    assert progressed.chunks_total == 2

    revoked = await pairings.revoke_pairing(pairing.id, owner(), NOW + timedelta(minutes=3))
    assert revoked.revoked_at == NOW + timedelta(minutes=3)
    assert await pairings.live_pairing(SURFACE_ID, "sender-1") is None


def in_memory_surface_repositories() -> object:
    spec = importlib.util.find_spec("agent_core.adapters.persistence.surfaces")
    assert spec is not None, "Milestone 14 surface repositories have not been implemented"
    from agent_core.adapters.persistence.surfaces import (
        InMemorySurfacePairingRepository,
        InMemorySurfaceReceiptRepository,
        InMemorySurfaceReplyOutbox,
        InMemorySurfaceRepositories,
        InMemorySurfaceSessionRepository,
    )
    from agent_core.adapters.surface_admission import AllowSurfaceAdmissionController

    return InMemorySurfaceRepositories(
        admission=AllowSurfaceAdmissionController(),
        pairings=InMemorySurfacePairingRepository(),
        sessions=InMemorySurfaceSessionRepository(),
        receipts=InMemorySurfaceReceiptRepository(),
        replies=InMemorySurfaceReplyOutbox(),
    )


def test_postgres_surface_adapter_is_registered_for_shared_contract() -> None:
    from agent_core.adapters.persistence import surfaces

    assert hasattr(surfaces, "PostgresSurfaceRepositories"), (
        "PostgreSQL surface repositories are not implemented"
    )


async def test_in_memory_surface_repositories_satisfy_shared_contract() -> None:
    await assert_surface_repositories_contract(in_memory_surface_repositories())
