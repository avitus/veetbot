"""Memory review and deletion over HTTP (ADR-0117).

Two write routes join the read-only router: ``DELETE /v1/memories/{id}`` and
``POST /v1/memories/{id}/review``. Both require ``memory.write``, a bounded
``Idempotency-Key`` and the caller's ceiling; a belief above the ceiling is
indistinguishable from an absent one, exactly as on a read. The list route
gains a ``flagged`` filter so the review queue can be asked for directly.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.domain.memory import MemoryReviewOutcome, MemoryStatus, Portability, Sensitivity
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import AGENT_ID, NOW
from tests.gates.memory_api_support import memory_routes
from tests.gates.test_memory_read_api_m17 import (
    PRINCIPAL_ID,
    SESSION_A,
    TENANT,
    _belief,
    _client,
    _enabled_settings,
    _principal,
    _seed,
)
from tests.integration.m2_support import memory_settings

WRITER = _principal(scopes=set(PLATFORM_SCOPES) | {"memory.read", "memory.write"})


def _flagged(**changes: Any) -> Any:
    record = _belief(**changes)
    return record.model_copy(update={"flagged_for_review": True})


async def _seed_with_session(composition: Any, records: list[Any]) -> None:
    """Beliefs cite a source session; the receipts of ADR-0117 are appended to it."""
    async with composition.uow_factory() as uow:
        await uow.sessions.create(
            Session(
                id=SESSION_A,
                tenant_id=TENANT,
                principal_id=PRINCIPAL_ID,
                agent_id=AGENT_ID,
                agent_version="1.0.0",
                status=SessionStatus.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    await _seed(composition, records)


async def _events(composition: Any, session_id: UUID) -> list[str]:
    async with composition.uow_factory() as uow:
        events = await uow.events.list_after(session_id, 0, composition.principal)
    return [event.event_type for event in events]


async def test_delete_removes_a_belief_and_a_repeated_key_replays_the_result() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        doomed = _flagged(belief_id=1, position=1, sensitivity=Sensitivity.SENSITIVE)
        await _seed_with_session(composition, [doomed])
        async with _client(composition) as client:
            deleted = await client.delete(
                f"/v1/memories/{doomed.id}",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "delete-1"},
            )
            assert deleted.status_code == 204, deleted.text
            gone = await client.get(f"/v1/memories/{doomed.id}", params={"ceiling": "restricted"})
            assert gone.status_code == 404
            # The same key replays the completed result rather than failing on the tombstone.
            again = await client.delete(
                f"/v1/memories/{doomed.id}",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "delete-1"},
            )
            assert again.status_code == 204, again.text
            # A different key finds nothing to delete.
            fresh = await client.delete(
                f"/v1/memories/{doomed.id}",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "delete-2"},
            )
            assert fresh.status_code == 404
        assert "memory.deleted" in await _events(composition, doomed.source_session_id)


async def test_review_outcomes_clear_the_flag_retire_or_localize() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        dismissed = _flagged(belief_id=1, position=1, sensitivity=Sensitivity.SENSITIVE)
        untrue = _flagged(belief_id=2, position=2, sensitivity=Sensitivity.SENSITIVE)
        elsewhere = _flagged(belief_id=3, position=3, sensitivity=Sensitivity.SENSITIVE)
        await _seed_with_session(composition, [dismissed, untrue, elsewhere])
        async with _client(composition) as client:

            async def review(belief_id: UUID, outcome: str, key: str) -> Any:
                response = await client.post(
                    f"/v1/memories/{belief_id}/review",
                    params={"ceiling": "restricted"},
                    headers={"Idempotency-Key": key},
                    json={"outcome": outcome},
                )
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "private, no-store"
                return response.json()

            reviewed = await review(dismissed.id, "dismiss", "review-1")
            assert reviewed["flagged_for_review"] is False
            assert reviewed["status"] == MemoryStatus.ACTIVE.value
            assert reviewed["confidence"] == dismissed.confidence
            assert reviewed["authority"] == dismissed.authority.value
            # Dismissal took a fresh store position, so the newest-first list now
            # leads with it: the next recall delta is how the owner learns it moved.
            listing = await client.get("/v1/memories", params={"ceiling": "restricted"})
            assert listing.json()["items"][0]["id"] == str(dismissed.id)

            retired = await review(untrue.id, "untrue", "review-2")
            assert retired["status"] == MemoryStatus.RETIRED.value
            assert retired["valid_to"] is not None

            localized = await review(elsewhere.id, "not_here", "review-3")
            assert localized["portability"] == Portability.LOCAL.value
            assert localized["status"] == MemoryStatus.ACTIVE.value

            # The queue drops the dismissed belief; a retired one leaves the live
            # default set; a localized one stays flagged and down-weighted, as the
            # correction table says.
            queue = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "flagged": "true"}
            )
            assert {item["id"] for item in queue.json()["items"]} == {str(elsewhere.id)}
            # Replaying the key returns the recorded view; a reused key with a
            # different request is a conflict.
            replay = await review(dismissed.id, "dismiss", "review-1")
            assert replay == reviewed
            reused = await client.post(
                f"/v1/memories/{dismissed.id}/review",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "review-1"},
                json={"outcome": "untrue"},
            )
            assert reused.status_code == 409
            assert reused.json()["error"]["code"] == "conflict"
        events = await _events(composition, dismissed.source_session_id)
        assert "memory.reviewed" in events
        assert "memory.rejected" in events


async def test_write_routes_require_scope_key_and_ceiling_and_hide_beliefs_above_it() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        restricted = _flagged(belief_id=1, position=1, sensitivity=Sensitivity.RESTRICTED)
        await _seed_with_session(composition, [restricted])
        path = f"/v1/memories/{restricted.id}"
        async with _client(composition) as client:
            no_key = await client.delete(path, params={"ceiling": "restricted"})
            assert no_key.status_code == 400
            assert no_key.json()["error"]["code"] == "malformed_request"
            no_ceiling = await client.delete(path, headers={"Idempotency-Key": "k"})
            assert no_ceiling.status_code == 400
            above = await client.post(
                f"{path}/review",
                params={"ceiling": "internal"},
                headers={"Idempotency-Key": "k"},
                json={"outcome": "dismiss"},
            )
            assert above.status_code == 404
            assert above.json()["error"]["code"] == "not_found"
            bad_outcome = await client.post(
                f"{path}/review",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "k"},
                json={"outcome": "changed"},
            )
            assert bad_outcome.status_code == 400
        reader = _principal(scopes=set(PLATFORM_SCOPES) - {"memory.write"})
        async with _client(composition, principal=reader) as client:
            forbidden = await client.delete(
                path, params={"ceiling": "restricted"}, headers={"Idempotency-Key": "k"}
            )
            assert forbidden.status_code == 403
            assert forbidden.json()["error"]["code"] == "authorization_error"
        async with composition.uow_factory() as uow:
            untouched = await uow.memories.get(restricted.id, WRITER)
        assert untouched.flagged_for_review is True


async def test_the_flag_removes_the_write_routes_with_the_read_routes() -> None:
    async with build(
        settings=memory_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        assert memory_routes(app) == []
        async with _client(composition) as client:
            response = await client.delete(
                f"/v1/memories/{UUID(int=1)}",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "k"},
            )
            assert response.status_code == 404
    assert "memory.write" in PLATFORM_SCOPES


async def test_the_router_exposes_exactly_the_documented_routes() -> None:
    """Two reads with memory.read and exactly two writes with memory.write."""
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
    table = {
        (route.path, method): (route.openapi_extra or {}).get("required_scope")
        for route in memory_routes(app)
        for method in (route.methods or set()) - {"HEAD"}
    }
    assert table == {
        ("/v1/memories", "GET"): "memory.read",
        ("/v1/memories/{memory_id}", "GET"): "memory.read",
        ("/v1/memories/{memory_id}", "DELETE"): "memory.write",
        ("/v1/memories/{memory_id}/review", "POST"): "memory.write",
    }
    document = app.openapi()
    for path, method in (
        ("/v1/memories/{memory_id}", "delete"),
        ("/v1/memories/{memory_id}/review", "post"),
    ):
        parameters = document["paths"][path][method]["parameters"]
        assert any(
            p["name"] == "Idempotency-Key" and p["in"] == "header" and p["required"]
            for p in parameters
        )
        assert any(p["name"] == "ceiling" and p["required"] for p in parameters)


async def test_the_flagged_filter_composes_with_the_others() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        corpus = [
            _flagged(belief_id=1, position=1),
            _belief(belief_id=2, position=2),
            _flagged(belief_id=3, position=3, status=MemoryStatus.SUPERSEDED),
        ]
        await _seed(composition, corpus)
        async with _client(composition) as client:

            async def listed(**params: Any) -> set[str]:
                response = await client.get(
                    "/v1/memories", params={"ceiling": "restricted", **params}
                )
                assert response.status_code == 200, response.text
                return {item["id"] for item in response.json()["items"]}

            assert await listed(flagged="true") == {str(UUID(int=1))}
            assert await listed(flagged="false") == {str(UUID(int=2))}
            assert await listed() == {str(UUID(int=1)), str(UUID(int=2))}
            assert await listed(flagged="true", status="superseded") == {str(UUID(int=3))}
            malformed = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "flagged": "maybe"}
            )
            assert malformed.status_code == 400


async def test_receipts_hold_no_statement_and_a_deleted_belief_cannot_be_replayed() -> None:
    """A replay rebuilds the view from the live record, so deletion also ends replay."""
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        belief = _flagged(belief_id=1, position=1, sensitivity=Sensitivity.SENSITIVE)
        await _seed_with_session(composition, [belief])
        async with _client(composition) as client:
            reviewed = await client.post(
                f"/v1/memories/{belief.id}/review",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "review-then-delete"},
                json={"outcome": "dismiss"},
            )
            assert reviewed.status_code == 200, reviewed.text
            deleted = await client.delete(
                f"/v1/memories/{belief.id}",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "delete-after-review"},
            )
            assert deleted.status_code == 204
            replay = await client.post(
                f"/v1/memories/{belief.id}/review",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "review-then-delete"},
                json={"outcome": "dismiss"},
            )
            assert replay.status_code == 404, replay.text
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(belief.source_session_id, 0, WRITER)
        receipts = [
            event
            for event in events
            if event.event_type in {"memory.review_completed", "memory.delete_completed"}
        ]
        assert len(receipts) == 2
        for receipt in receipts:
            assert set(receipt.payload) == {"request_hash", "memory_id"}, receipt.payload
            assert belief.statement not in str(receipt.payload)


async def test_concurrent_requests_with_one_key_apply_the_write_once() -> None:
    """The write and its receipt commit together under the owner's locks."""
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        belief = _flagged(belief_id=1, position=1, sensitivity=Sensitivity.SENSITIVE)
        await _seed_with_session(composition, [belief])
        service = composition.services.memory
        results = await asyncio.gather(
            *(
                service.review(
                    WRITER,
                    belief.id,
                    MemoryReviewOutcome.NOT_HERE,
                    ceiling=Sensitivity.RESTRICTED,
                    key="shared-key",
                )
                for _ in range(2)
            )
        )
        assert results[0] == results[1]
        assert results[0].confidence == pytest.approx(belief.confidence - 0.2)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(belief.source_session_id, 0, WRITER)
        assert [event.event_type for event in events].count("memory.rejected") == 1
        assert [event.event_type for event in events].count("memory.review_completed") == 1
