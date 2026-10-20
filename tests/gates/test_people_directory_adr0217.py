"""ADR-0217: last contact, owed threads and section counts in the People directory.

Every response that shows a person carries `last_interaction_at` (the key
`sort=recent` orders by) and `next_thread` (the earliest open reply the owner
owes them that is overdue or due within fourteen days). `open_threads=true`
lists those people, earliest due first, and `GET /v1/people/counts` returns
each sidebar number under its own list's predicates.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from itertools import count
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx

from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.memory import MemoryStatus
from agent_core.domain.people import (
    InteractionParticipant,
    PeopleCommitment,
    PeopleEndpoint,
    PeopleInteraction,
)
from tests.contract.memory_fixtures import memory
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import memory_settings

_POSITIONS = count(100)
OWNER = principal().model_copy(update={"scopes": {"people.read", "people.write", "session.read"}})


@asynccontextmanager
async def _api() -> AsyncIterator[tuple[httpx.AsyncClient, Composition]]:
    async with build(
        settings=replace(memory_settings(), people_enabled=True),
        storage="memory",
        principal=OWNER,
        fixed_clock_at=NOW,
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            yield client, app


async def _person(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    created = await client.post(
        "/v1/people?ceiling=sensitive",
        json={"session_id": str(session().id), "display_name": name},
        headers={"Idempotency-Key": f"create-{name}"},
    )
    assert created.status_code == 200, created.text
    body: dict[str, Any] = created.json()
    return body


def _common() -> PeopleFields:
    return {
        "tenant_id": OWNER.tenant_id,
        "principal_id": OWNER.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }


async def _met(app: Composition, person: dict[str, Any], days_ago: int) -> None:
    async with app.uow_factory() as uow:
        await uow.people.put(
            PeopleInteraction(
                id=uuid4(),
                channel="chat",
                interaction_kind="meeting",
                direction="reported",
                attribution="owner_reported",
                summary="Met for coffee",
                participants=[
                    InteractionParticipant(person_id=UUID(person["id"]), role="participant")
                ],
                occurred_at=NOW - timedelta(days=days_ago),
                **_common(),
            ),
            expected_revision=0,
        )


async def _owed(
    app: Composition,
    person: dict[str, Any],
    *,
    due_in_days: int | None = 3,
    state: Literal["proposed", "open", "completed", "cancelled", "uncertain"] = "open",
    owner_owes: bool = True,
    precision: Literal["instant", "day", "month", "year", "unknown"] = "day",
    description: str = "Reply about the lease",
    belief_status: MemoryStatus = MemoryStatus.ACTIVE,
) -> PeopleCommitment:
    belief = memory(belief_id=next(_POSITIONS), statement=f"I promised: {description}")
    belief = belief.model_copy(
        update={
            "store_position": belief.id.int,
            "status": belief_status,
            "valid_to": None if belief_status is MemoryStatus.ACTIVE else NOW,
        }
    )
    them = PeopleEndpoint(kind="person", id=UUID(person["id"]))
    me = PeopleEndpoint(kind="owner")
    commitment = PeopleCommitment(
        id=uuid4(),
        debtor=me if owner_owes else them,
        beneficiary=them if owner_owes else me,
        description=description,
        state=state,
        belief_id=belief.id,
        due_at=None if due_in_days is None else NOW + timedelta(days=due_in_days),
        due_precision=precision,
        support_ids=[UUID(item) for item in person["support_ids"]],
        state_source_id=UUID(person["support_ids"][0]),
        **_common(),
    )
    async with app.uow_factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(commitment, expected_revision=0)
    return commitment


async def test_every_person_response_carries_last_contact_and_the_next_thread() -> None:
    async with _api() as (client, app):
        maya = await _person(client, "Maya")
        assert maya["last_interaction_at"] is None
        assert maya["next_thread"] is None
        riv = await _person(client, "Riv")
        await _met(app, maya, days_ago=3)
        await _met(app, maya, days_ago=10)
        thread = await _owed(app, maya, due_in_days=2)

        for sort in ("id", "recent"):
            listed = await client.get(f"/v1/people?ceiling=sensitive&sort={sort}")
            assert listed.status_code == 200, listed.text
            rows = {row["id"]: row for row in listed.json()["items"]}
            assert rows[maya["id"]]["last_interaction_at"] == (
                (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z")
            )
            assert rows[riv["id"]]["last_interaction_at"] is None
            assert rows[maya["id"]]["next_thread"]["id"] == str(thread.id)
            assert rows[riv["id"]]["next_thread"] is None

        profile = await client.get(f"/v1/people/{maya['id']}?ceiling=sensitive")
        assert profile.status_code == 200, profile.text
        assert profile.json()["person"]["last_interaction_at"] is not None
        assert profile.json()["person"]["next_thread"]["description"] == "Reply about the lease"

        updated = await client.patch(
            f"/v1/people/{maya['id']}?ceiling=sensitive",
            json={"session_id": str(session().id), "expected_revision": 1, "pinned": True},
            headers={"Idempotency-Key": "pin-maya"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["last_interaction_at"] is not None


async def test_only_an_open_reply_the_owner_owes_soon_is_a_thread() -> None:
    async with _api() as (client, app):
        due = await _person(client, "Due soon")
        overdue = await _person(client, "Overdue")
        excluded = {
            "proposed": await _person(client, "Proposed"),
            "uncertain": await _person(client, "Uncertain"),
            "they owe": await _person(client, "They owe"),
            "far off": await _person(client, "Far off"),
            "vague date": await _person(client, "Vague date"),
            "no date": await _person(client, "No date"),
            "done": await _person(client, "Done"),
        }
        await _owed(app, due, due_in_days=5)
        await _owed(app, due, due_in_days=9, description="Send the photos")
        await _owed(app, overdue, due_in_days=-2)
        await _owed(app, excluded["proposed"], state="proposed")
        await _owed(app, excluded["uncertain"], state="uncertain")
        await _owed(app, excluded["they owe"], owner_owes=False)
        await _owed(app, excluded["far off"], due_in_days=20)
        await _owed(app, excluded["vague date"], precision="month")
        await _owed(app, excluded["no date"], due_in_days=None)
        await _owed(app, excluded["done"], state="completed")

        listed = await client.get("/v1/people?ceiling=sensitive&open_threads=true")
        assert listed.status_code == 200, listed.text
        rows = listed.json()["items"]
        # Earliest due first, and each row carries the earliest of its threads.
        assert [row["id"] for row in rows] == [overdue["id"], due["id"]]
        assert rows[1]["next_thread"]["description"] == "Reply about the lease"

        first = await client.get("/v1/people?ceiling=sensitive&open_threads=true&limit=1")
        assert [row["id"] for row in first.json()["items"]] == [overdue["id"]]
        second = await client.get(
            "/v1/people",
            params={
                "ceiling": "sensitive",
                "open_threads": "true",
                "limit": 1,
                "cursor": first.json()["next_cursor"],
            },
        )
        assert [row["id"] for row in second.json()["items"]] == [due["id"]]
        assert second.json()["next_cursor"] is None

        combined = await client.get("/v1/people?ceiling=sensitive&open_threads=true&review=true")
        assert combined.status_code == 422


async def test_a_thread_is_shown_only_as_the_persons_open_threads_show_it() -> None:
    async with _api() as (client, app):
        retired = await _person(client, "Retired memory")
        await _owed(app, retired, belief_status=MemoryStatus.RETIRED)
        listed = await client.get("/v1/people?ceiling=sensitive&open_threads=true")
        assert listed.json()["items"] == []
        # The commitment and the person are sensitive by default, so a lower
        # ceiling shows neither.
        shown = await _person(client, "Shown")
        await _owed(app, shown)
        internal = await client.get("/v1/people?ceiling=internal&open_threads=true")
        assert internal.json()["items"] == []
        sensitive = await client.get("/v1/people?ceiling=sensitive&open_threads=true")
        assert [row["id"] for row in sensitive.json()["items"]] == [shown["id"]]


async def test_counts_match_the_lists_they_label() -> None:
    async with _api() as (client, app):
        maya = await _person(client, "Maya")
        await _person(client, "Riv")
        await _owed(app, maya)
        async with app.uow_factory() as uow:
            from agent_core.domain.people import Person

            await uow.people.put(
                Person(
                    id=uuid4(),
                    display_name="Someone from email",
                    state="provisional",
                    **_common(),
                ),
                expected_revision=0,
            )

        async def listed(**params: Any) -> int:
            response = await client.get(
                "/v1/people", params={"ceiling": "sensitive", "limit": 100, **params}
            )
            assert response.status_code == 200, response.text
            return len(response.json()["items"])

        counted = await client.get("/v1/people/counts?ceiling=sensitive")
        assert counted.status_code == 200, counted.text
        assert counted.headers["cache-control"] == "private, no-store"
        counts = counted.json()
        assert counts["everyone"] == await listed(state=["active", "provisional"]) == 3
        assert counts["needs_review"] == await listed(review="true") == 1
        assert counts["open_threads"] == await listed(open_threads="true") == 1
        # Maya is owed a reply; the email correspondent waits for review.
        assert counts["needs_you"] == 2
        duplicates = await client.get("/v1/people/merge-suggestions?ceiling=sensitive&limit=100")
        assert counts["possible_duplicates"] == len(duplicates.json()["items"])
        assert counts["counted_at"]

        # Everyone here is sensitive, so a lower ceiling counts no one.
        internal = (await client.get("/v1/people/counts?ceiling=internal")).json()
        assert internal == {
            **internal,
            "everyone": 0,
            "needs_review": 0,
            "open_threads": 0,
            "needs_you": 0,
        }


async def test_counts_need_people_read_and_are_not_a_person_id() -> None:
    async with _api() as (client, app):
        missing = await client.get("/v1/people/counts")
        assert missing.status_code in {400, 422}
        writer = OWNER.model_copy(update={"scopes": {"people.write"}})
        api = create_app(
            app.services, app.settings, writer, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as limited:
            denied = await limited.get("/v1/people/counts?ceiling=sensitive")
            assert denied.status_code == 403
        flattened = [
            nested
            for route in api.routes
            for nested in (
                route.original_router.routes if hasattr(route, "original_router") else (route,)
            )
        ]
        paths = [getattr(route, "path", "") for route in flattened]
        assert paths.index("/v1/people/counts") < paths.index("/v1/people/{person_id}")
