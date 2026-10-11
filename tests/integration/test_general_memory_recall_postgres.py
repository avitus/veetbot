"""Production SQL candidate ordering, historical filtering and in-memory parity."""

from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.adapters.memory.in_memory import InMemoryMemoryStore
from agent_core.adapters.memory.reconsolidation_index import ReconsolidationIndex
from agent_core.adapters.memory.transactions import MemoryTransaction
from agent_core.bootstrap import build
from agent_core.domain.memory import BeliefType, Portability, Sensitivity
from tests.integration.test_memory_postgres_m9 import _query, _remember, _settings


@pytest.mark.parametrize("kind", list(BeliefType))
@pytest.mark.parametrize("historical", [False, True])
async def test_relevance_before_cap_and_adapter_parity(
    tmp_path: Path, kind: BeliefType, historical: bool
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as app:
        session_id = await app.sessions.create()
        target = await _remember(
            app,
            session_id,
            "User requested saffron meals.",
            subject="menu constraint",
            belief_type=kind,
            portability=Portability.CONTEXTUAL,
        )
        mirror = InMemoryMemoryStore(app.clock, ReconsolidationIndex(MemoryTransaction()))
        await mirror.upsert_belief(target)
        async with app.uow_factory() as uow:
            for index in range(161):
                row = target.model_copy(
                    update={
                        "id": uuid4(),
                        "subject": f"project {index}",
                        "statement": f"User requested project {index}.",
                        "store_position": target.store_position + index + 1,
                    }
                )
                await uow.memories.upsert_belief(row)
                await mirror.upsert_belief(row)
        query = _query(app, text="user saffron meals").model_copy(
            update={
                "candidate_selection": "relevance",
                "max_items": 20,
                "known_at": app.clock.now() if historical else None,
            }
        )
        async with app.uow_factory() as uow:
            candidates = await uow.memories.query(query)
            assert candidates[0].id == target.id
            assert [r.id for r in candidates] == [r.id for r in await mirror.query(query)]
            # Exclusion is a pre-limit predicate for both current and historical reads.
            excluded = query.model_copy(update={"exclude_ids": tuple(r.id for r in candidates[1:])})
            assert [r.id for r in await uow.memories.query(excluded)] == [
                r.id for r in await mirror.query(excluded)
            ]
            assert (await uow.memories.query(excluded))[0].id == target.id

        restrictions: list[dict[str, object]] = [
            {"tenant_id": "other-tenant"},
            {"principal_id": "other-owner"},
            {"sensitivity_ceiling": Sensitivity.PUBLIC},
            {"belief_types": [item for item in BeliefType if item != kind]},
        ]
        async with app.uow_factory() as uow:
            for changes in restrictions:
                restricted = query.model_copy(update=changes)
                assert await uow.memories.query(restricted) == []
                assert await mirror.query(restricted) == []


@pytest.mark.parametrize("historical", [False, True])
async def test_sql_candidate_arms_keep_independent_limits(tmp_path: Path, historical: bool) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as app:
        session_id = await app.sessions.create()
        exact = await _remember(
            app,
            session_id,
            "Amber is the maintenance window.",
            subject="release window",
            belief_type=BeliefType.FACT,
            portability=Portability.CONTEXTUAL,
        )
        lexical = await _remember(
            app,
            session_id,
            "Saffron meals are requested.",
            subject="menu constraint",
            belief_type=BeliefType.FACT,
            portability=Portability.CONTEXTUAL,
        )
        mirror = InMemoryMemoryStore(app.clock, ReconsolidationIndex(MemoryTransaction()))
        for record in (exact, lexical):
            await mirror.upsert_belief(record)
        async with app.uow_factory() as uow:
            for index in range(161):
                row = lexical.model_copy(
                    update={
                        "id": uuid4(),
                        "subject": f"preference {index}",
                        "statement": f"Unrelated preference {index}.",
                        "belief_type": BeliefType.PREFERENCE,
                        "store_position": lexical.store_position + index + 1,
                    }
                )
                await uow.memories.upsert_belief(row)
                await mirror.upsert_belief(row)
        query = _query(app, text="saffron meals").model_copy(
            update={
                "subjects": [exact.subject],
                "structured_belief_types": [BeliefType.PREFERENCE],
                "candidate_selection": "relevance",
                "max_items": 20,
                "known_at": app.clock.now() if historical else None,
            }
        )
        async with app.uow_factory() as uow:
            candidates = await uow.memories.query(query)
            assert {exact.id, lexical.id} <= {row.id for row in candidates}
            assert len(candidates) == 161  # 160 structured candidates plus the lexical match.
            assert [row.id for row in candidates] == [row.id for row in await mirror.query(query)]
