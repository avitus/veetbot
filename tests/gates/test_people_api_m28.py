"""People API admission, exact scopes, validation, and guarded writes."""

from dataclasses import replace
from typing import Any, Literal

import httpx

from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.config import Settings
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import principal, session
from tests.integration.m2_support import memory_settings


async def test_people_api_defaults_off() -> None:
    async with build(settings=memory_settings(), storage="memory") as app:
        schema = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        ).openapi()
        assert not any(path.startswith("/v1/people") for path in schema["paths"])


async def test_people_directory_filters_before_pagination_and_binds_cursor() -> None:
    from uuid import UUID

    from agent_core.domain.people import Person
    from tests.contract.support import NOW

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(
        settings=replace(memory_settings(), people_enabled=True), storage="memory", principal=owner
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            for index, state, pinned in [
                (1, "provisional", True),
                (2, "active", False),
                (3, "active", True),
                (4, "active", True),
            ]:
                await uow.people.put(
                    Person(
                        id=UUID(int=index),
                        display_name=f"Person {index}",
                        state="active" if state == "active" else "provisional",
                        pinned=pinned,
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    expected_revision=0,
                )
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            url = "/v1/people?ceiling=sensitive&state=active&pinned=true&limit=1"
            first = await client.get(url)
            assert first.status_code == 200
            assert [row["id"] for row in first.json()["items"]] == [str(UUID(int=3))]
            cursor = first.json()["next_cursor"]
            second = await client.get(
                url,
                params={
                    "cursor": cursor,
                    "ceiling": "sensitive",
                    "state": "active",
                    "pinned": "true",
                    "limit": 1,
                },
            )
            assert [row["id"] for row in second.json()["items"]] == [str(UUID(int=4))]
            changed = await client.get(
                "/v1/people",
                params={
                    "cursor": cursor,
                    "ceiling": "sensitive",
                    "state": "provisional",
                    "pinned": "true",
                    "limit": 1,
                },
            )
            assert changed.status_code == 409
            confirmed = await client.patch(
                f"/v1/people/{UUID(int=1)}?ceiling=sensitive",
                json={"session_id": str(session().id), "expected_revision": 1, "confirm": True},
                headers={"Idempotency-Key": "confirm-identity"},
            )
            assert confirmed.status_code == 200 and confirmed.json()["state"] == "active"


async def test_people_import_runs_selected_empty_history_without_advancing_automatic_cursor() -> (
    None
):
    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.people_imports import PeopleImportRequest

    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "session.read", "session.write"}}
    )
    async with build(
        settings=replace(memory_settings(), people_enabled=True),
        storage="memory",
        principal=owner,
        memory_people_evaluation_mode=True,
    ) as app:
        source = await app.services.sessions.create(owner, "general", {})
        service = app.services.people
        assert service is not None
        preview_request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": str(source.id),
                "scope": {
                    "session_ids": [str(source.id)],
                    "since": "2026-01-01T00:00:00Z",
                    "until": "2026-02-01T00:00:00Z",
                    "max_records": 10,
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await service.create_import(
            owner,
            preview_request,
            key="empty-preview",
            ceiling=Sensitivity.SENSITIVE,
        )
        result = await service.create_import(
            owner,
            preview_request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="empty-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        assert result.state == "completed"
        assert result.source_read_complete and result.analysis_complete
        assert result.records_read == result.records_processed == 0
        assert result.spent_usd == result.reserved_usd == 0


async def test_people_import_preserves_failed_source_and_records_provider_cost() -> None:
    from datetime import timedelta

    from agent_core.domain.events import NewEvent
    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.people_imports import PeopleImportRequest

    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "session.read", "session.write"}}
    )
    async with build(
        settings=replace(memory_settings(), people_enabled=True),
        storage="memory",
        principal=owner,
        memory_people_evaluation_mode=True,
        script=FakeModelScript(turns=[ScriptedTurn(text="{}") for _ in range(3)]),
    ) as app:
        source = await app.services.sessions.create(owner, "general", {})
        async with app.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=source.id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    payload={"content": "I prefer jasmine tea."},
                )
            )
        service = app.services.people
        assert service is not None
        preview_request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": str(source.id),
                "scope": {
                    "session_ids": [str(source.id)],
                    "since": (app.clock.now() - timedelta(days=1)).isoformat(),
                    "until": (app.clock.now() + timedelta(days=1)).isoformat(),
                    "max_records": 10,
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await service.create_import(
            owner,
            preview_request,
            key="empty-preview",
            ceiling=Sensitivity.SENSITIVE,
        )
        result = await service.create_import(
            owner,
            preview_request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="empty-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        assert result.state == "failed"
        assert not result.source_read_complete and not result.analysis_complete
        assert result.failures == 1 and result.error_code == "analysis_incomplete"
        assert result.records_processed == 0
        # The session-created event is read and excluded before the failed owner source.
        assert result.records_excluded == 1 and result.records_read == 2
        async with app.uow_factory() as uow:
            assert await uow.memories.consolidation_watermark(source.id, owner) == 0
            assert result.run_id is not None
            run = await uow.runs.get(result.run_id, owner)
            assert run.usage.model_calls == 3
        assert result.spent_usd == result.reserved_usd == 0


async def test_people_import_preview_is_scoped_idempotent_and_requires_a_finite_budget() -> None:
    settings = replace(memory_settings(), people_enabled=True)
    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "session.read"}}
    )
    async with build(settings=settings, storage="memory", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            body: dict[str, Any] = {
                "phase": "preview",
                "session_id": str(session().id),
                "scope": {
                    "session_ids": [str(session().id)],
                    "since": "2026-01-01T00:00:00Z",
                    "until": "2026-09-01T00:00:00Z",
                    "max_records": 100,
                    "max_cost_usd": "1.00",
                },
            }
            response = await client.post(
                "/v1/people/imports?ceiling=sensitive",
                json=body,
                headers={"Idempotency-Key": "preview-import"},
            )
            assert response.status_code == 200, response.text
            preview = response.json()
            assert preview["state"] == "preview" and preview["run_id"] is None
            assert preview["spent_usd"] == "0" and preview["reserved_usd"] == "0"
            assert not {"tenant_id", "principal_id", "request_hash"} & preview.keys()
            replay = await client.post(
                "/v1/people/imports?ceiling=sensitive",
                json=body,
                headers={"Idempotency-Key": "preview-import"},
            )
            assert replay.json() == preview
            invalid = {**body, "scope": {**body["scope"], "max_cost_usd": "NaN"}}
            assert (
                await client.post(
                    "/v1/people/imports?ceiling=sensitive",
                    json=invalid,
                    headers={"Idempotency-Key": "bad-import"},
                )
            ).status_code == 400
            status = await client.get(f"/v1/people/imports/{preview['id']}?ceiling=sensitive")
            assert status.json() == preview
            cancel_url = f"/v1/people/imports/{preview['id']}/cancel?ceiling=sensitive"
            cancel_body = {"expected_revision": preview["revision"]}
            assert (await client.post(cancel_url, json=cancel_body)).status_code == 400
            headers = {"Idempotency-Key": "cancel-import"}
            cancelled = await client.post(cancel_url, json=cancel_body, headers=headers)
            assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
            assert (
                await client.post(cancel_url, json=cancel_body, headers=headers)
            ).json() == cancelled.json()
            assert (
                await client.post(cancel_url, json={"expected_revision": 999}, headers=headers)
            ).status_code == 409


async def test_people_api_write_read_retry_and_validation() -> None:
    settings = replace(memory_settings(), people_enabled=True)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=settings, storage="memory", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            body: dict[str, Any] = {"session_id": str(session().id), "display_name": "Alex"}
            response = await client.post(
                "/v1/people?ceiling=sensitive", json=body, headers={"Idempotency-Key": "create-1"}
            )
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            data = response.json()
            assert (
                not {"tenant_id", "principal_id", "request_hash", "source_revision"} & data.keys()
            )
            replay = await client.post(
                "/v1/people?ceiling=sensitive", json=body, headers={"Idempotency-Key": "create-1"}
            )
            assert replay.json() == data
            for section in ("relationships", "history", "facts", "identity-evidence"):
                section_response = await client.get(
                    f"/v1/people/{data['id']}/{section}?ceiling=sensitive"
                )
                assert section_response.status_code == 200, section_response.text
                assert section_response.json()["items"] == []
            assert (
                await client.get(f"/v1/people/{data['id']}/history?ceiling=sensitive&limit=101")
            ).status_code == 400
            for invalid in (
                "known_at=2026-01-01T00:00:00",
                "since=2026-02-01T00:00:00Z&until=2026-01-01T00:00:00Z",
            ):
                invalid_response = await client.get(
                    f"/v1/people/{data['id']}/history?ceiling=sensitive&{invalid}"
                )
                assert invalid_response.status_code == 400
            page = await client.get("/v1/people?ceiling=sensitive&text=Alex")
            assert [row["id"] for row in page.json()["items"]] == [data["id"]]
            assert (
                await client.get(f"/v1/people/{data['id']}?ceiling=internal")
            ).status_code == 404
            assert (await client.get("/v1/people")).status_code == 400
            assert (await client.post("/v1/people?ceiling=sensitive", json=body)).status_code == 400
            assert (
                await client.post(
                    "/v1/people?ceiling=sensitive",
                    json=body | {"authority": "system"},
                    headers={"Idempotency-Key": "bad"},
                )
            ).status_code == 400


async def test_people_tools_advertised_only_when_enabled_with_exact_scope() -> None:
    for enabled in (False, True):
        async with build(
            settings=replace(memory_settings(), people_enabled=enabled), storage="memory"
        ) as app:
            for name in ("people.search", "people.context", "people.history"):
                if not enabled:
                    import pytest

                    from agent_core.domain.errors import NotFoundError

                    with pytest.raises(NotFoundError):
                        app.tool_pipeline._registry.get(name)
                else:
                    spec = app.tool_pipeline._registry.get(name).spec
                    assert spec.required_scopes == {"people.read"}
                    assert spec.maximum_output_bytes == 65536


async def test_people_identity_http_previews_apply_and_receipts() -> None:
    settings = replace(memory_settings(), people_enabled=True)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=settings, storage="memory", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            people = []
            for index, name in enumerate(("Al", "Alex")):
                response = await client.post(
                    "/v1/people?ceiling=sensitive",
                    json={"session_id": str(session().id), "display_name": name},
                    headers={"Idempotency-Key": f"create-{index}"},
                )
                people.append(response.json())
            request = {
                "session_id": str(session().id),
                "operation": "merge",
                "source_id": people[0]["id"],
                "target_id": people[1]["id"],
                "expected_revisions": {p["id"]: 1 for p in people},
            }
            preview = await client.post(
                "/v1/people/identity-operations?ceiling=sensitive",
                json=request,
                headers={"Idempotency-Key": "preview"},
            )
            assert preview.status_code == 200, preview.text
            assert (
                not {"tenant_id", "principal_id", "request_hash", "assignment_scope_hash"}
                & preview.json().keys()
            )
            applied = await client.post(
                "/v1/people/identity-operations?ceiling=sensitive",
                json={
                    "session_id": str(session().id),
                    "operation": "apply",
                    "operation_id": preview.json()["id"],
                    "expected_revision": 1,
                },
                headers={"Idempotency-Key": "apply"},
            )
            assert applied.status_code == 200 and applied.json()["state"] == "completed"
            receipt = await client.get(
                f"/v1/people/operations/{preview.json()['id']}?ceiling=sensitive"
            )
            assert receipt.json() == applied.json()


async def test_forget_http_receipt_survives_removed_person() -> None:
    settings = replace(memory_settings(), people_enabled=True)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=settings, storage="memory", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            created = await client.post(
                "/v1/people?ceiling=sensitive",
                json={"session_id": str(session().id), "display_name": "Sam"},
                headers={"Idempotency-Key": "sam"},
            )
            target = created.json()["id"]
            preview = await client.post(
                f"/v1/people/{target}/forget?ceiling=sensitive",
                json={"session_id": str(session().id), "phase": "preview", "expected_revision": 1},
                headers={"Idempotency-Key": "preview"},
            )
            assert preview.status_code == 200, preview.text
            applied = await client.post(
                f"/v1/people/{target}/forget?ceiling=sensitive",
                json={
                    "session_id": str(session().id),
                    "phase": "apply",
                    "expected_revision": 1,
                    "operation_id": preview.json()["id"],
                },
                headers={"Idempotency-Key": "apply"},
            )
            assert applied.status_code == 200 and applied.json()["state"] == "completed"
            assert (await client.get(f"/v1/people/{target}?ceiling=sensitive")).status_code == 404
            receipt = await client.get(
                f"/v1/people/operations/{preview.json()['id']}?ceiling=sensitive"
            )
            assert receipt.json() == applied.json()
            assert "Sam" not in receipt.text


async def test_import_discovery_is_owner_bound_bounded_and_cursor_bound() -> None:
    from datetime import timedelta
    from decimal import Decimal
    from uuid import UUID

    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.people import PeopleImportJob
    from agent_core.domain.people_imports import PeopleImportScope
    from tests.contract.support import NOW

    owner = principal().model_copy(update={"scopes": {"people.read"}})
    async with build(
        settings=replace(memory_settings(), people_enabled=True), storage="memory", principal=owner
    ) as app:
        scope = PeopleImportScope(
            session_ids=[session().id],
            since=NOW,
            until=NOW + timedelta(days=1),
            max_records=10,
            max_cost_usd=Decimal("1"),
        )
        async with app.uow_factory() as uow:
            for index in range(1, 5):
                await uow.people.put(
                    PeopleImportJob(
                        id=UUID(int=index),
                        tenant_id=owner.tenant_id,
                        principal_id="another-owner" if index == 1 else owner.principal_id,
                        sensitivity=Sensitivity.RESTRICTED if index == 2 else Sensitivity.SENSITIVE,
                        scope=scope,
                        audit_session_id=session().id,
                        created_at=NOW,
                        updated_at=NOW,
                        expires_at=NOW + timedelta(minutes=10),
                        request_hash="a" * 64,
                        implementation_sha256="b" * 64,
                    ),
                    expected_revision=0,
                )
        api = create_app(app.services, app.settings, owner, app.new_request_id, app.readiness_probe)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            first = await client.get(
                "/v1/people/imports", params={"ceiling": "sensitive", "limit": 1}
            )
            assert first.status_code == 200, first.text
            assert [row["id"] for row in first.json()["items"]] == [str(UUID(int=3))]
            assert first.headers["cache-control"] == "private, no-store"
            cursor = first.json()["next_cursor"]
            second = await client.get(
                "/v1/people/imports", params={"ceiling": "sensitive", "limit": 1, "cursor": cursor}
            )
            assert [row["id"] for row in second.json()["items"]] == [str(UUID(int=4))]
            rebound = await client.get(
                "/v1/people/imports", params={"ceiling": "internal", "limit": 1, "cursor": cursor}
            )
            assert rebound.status_code == 409
            malformed = await client.get(
                "/v1/people/imports", params={"ceiling": "sensitive", "limit": 101}
            )
            assert malformed.status_code == 400
            missing = await client.get("/v1/people/imports")
            assert missing.status_code == 400


async def test_relationship_directory_uses_live_permitted_owner_relationships_before_paging() -> (
    None
):
    await people_relationship_directory_api_contract("memory", memory_settings())


async def people_relationship_directory_api_contract(
    storage: Literal["memory", "postgres"], settings: Settings
) -> None:
    from uuid import UUID

    from agent_core.domain.memory import MemoryStatus, Sensitivity
    from agent_core.domain.people import PeopleEndpoint, Person, RelationshipAssertion
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW

    owner = principal().model_copy(update={"scopes": {"people.read"}})
    async with build(
        settings=replace(settings, people_enabled=True),
        storage=storage,
        principal=owner,
        fixed_clock_at=NOW,
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            for index in range(1, 6):
                person = Person(
                    id=UUID(int=index),
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    display_name=f"Person {index}",
                    created_at=NOW,
                    updated_at=NOW,
                )
                await uow.people.put(person, expected_revision=0)
                belief = memory().model_copy(
                    update={
                        "id": UUID(int=100 + index),
                        "store_position": await uow.memories.next_position(),
                        "sensitivity": Sensitivity.RESTRICTED
                        if index == 2
                        else Sensitivity.SENSITIVE,
                        "status": MemoryStatus.RETIRED if index == 3 else MemoryStatus.ACTIVE,
                    }
                )
                await uow.memories.upsert_belief(belief)
                await uow.people.put(
                    RelationshipAssertion(
                        id=UUID(int=200 + index),
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        created_at=NOW,
                        updated_at=NOW,
                        subject=PeopleEndpoint(kind="person", id=person.id),
                        object=PeopleEndpoint(kind="owner"),
                        predicate="friend" if index == 1 else "sibling",
                        belief_id=belief.id,
                    ),
                    expected_revision=0,
                )
        api = create_app(app.services, app.settings, owner, app.new_request_id, app.readiness_probe)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            params: dict[str, str | int] = {
                "ceiling": "sensitive",
                "limit": 1,
                "relationship": "family",
            }
            first = await client.get("/v1/people", params=params)
            assert first.status_code == 200, first.text
            assert [row["id"] for row in first.json()["items"]] == [str(UUID(int=4))]
            cursor = first.json()["next_cursor"]
            assert cursor is not None
            second = await client.get("/v1/people", params={**params, "cursor": cursor})
            assert [row["id"] for row in second.json()["items"]] == [str(UUID(int=5))]
            rebound = await client.get(
                "/v1/people", params={**params, "cursor": cursor, "relationship": "friend"}
            )
            assert rebound.status_code == 409
            invalid = await client.get("/v1/people", params={**params, "relationship": "unknown"})
            assert invalid.status_code == 400

        from datetime import timedelta

        from agent_core.adapters.determinism import FixedClock
        from agent_core.domain.people import PeopleQuery

        assert isinstance(app.clock, FixedClock)
        app.clock.advance(timedelta(days=1))
        async with app.uow_factory() as uow:
            prior = await uow.memories.get(UUID(int=104), owner)
            await uow.memories.reinforce(
                prior.model_copy(
                    update={
                        "valid_to": NOW,
                        "updated_at": app.clock.now(),
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
        historical = PeopleQuery(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            relationship="family",
            sensitivity_ceiling=Sensitivity.SENSITIVE,
            as_of=NOW,
            known_at=NOW,
        )
        async with app.uow_factory() as uow:
            assert UUID(int=4) in {row.id for row in await uow.people.query(historical)}
            current = await uow.memories.get(UUID(int=104), owner)
            await uow.memories.reinforce(
                current.model_copy(
                    update={
                        "sensitivity": Sensitivity.RESTRICTED,
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
        async with app.uow_factory() as uow:
            assert UUID(int=4) not in {row.id for row in await uow.people.query(historical)}


async def test_identity_evidence_lists_claim_assignments_and_mentions_before_paging() -> None:
    from uuid import UUID, uuid4

    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.people import PeopleSource, Person, PersonMemoryLink, PersonMention
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW

    owner = principal().model_copy(update={"scopes": {"people.read"}})
    async with build(
        settings=replace(memory_settings(), people_enabled=True), storage="memory", principal=owner
    ) as app:
        fields: PeopleFields = {
            "tenant_id": owner.tenant_id,
            "principal_id": owner.principal_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
        person = Person(id=uuid4(), display_name="Alex", **fields)
        source = PeopleSource(
            id=uuid4(),
            source_kind="owner",
            session_id=session().id,
            event_sequence=1,
            evidence_at=NOW,
            source_revision="test",
            **fields,
        )
        hidden = memory().model_copy(update={"id": uuid4(), "sensitivity": Sensitivity.RESTRICTED})
        visible = memory().model_copy(update={"id": uuid4(), "statement": "Alex enjoys cycling."})
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(source, expected_revision=0)
            for index, belief in enumerate([hidden, visible], 1):
                await uow.memories.upsert_belief(belief)
                await uow.people.put(
                    PersonMemoryLink(
                        id=UUID(int=index),
                        person_id=person.id,
                        belief_id=belief.id,
                        support_ids=[source.id],
                        **fields,
                    ),
                    expected_revision=0,
                )
            await uow.people.put(
                PersonMention(
                    id=UUID(int=3),
                    person_id=person.id,
                    source_id=source.id,
                    start=0,
                    end=4,
                    support_ids=[source.id],
                    **fields,
                ),
                expected_revision=0,
            )
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            url = f"/v1/people/{person.id}/identity-evidence"
            query: dict[str, Any] = {"ceiling": "sensitive", "limit": 1}
            first = await client.get(url, params=query)
            assert first.status_code == 200, first.text
            assert first.headers["cache-control"] == "private, no-store"
            row = first.json()["items"][0]
            assert row["id"] == str(UUID(int=2)) and row["kind"] == "memory_link"
            assert row["label"] == visible.statement
            assert row["belief_id"] == str(visible.id)
            assert not {"tenant_id", "principal_id", "request_hash"} & row.keys()
            assert (await client.get(url, params=query)).json() == first.json()
            cursor = first.json()["next_cursor"]
            second = await client.get(url, params=query | {"cursor": cursor})
            assert second.status_code == 200
            assert second.json()["items"][0]["kind"] == "mention"
            assert second.json()["items"][0]["support_ids"] == [str(source.id)]
            assert second.json()["next_cursor"] is None
            assert (
                await client.get(url, params=query | {"cursor": cursor, "limit": 2})
            ).status_code == 409
            for bad in ({"limit": 101}, {"limit": 0}, {"cursor": "x" * 2049}):
                assert (await client.get(url, params=query | bad)).status_code == 400
            assert (await client.get(url)).status_code == 400
            assert (await client.get(url, params={"ceiling": "internal"})).status_code == 404
            assert (
                await client.get(f"/v1/people/{uuid4()}/identity-evidence", params=query)
            ).status_code == 404
        denied = create_app(
            app.services,
            app.settings,
            owner.model_copy(update={"scopes": set()}),
            app.new_request_id,
            app.readiness_probe,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=denied, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            assert (await client.get(url, params=query)).status_code == 403
