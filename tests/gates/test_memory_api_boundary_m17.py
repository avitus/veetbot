"""Milestone 17 memory read API boundary coverage (PR 1, task 4).

Sibling-surface boundary tests for `/v1/memories` — the service, the two
routes, the config flag, and the `memory.read` scope. The formal ten hard
gates (`gate.memory.read_api_*`) belong to a later task's
`tests/gates/test_memory_read_api_m17.py`; this file stays at the
happy-path / validation / authorization / failure level the sibling
schedule and notification API boundary tests (`test_schedule_api_m11.py`,
`test_notification_api_m12.py`) hold themselves to.
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    Sensitivity,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.gates.memory_api_support import memory_routes
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)
TENANT = "local"
PRINCIPAL_ID = "local-user"

# The spec's exact 26-field exposure list (memory-read-api-and-browser.md).
MEMORY_VIEW_FIELDS = {
    "id",
    "subject",
    "statement",
    "belief_type",
    "status",
    "polarity",
    "scope",
    "portability",
    "authority",
    "sensitivity",
    "confidence",
    "corroboration_count",
    "flagged_for_review",
    "conflicts_with",
    "superseded_by",
    "source_session_id",
    "source_event_ids",
    "formation_run_id",
    "consolidation_policy_version",
    "origin_scopes",
    "valid_from",
    "valid_to",
    "expires_at",
    "last_reinforced_at",
    "created_at",
    "updated_at",
}


def _belief(
    *,
    belief_id: int,
    position: int,
    tenant_id: str = TENANT,
    principal_id: str = PRINCIPAL_ID,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryRecord:
    return MemoryRecord(
        id=UUID(int=belief_id),
        tenant_id=tenant_id,
        principal_id=principal_id,
        scope="project-a",
        subject=f"subject-{belief_id}",
        statement=f"statement {belief_id}",
        source_session_id=UUID(int=900),
        source_event_ids=[1],
        confidence=0.9,
        sensitivity=sensitivity,
        valid_from=NOW,
        status=status,
        belief_type=BeliefType.PREFERENCE,
        polarity=Polarity.ASSERT,
        portability=Portability.PORTABLE,
        origin_scopes=["project-a"],
        corroboration_count=1,
        last_reinforced_at=NOW,
        formation_run_id=UUID(int=belief_id + 10_000),
        consolidation_policy_version="formation@1",
        authority=MemoryAuthority.USER,
        store_position=position,
        created_at=NOW,
        updated_at=NOW,
    )


def _crafted_cursor(position: int, identifier: UUID) -> str:
    """Hand-build a cursor's wire shape to test decode robustness against
    adversarial input, independent of `_encode_memory_cursor`'s internals."""

    payload = json.dumps({"p": position, "i": str(identifier)}, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


@asynccontextmanager
async def _client(composition: Composition, *, principal: Principal | None = None) -> Any:
    app = create_app(
        composition.services,
        composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def test_memory_routes_cover_listing_detail_scopes_and_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        roles={"user"},
        scopes=set(PLATFORM_SCOPES) | {"memory.read"},
    )
    settings = replace(memory_settings(), memory_api_enabled=True)
    async with build(
        settings=settings,
        storage="memory",
        sequential_ids=True,
        principal=principal,
    ) as composition:
        visible = _belief(belief_id=1, position=1, sensitivity=Sensitivity.INTERNAL)
        restricted = _belief(belief_id=2, position=2, sensitivity=Sensitivity.RESTRICTED)
        async with composition.uow_factory() as uow:
            await uow.memories.upsert_belief(visible)
            await uow.memories.upsert_belief(restricted)

        async with _client(composition) as client:
            missing_ceiling = await client.get("/v1/memories")
            assert missing_ceiling.status_code == 400
            missing_ceiling_error = missing_ceiling.json()["error"]
            assert missing_ceiling_error["code"] == "malformed_request"
            # Hard gate 1: the error names the offending parameter. `details`
            # stays the closed-vocabulary `{}` (http-api-and-streaming.md
            # rule 3); the parameter name lives in `message` instead (rule 2
            # makes `message` a log surface, not a typed contract).
            assert "ceiling" in missing_ceiling_error["message"]
            assert missing_ceiling_error["details"] == {}

            unknown_ceiling = await client.get("/v1/memories", params={"ceiling": "top-secret"})
            assert unknown_ceiling.status_code == 400
            assert "ceiling" in unknown_ceiling.json()["error"]["message"]

            listing = await client.get("/v1/memories", params={"ceiling": "internal"})
            assert listing.status_code == 200, listing.text
            body = listing.json()
            assert [item["id"] for item in body["items"]] == [str(visible.id)]
            assert set(body["items"][0]) == MEMORY_VIEW_FIELDS
            # A belief body is principal-scoped and sensitivity-bearing, so no
            # shared or on-disk cache may keep it. The artifact content route
            # carries the same header for the same reason.
            assert listing.headers["cache-control"] == "private, no-store"

            malformed_cursor = await client.get(
                "/v1/memories",
                params={"ceiling": "internal", "cursor": "not-a-real-cursor"},
            )
            assert malformed_cursor.status_code == 400

            # Keep the page-content assertions as end-to-end coverage of the
            # service cap, then spy on the service call below to distinguish
            # the route's own clamp from that service backstop.
            many = [
                _belief(
                    belief_id=100 + offset,
                    position=100 + offset,
                    sensitivity=Sensitivity.RESTRICTED,
                )
                for offset in range(201)
            ]
            async with composition.uow_factory() as uow:
                for belief in many:
                    await uow.memories.upsert_belief(belief)
            # `visible` (internal) and `restricted` are both within the
            # "restricted" ceiling too, so the full live set is 203 beliefs.
            all_ids = {str(visible.id), str(restricted.id)} | {str(b.id) for b in many}
            assert len(all_ids) == 203

            list_spy = AsyncMock(wraps=composition.services.memory.list)
            monkeypatch.setattr(composition.services.memory, "list", list_spy)
            oversized = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "limit": 300}
            )
            assert oversized.status_code == 200, oversized.text
            list_spy.assert_awaited_once()
            awaited = list_spy.await_args
            assert awaited is not None
            assert awaited.kwargs["limit"] == 200
            oversized_body = oversized.json()
            assert len(oversized_body["items"]) == 200
            assert oversized_body["next_cursor"] is not None

            remainder = await client.get(
                "/v1/memories",
                params={
                    "ceiling": "restricted",
                    "limit": 300,
                    "cursor": oversized_body["next_cursor"],
                },
            )
            assert remainder.status_code == 200, remainder.text
            remainder_body = remainder.json()
            assert len(remainder_body["items"]) == 3
            assert remainder_body["next_cursor"] is None

            paged_ids = {item["id"] for item in oversized_body["items"]} | {
                item["id"] for item in remainder_body["items"]
            }
            assert paged_ids == all_ids

            detail = await client.get(f"/v1/memories/{visible.id}", params={"ceiling": "internal"})
            assert detail.status_code == 200, detail.text
            assert set(detail.json()) == MEMORY_VIEW_FIELDS
            assert detail.json()["id"] == str(visible.id)
            assert detail.headers["cache-control"] == "private, no-store"

            above_ceiling = await client.get(
                f"/v1/memories/{restricted.id}", params={"ceiling": "internal"}
            )
            missing = await client.get(
                f"/v1/memories/{UUID(int=999)}", params={"ceiling": "restricted"}
            )
            assert above_ceiling.status_code == missing.status_code == 404
            assert (
                above_ceiling.json()["error"]["code"]
                == missing.json()["error"]["code"]
                == "not_found"
            )

        reader = principal.model_copy(update={"scopes": set()}, deep=True)
        async with _client(composition, principal=reader) as unauthorized:
            denied = await unauthorized.get("/v1/memories", params={"ceiling": "restricted"})
            assert denied.status_code == 403

        foreign = principal.model_copy(
            update={"tenant_id": "other", "principal_id": "other-user"}, deep=True
        )
        async with _client(composition, principal=foreign) as stranger:
            hidden = await stranger.get(
                f"/v1/memories/{visible.id}", params={"ceiling": "restricted"}
            )
            assert hidden.status_code == 404

        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        routes = memory_routes(app)
        assert len(routes) == 2
        assert {
            (method, (route.openapi_extra or {})["required_scope"])
            for route in routes
            for method in (route.methods or set())
        } == {("GET", "memory.read")}


async def test_memory_http_surface_is_absent_by_default() -> None:
    # Hard gate 7's other half: the scope stays recognized even with the
    # route surface off, so configuration validation never rejects it.
    assert "memory.read" in PLATFORM_SCOPES
    async with build(settings=memory_settings(), storage="memory") as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        assert memory_routes(app) == []
        assert "/v1/memories" not in app.openapi()["paths"]


async def test_memory_list_rejects_a_cursor_position_beyond_bigint_range() -> None:
    # store_position is a PostgreSQL BIGINT column; a crafted cursor that
    # decodes to a position outside int64 range must never reach the keyset
    # predicate. Left unvalidated, asyncpg raises DataError constructing the
    # query, which is not a ValueError and would otherwise escape the closed
    # error vocabulary as an internal_error/500.
    principal = Principal(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        roles={"user"},
        scopes=set(PLATFORM_SCOPES) | {"memory.read"},
    )
    settings = replace(memory_settings(), memory_api_enabled=True)
    async with build(
        settings=settings,
        storage="memory",
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.memories.upsert_belief(_belief(belief_id=1, position=1))

        async with _client(composition) as client:
            too_large = _crafted_cursor(2**63, UUID(int=1))
            response = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "cursor": too_large}
            )
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "malformed_request"

            negative = _crafted_cursor(-1, UUID(int=1))
            response = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "cursor": negative}
            )
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "malformed_request"

            boundary = _crafted_cursor(2**63 - 1, UUID(int=1))
            response = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "cursor": boundary}
            )
            assert response.status_code == 200, response.text


async def test_memory_list_non_cursor_value_error_is_not_mislabeled_as_a_bad_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `ValueError` can also come from converting a stored row into a
    # `MemoryRecord` (an unrecognized enum value, an unparseable UUID) — a
    # data-corruption failure with nothing to do with the cursor. Only the
    # cursor codec's own `MemoryCursorError` may be mapped to the cursor's
    # 400; any other `ValueError` must fall through to `internal_error`/500,
    # the correct outcome for genuine data corruption.
    principal = Principal(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        roles={"user"},
        scopes=set(PLATFORM_SCOPES) | {"memory.read"},
    )
    settings = replace(memory_settings(), memory_api_enabled=True)
    async with build(
        settings=settings,
        storage="memory",
        sequential_ids=True,
        principal=principal,
    ) as composition:

        async def broken_list(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise ValueError("boom")

        monkeypatch.setattr(composition.services.memory, "list", broken_list)

        async with _client(composition) as client:
            response = await client.get("/v1/memories", params={"ceiling": "restricted"})
            assert response.status_code == 500, response.text
            assert response.json()["error"]["code"] == "internal_error"


async def test_memory_list_pages_every_belief_exactly_once() -> None:
    principal = Principal(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        roles={"user"},
        scopes=set(PLATFORM_SCOPES) | {"memory.read"},
    )
    settings = replace(memory_settings(), memory_api_enabled=True)
    async with build(
        settings=settings,
        storage="memory",
        sequential_ids=True,
        principal=principal,
    ) as composition:
        beliefs = [_belief(belief_id=i, position=i) for i in range(1, 6)]
        async with composition.uow_factory() as uow:
            for belief in beliefs:
                await uow.memories.upsert_belief(belief)

        async with _client(composition) as client:
            first_page = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "limit": 2}
            )
            assert first_page.status_code == 200, first_page.text
            first_body = first_page.json()
            assert len(first_body["items"]) == 2
            assert first_body["next_cursor"] is not None

            second_page = await client.get(
                "/v1/memories",
                params={
                    "ceiling": "restricted",
                    "limit": 2,
                    "cursor": first_body["next_cursor"],
                },
            )
            assert second_page.status_code == 200, second_page.text
            second_body = second_page.json()
            assert len(second_body["items"]) == 2
            assert second_body["next_cursor"] is not None

            first_ids = {item["id"] for item in first_body["items"]}
            second_ids = {item["id"] for item in second_body["items"]}
            assert first_ids.isdisjoint(second_ids)

            third_page = await client.get(
                "/v1/memories",
                params={
                    "ceiling": "restricted",
                    "limit": 2,
                    "cursor": second_body["next_cursor"],
                },
            )
            assert third_page.status_code == 200, third_page.text
            third_body = third_page.json()
            assert len(third_body["items"]) == 1
            assert third_body["next_cursor"] is None

            third_ids = {item["id"] for item in third_body["items"]}
            walked_ids = first_ids | second_ids | third_ids
            assert walked_ids == {str(belief.id) for belief in beliefs}
            # Newest first by store_position, descending across pages too.
            assert [item["id"] for item in first_body["items"]] == [
                str(beliefs[4].id),
                str(beliefs[3].id),
            ]
            assert [item["id"] for item in second_body["items"]] == [
                str(beliefs[2].id),
                str(beliefs[1].id),
            ]
            assert [item["id"] for item in third_body["items"]] == [str(beliefs[0].id)]

            # Re-reading an earlier page against an unchanged store is stable.
            replay = await client.get("/v1/memories", params={"ceiling": "restricted", "limit": 2})
            assert replay.json() == first_body
