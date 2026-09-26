"""Maintenance merges decisive duplicates; the owner answers the rest (ADR-0125)."""

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx

from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.domain.people import Person, PersonIdentifier
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import memory_settings


async def _seed(app: Any, owner: Any) -> dict[str, Person]:
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW - timedelta(days=10),
        "updated_at": NOW - timedelta(days=10),
    }
    people: dict[str, Person] = {}
    async with app.uow_factory() as uow:
        await uow.sessions.create(session())
        for label, name, state, address, verification in (
            ("erin", "Erin", "active", "erin@home.test", "owner_confirmed"),
            ("written", "Erin Vitus", "provisional", "erin@home.test", "channel_observed"),
            ("sabina", "Sabina Smith", "provisional", "sabina@home.test", "channel_observed"),
            ("work", "Sabina Smith", "provisional", "sabina@work.test", "channel_observed"),
        ):
            person = Person(id=uuid4(), display_name=name, state=state, **common)  # type: ignore[arg-type]
            people[label] = person
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="email",
                    namespace="owner",
                    value=address,
                    context="owner",
                    verification=verification,  # type: ignore[arg-type]
                    valid_from=NOW - timedelta(days=10),
                    **common,
                ),
                expected_revision=0,
            )
    return people


async def test_maintenance_merges_decisive_duplicates_and_the_owner_answers_the_rest() -> None:
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(
        settings=replace(memory_settings(), people_enabled=True), storage="memory", principal=owner
    ) as app:
        people = await _seed(app, owner)
        worker = app.maintenance_factory()
        assert isinstance(worker, MaintenanceWorker)
        await worker.run_once()
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            listed = await client.get("/v1/people/merge-suggestions?ceiling=sensitive")
            assert listed.status_code == 200, listed.text
            assert listed.headers["cache-control"] == "private, no-store"
            [suggestion] = listed.json()["items"]
            assert suggestion["reason"] == "same_name" and suggestion["state"] == "open"
            assert {suggestion["source"]["id"], suggestion["target"]["id"]} == {
                str(people["sabina"].id),
                str(people["work"].id),
            }
            erin = (await client.get(f"/v1/people/{people['erin'].id}?ceiling=sensitive")).json()
            [automatic] = erin["automatic_merges"]
            assert automatic["merged"]["display_name"] == "Erin Vitus"
            sabina = (
                await client.get(f"/v1/people/{people['sabina'].id}?ceiling=sensitive")
            ).json()
            assert [row["id"] for row in sabina["merge_suggestions"]] == [suggestion["id"]]

            url = f"/v1/people/merge-suggestions/{suggestion['id']}?ceiling=sensitive"
            body = {
                "session_id": str(session().id),
                "expected_revision": suggestion["revision"],
                "decision": "merge",
            }
            assert (await client.post(url, json=body)).status_code == 400
            invalid = await client.post(
                url, json=body | {"decision": "maybe"}, headers={"Idempotency-Key": "bad"}
            )
            assert invalid.status_code == 400
            resolved = await client.post(url, json=body, headers={"Idempotency-Key": "merge"})
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["state"] == "merged"
            again = await client.post(url, json=body, headers={"Idempotency-Key": "merge"})
            assert again.json() == resolved.json()
            stale = await client.post(url, json=body, headers={"Idempotency-Key": "stale"})
            assert stale.status_code == 409
            assert (await client.get("/v1/people/merge-suggestions?ceiling=sensitive")).json()[
                "items"
            ] == []


async def test_merge_suggestions_need_people_scopes() -> None:
    reader = principal().model_copy(update={"scopes": {"people.read"}})
    async with build(
        settings=replace(memory_settings(), people_enabled=True), storage="memory", principal=reader
    ) as app:
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            assert (
                await client.get("/v1/people/merge-suggestions?ceiling=sensitive")
            ).status_code == 200
            refused = await client.post(
                f"/v1/people/merge-suggestions/{uuid4()}?ceiling=sensitive",
                json={
                    "session_id": str(session().id),
                    "expected_revision": 1,
                    "decision": "separate",
                },
                headers={"Idempotency-Key": "refused"},
            )
            assert refused.status_code == 403
