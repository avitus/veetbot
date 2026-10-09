"""Derived memories use owner controls without impersonating original beliefs."""

from dataclasses import replace
from typing import Any, cast

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.reconsolidation_operations import StoredSummary
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_summary_cases import committed_summary
from tests.contract.reconsolidation_surface_cases import owner
from tests.contract.support import NOW, session
from tests.gates.test_memory_api_boundary_m17 import _client
from tests.integration.m2_support import memory_settings


@pytest.mark.parametrize("kind", ["summary", "hypothesis"])
@pytest.mark.parametrize("action", ["browse", "detail", "dismiss", "delete"])
async def test_derived_memory_owner_boundary(action: str, kind: str) -> None:
    settings = replace(
        memory_settings(), memory_api_enabled=True, memory_reconsolidation_api_enabled=True
    )
    async with build(
        settings=settings, storage="memory", principal=owner(), clock=FixedClock(NOW)
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        operation = await derived_operation(cast(Factory, app.uow_factory), kind, omitted=True)
        path = f"/v1/memories/{operation.id}"
        async with _client(app) as client:
            if action == "browse":
                response = await client.get(
                    "/v1/memories", params={"ceiling": "internal", "include_derived": True}
                )
                assert response.status_code == 200, response.text
                derived = [r for r in response.json()["items"] if r["id"] == str(operation.id)]
                assert len(derived) == 1, "opt-in browsing must include the valid summary"
                assert derived[0]["record_kind"] == kind
            elif action == "detail":
                response = await client.get(path, params={"ceiling": "internal"})
                assert response.status_code == 200, response.text
                assert response.json()["operation_id"] == str(operation.id)
                assert len(response.json()["sources"]) == 2
                assert response.json()["sources"][1]["omitted"] is (kind == "summary")
                assert response.headers["cache-control"] == "private, no-store"
            elif action == "dismiss":
                response = await client.post(
                    path + "/review",
                    params={"ceiling": "internal"},
                    headers={"Idempotency-Key": "review"},
                    json={"outcome": "dismiss"},
                )
                assert response.status_code == 200, response.text
                assert response.json()["flagged_for_review"] is False
                assert response.json()["revision"] == 2
            else:
                for _ in range(2):
                    response = await client.delete(
                        path, params={"ceiling": "internal"}, headers={"Idempotency-Key": "delete"}
                    )
                    assert response.status_code == 204, response.text
                assert (await client.get(path, params={"ceiling": "restricted"})).status_code == 404
        async with app.uow_factory() as uow:
            for key in operation.plan.member_ids:
                assert (await uow.memories.get(key, owner())).id == key


@pytest.mark.parametrize("kind", ["summary", "hypothesis"])
async def test_derived_boundary_validation_isolation_and_replay(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from unittest.mock import AsyncMock

    from tests.gates.test_memory_api_boundary_m17 import MEMORY_VIEW_FIELDS

    settings = replace(
        memory_settings(), memory_api_enabled=True, memory_reconsolidation_api_enabled=True
    )
    async with build(
        settings=settings, storage="memory", principal=owner(), clock=FixedClock(NOW)
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        operation = await derived_operation(cast(Factory, app.uow_factory), kind)
        path = f"/v1/memories/{operation.id}"
        async with _client(app) as client:
            ordinary = await client.get("/v1/memories", params={"ceiling": "internal"})
            assert len(ordinary.json()["items"]) == (2 if kind == "summary" else 4)
            assert all(set(row) == MEMORY_VIEW_FIELDS for row in ordinary.json()["items"])
            for params in (
                {},
                {"ceiling": "bogus"},
                {"ceiling": "internal", "include_derived": "bad"},
                {"ceiling": "internal", "include_derived": True, "cursor": "bad"},
            ):
                response = await client.get("/v1/memories", params=params)
                assert (
                    response.status_code == 400
                    and response.json()["error"]["code"] == "malformed_request"
                )
            for body, headers in (
                ({}, {"Idempotency-Key": "x"}),
                ({"outcome": "wrong"}, {"Idempotency-Key": "x"}),
                ({"outcome": "dismiss", "statement": "fabrication"}, {"Idempotency-Key": "x"}),
                ({"outcome": "dismiss"}, {}),
                ({"outcome": "dismiss"}, {"Idempotency-Key": "x" * 201}),
            ):
                response = await client.post(
                    path + "/review", params={"ceiling": "internal"}, json=body, headers=headers
                )
                assert response.status_code == 400, response.text
            for method, suffix, request_body in (
                ("GET", "", None),
                ("POST", "/review", {"outcome": "dismiss"}),
                ("DELETE", "", None),
            ):
                response = await client.request(
                    method,
                    path + suffix,
                    params={"ceiling": "public"},
                    json=request_body,
                    headers={"Idempotency-Key": "hidden"},
                )
                assert (
                    response.status_code == 404 and response.json()["error"]["code"] == "not_found"
                )
                denied = await client.request(
                    method, path + suffix, headers={"Forwarded": "for=untrusted"}
                )
                assert denied.status_code == 401
            before = await client.get(path, params={"ceiling": "internal"})
            async with app.uow_factory() as uow:
                store = uow.reconsolidation
            with monkeypatch.context() as patch:
                patch.setattr(
                    store,
                    "record_summary_write",
                    AsyncMock(side_effect=RuntimeError("receipt storage failed")),
                )
                failed = await client.delete(
                    path, params={"ceiling": "internal"}, headers={"Idempotency-Key": "retry"}
                )
                assert failed.status_code == 500 and "receipt storage failed" not in failed.text
            assert (await client.get(path, params={"ceiling": "internal"})).json() == before.json()
            rejected = await client.post(
                path + "/review",
                params={"ceiling": "internal"},
                json={"outcome": "untrue"},
                headers={"Idempotency-Key": "untrue"},
            )
            assert rejected.status_code == 200 and rejected.json()["content"] is None
            async with app.uow_factory() as uow:
                await uow.memories.fence_for_erasure(owner(), [operation.plan.member_ids[0]])
            hidden_replay = await client.post(
                path + "/review",
                params={"ceiling": "internal"},
                json={"outcome": "untrue"},
                headers={"Idempotency-Key": "untrue"},
            )
            assert hidden_replay.status_code == 404
            redacted = await client.post(
                path + "/review",
                params={"ceiling": "restricted"},
                json={"outcome": "untrue"},
                headers={"Idempotency-Key": "untrue"},
            )
            assert redacted.status_code == 200 and redacted.json()["sources"] == []
            assert redacted.headers["cache-control"] == "private, no-store"
            conflict = await client.post(
                path + "/review",
                params={"ceiling": "restricted"},
                json={"outcome": "dismiss"},
                headers={"Idempotency-Key": "untrue"},
            )
            assert conflict.status_code == 409
        for scopes in (set(), {"memory.read"}):
            async with _client(
                app, principal=owner().model_copy(update={"scopes": scopes})
            ) as client:
                assert (
                    await client.delete(
                        path, params={"ceiling": "restricted"}, headers={"Idempotency-Key": "x"}
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        path + "/review",
                        params={"ceiling": "restricted"},
                        json={"outcome": "dismiss"},
                        headers={"Idempotency-Key": "x"},
                    )
                ).status_code == 403
        for foreign in (
            owner().model_copy(update={"principal_id": "other"}),
            owner().model_copy(update={"tenant_id": "other"}),
        ):
            async with _client(app, principal=foreign) as client:
                assert (await client.get(path, params={"ceiling": "restricted"})).status_code == 404
                assert (
                    await client.delete(
                        path,
                        params={"ceiling": "restricted"},
                        headers={"Idempotency-Key": "untrue"},
                    )
                ).status_code == 404
                assert (
                    await client.post(
                        path + "/review",
                        params={"ceiling": "restricted"},
                        json={"outcome": "untrue"},
                        headers={"Idempotency-Key": "untrue"},
                    )
                ).status_code == 404


async def test_derived_surface_remains_unavailable_when_flag_disabled() -> None:
    settings = replace(memory_settings(), memory_api_enabled=True)
    async with build(
        settings=settings, storage="memory", principal=owner(), clock=FixedClock(NOW)
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        operation = await committed_summary(cast(Factory, app.uow_factory))
        async with _client(app) as client:
            result = await client.get(
                "/v1/memories", params={"ceiling": "internal", "include_derived": True}
            )
            assert (
                result.status_code == 400 and result.json()["error"]["code"] == "malformed_request"
            )
            assert (
                await client.get(f"/v1/memories/{operation.id}", params={"ceiling": "restricted"})
            ).status_code == 404
            assert (
                await client.get("/v1/memories", params={"ceiling": "internal"})
            ).status_code == 200


async def derived_operation(factory: Factory, kind: str, *, omitted: bool = False) -> StoredSummary:
    if kind == "summary":
        return await committed_summary(factory, omitted=omitted)
    from agent_core.memory.reconsolidation_apply import apply_review
    from tests.contract.reconsolidation_apply_cases import prepared_batch
    from tests.contract.support import principal

    def connection(operations: list[dict[str, Any]]) -> None:
        del operations[1:]
        operations[0]["clauses"][0]["text"] = "User may prefer concise explanations."

    prepared, review = await prepared_batch(
        factory, kind="infer_connection", independent=True, change=connection
    )
    result = await apply_review(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    async with factory() as uow:
        operation = await uow.reconsolidation.summary_operation(
            principal(), result[0].operation_ids[0]
        )
        assert operation is not None
        return operation
