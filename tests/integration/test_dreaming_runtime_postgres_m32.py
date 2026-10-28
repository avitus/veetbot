"""Supervised dreaming end to end on PostgreSQL with owner review over HTTP.

Covers `docs/plan/memory-reconsolidation.md` "Owner-reviewed production mode
(ADR-0174)" and the owner-control routes ("Read paths, lifecycle and owner
surfaces"): a real `ReconsolidationPass` proposes a merge over PostgreSQL, the
pending proposal stays out of recall, the owner approves it over
`POST /v1/dreaming/{id}/decision`, recall then hides the suppressed original,
`POST /v1/memory-reconsolidations/{id}/undo` restores both originals and blocks
the pair, and the next day's pass does not re-propose it.

Guards the shipped behavior behind the dreaming repairs: isolating refused
sources while resuming inventory, fitting complete evidence and the verifier's
output to the remaining slice budget and model context, and recovering verified
legacy attribution before a cutoff retires it. Each repair changed what reaches
owner review; this journey asserts the persisted review, undo, spend and recall
outcomes those paths feed.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import (
    ReconsolidationOperationHistoryRow,
    ReconsolidationSpendRow,
)
from agent_core.api.app import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.memory.reconsolidation_worker import ReconsolidationPass
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.reconsolidation_admission_cases import model, policy
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.reconsolidation_execution_cases import TrackedFactory
from tests.contract.reconsolidation_selection_cases import MergeProvider
from tests.contract.reconsolidation_source_cases import seed_sources
from tests.contract.reconsolidation_surface_cases import owner
from tests.contract.support import NOW, SESSION_ID, TENANT, session
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

ORIGINALS = frozenset({UUID(int=501), UUID(int=502)})
# The same claim under `normalize_claim` (whitespace folds) but a different verbatim
# statement, so recall shows both originals until a merge suppresses one.
RESTATEMENT = "User prefers  concise answers"


@asynccontextmanager
async def _client(composition: Composition, *, principal: Principal | None = None) -> Any:
    """Yield an in-process HTTP client for the supplied composition."""

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


async def _recalled(composition: Composition) -> list[UUID]:
    result = await composition.memory_retriever.recall(recall_query(), session_id=SESSION_ID)
    return [item.belief_id for item in result.items]


@asynccontextmanager
async def _tables(database_url: str) -> AsyncIterator[Any]:
    """Read the owner's persisted rows directly, outside the application's unit of work."""

    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": TENANT},
            )
            yield connection
    finally:
        await engine.dispose()


async def test_dreaming_pass_review_undo_and_recall_on_postgres() -> None:
    clock = FixedClock(NOW)
    settings = replace(
        database_settings(),
        memory_api_enabled=True,
        memory_reconsolidation_api_enabled=True,
        memory_dreaming_review_enabled=True,
    )
    async with build(settings=settings, storage="postgres", principal=owner(), clock=clock) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            await seed_sources(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
            second = await uow.memories.get(UUID(int=502), app.principal)
            await uow.memories.reinforce(second.model_copy(update={"statement": RESTATEMENT}))
            originals = {
                belief_id: await uow.memories.get(belief_id, app.principal)
                for belief_id in ORIGINALS
            }
        assert set(await _recalled(app)) == ORIGINALS, "both originals recall before dreaming"

        tracked = TrackedFactory(cast(Factory, app.uow_factory))
        provider = MergeProvider(tracked)
        worker = ReconsolidationPass(
            cast(UnitOfWorkFactory, tracked),
            clock,
            app.principal,
            worker_id="e2e-dreaming",
            admitted=lambda: True,
            ids=RandomIdFactory(),
            model=model(),
            provider=provider,
            egress_policy=policy,
            owner_review=True,
        )
        await worker.run_once()
        assert len(provider.requests) == 2, "a proposal and its verification reach the provider"

        async with _tables(settings.database_url) as connection:
            spend_ids = list(
                (await connection.execute(select(ReconsolidationSpendRow.id))).scalars()
            )
        assert len(spend_ids) == 2
        async with app.uow_factory() as uow:
            for spend_id in spend_ids:
                spend = await uow.reconsolidation.get_spend(app.principal, spend_id)
                assert spend.state == "settled" and spend.charged_usd == Decimal("0.0002")

        async with _client(app) as client:
            status = await client.get("/v1/dreaming")
            assert status.status_code == 200, status.text
            [run] = status.json()["runs"]
            assert (run["outcome"], run["proposals"], run["provider_calls"]) == ("proposed", 1, 2)
            assert Decimal(run["charged_usd"]) == Decimal("0.0004")

            listing = await client.get("/v1/memory-reconsolidations?ceiling=restricted")
            assert listing.status_code == 200, listing.text
            [proposal] = listing.json()["items"]
            assert (proposal["kind"], proposal["state"]) == ("merge", "proposed")
            assert {UUID(s["belief_id"]) for s in proposal["sources"]} == ORIGINALS
            key = proposal["id"]
            assert set(await _recalled(app)) == ORIGINALS, "a pending proposal never enters recall"

            path = f"/v1/dreaming/{key}/decision?ceiling=restricted"
            body = {"decision": "approved", "expected_revision": proposal["revision"]}
            headers = {"Idempotency-Key": "approve"}
            approved = await client.post(path, json=body, headers=headers)
            assert approved.status_code == 200, approved.text
            assert approved.json()["state"] == "committed"
            assert approved.json()["revision"] == proposal["revision"] + 1
            replay = await client.post(path, json=body, headers=headers)
            assert replay.status_code == 200 and replay.json() == approved.json()

            detail = await client.get(f"/v1/memory-reconsolidations/{key}?ceiling=restricted")
            assert detail.status_code == 200 and detail.json() == approved.json()
            canonical = UUID(detail.json()["content"]["memory_id"])
            assert canonical in ORIGINALS, "an equivalence merge keeps an original as canonical"
            assert await _recalled(app) == [canonical], "approval suppresses the other original"
            async with app.uow_factory() as uow:
                active = await uow.reconsolidation.active_merges(
                    app.principal, tuple(ORIGINALS), clock.now()
                )
                assert [merge.id for merge in active] == [UUID(key)]
                for belief_id, original in originals.items():
                    assert await uow.memories.get(belief_id, app.principal) == original, (
                        "approval preserves original facts and clocks"
                    )

            undone = await client.post(
                f"/v1/memory-reconsolidations/{key}/undo?ceiling=restricted",
                json={"expected_revision": approved.json()["revision"]},
                headers={"Idempotency-Key": "undo"},
            )
            assert undone.status_code == 200, undone.text
            assert (undone.json()["state"], undone.json()["reason"]) == ("undone", "owner_undo")
            assert set(await _recalled(app)) == ORIGINALS, "undo restores both originals"
            async with app.uow_factory() as uow:
                assert not await uow.reconsolidation.active_merges(
                    app.principal, tuple(ORIGINALS), clock.now()
                )

        async with _tables(settings.database_url) as connection:
            payloads = list(
                (
                    await connection.execute(select(ReconsolidationOperationHistoryRow.payload))
                ).scalars()
            )
        assert payloads, "owner decisions leave a durable operation history"
        for payload in payloads:
            assert memory().statement not in json.dumps(payload), "history retains no prose"

        clock.advance(timedelta(days=1))
        await worker.run_once()
        async with _client(app) as client:
            runs = (await client.get("/v1/dreaming")).json()["runs"]
            assert len(runs) == 2 and runs[0]["started_at"] > runs[1]["started_at"]
            assert runs[0]["proposals"] == 0, "the undone pair is not proposed again"
            listing = await client.get("/v1/memory-reconsolidations?ceiling=restricted")
            assert [(op["id"], op["state"]) for op in listing.json()["items"]] == [(key, "undone")]
        assert set(await _recalled(app)) == ORIGINALS
