"""General candidate starvation regressions, independent of personal categories."""

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.domain.memory import BeliefType, MemoryStatus, Portability
from agent_core.domain.messages import TextPart
from agent_core.memory.retrieval import DeterministicQueryFormer
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.memory_fixtures import formation_stack, memory, recall_query
from tests.contract.support import NOW, SESSION_ID, principal, tool_context


@pytest.mark.parametrize("kind", list(BeliefType))
@pytest.mark.parametrize("historical", [False, True])
async def test_relevance_precedes_recent_candidate_flood(
    kind: BeliefType, historical: bool
) -> None:
    clock, factory, _service, retriever = await formation_stack()
    target = memory(statement="User requested saffron meals.").model_copy(
        update={
            "subject": "menu constraint",
            "belief_type": kind,
            "portability": Portability.CONTEXTUAL,
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(target)
        for index in range(161):
            await uow.memories.upsert_belief(
                target.model_copy(
                    update={
                        "id": UUID(int=20_000 + index),
                        "subject": f"project {index}",
                        "statement": f"User requested project {index}.",
                        "store_position": index + 2,
                    }
                )
            )
    clock.advance(timedelta(days=14))
    query = recall_query(text="user saffron meals", max_items=20).model_copy(
        update={"candidate_selection": "relevance", "known_at": NOW if historical else None}
    )
    async with factory() as uow:
        candidates = await uow.memories.query(query)
        legacy = await uow.memories.query(
            query.model_copy(update={"candidate_selection": "recent"})
        )
    assert target.id not in {row.id for row in legacy}
    assert candidates[0].id == target.id
    recalled = await retriever.recall(query, session_id=SESSION_ID)
    assert target.id in {item.belief_id for item in recalled.items}


async def test_lexical_and_structured_candidates_have_independent_limits() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    exact = memory(statement="Amber is the maintenance window.")
    exact = exact.model_copy(
        update={
            "subject": "release window",
            "belief_type": BeliefType.FACT,
            "portability": Portability.CONTEXTUAL,
        }
    )
    lexical = memory(belief_id=503, statement="Saffron meals are requested.").model_copy(
        update={
            "subject": "menu",
            "store_position": 2,
            "belief_type": BeliefType.FACT,
            "portability": Portability.CONTEXTUAL,
        }
    )
    async with factory() as uow:
        for record in (exact, lexical):
            await uow.memories.upsert_belief(record)
        for index in range(161):
            await uow.memories.upsert_belief(
                memory(
                    belief_id=20_000 + index, statement=f"Unrelated preference {index}."
                ).model_copy(update={"subject": f"preference {index}", "store_position": index + 3})
            )
        query = recall_query(
            text="saffron meals", subjects=[exact.subject], max_items=20
        ).model_copy(
            update={
                "candidate_selection": "relevance",
                "structured_belief_types": [BeliefType.PREFERENCE],
            }
        )
        candidates = await uow.memories.query(query)
    assert {exact.id, lexical.id} <= {row.id for row in candidates}
    assert len(candidates) <= 2 * max(query.max_items * 8, 64)


async def test_excluded_high_relevance_candidates_cannot_consume_the_limit() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    target = memory(statement="User requested saffron meals.")
    excluded = []
    async with factory() as uow:
        await uow.memories.upsert_belief(target)
        for index in range(161):
            record = target.model_copy(
                update={"id": UUID(int=20_000 + index), "store_position": index + 2}
            )
            excluded.append(record.id)
            await uow.memories.upsert_belief(record)
        query = recall_query(
            text="saffron meals", max_items=20, exclude_ids=tuple(excluded)
        ).model_copy(update={"candidate_selection": "relevance"})
        assert [row.id for row in await uow.memories.query(query)] == [target.id]


async def test_newer_irrelevant_or_ineligible_records_do_not_change_the_best_match() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    target = memory(statement="User requested saffron meals.")
    query = recall_query(text="saffron meals").model_copy(
        update={"candidate_selection": "relevance"}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(target)
        before = await uow.memories.query(query)
        boundaries: list[dict[str, object]] = [
            {"principal_id": "another-owner"},
            {"tenant_id": "another-tenant"},
            {"status": MemoryStatus.RETIRED},
            {"statement": "Unrelated project notes."},
        ]
        for index, changes in enumerate(boundaries):
            await uow.memories.upsert_belief(
                target.model_copy(
                    update={"id": UUID(int=20_000 + index), "store_position": index + 2, **changes}
                )
            )
        assert await uow.memories.query(query) == before


def test_live_query_formation_selects_relevance_explicitly() -> None:
    query = DeterministicQueryFormer(principal()).from_text("Saffron meals?")[0]
    assert query.candidate_selection == "relevance"
    assert recall_query().candidate_selection == "recent"


async def test_deliberate_search_uses_general_candidate_selection() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    target = memory(statement="User requested saffron meals.")
    async with factory() as uow:
        await uow.memories.upsert_belief(target)
        for index in range(161):
            await uow.memories.upsert_belief(
                target.model_copy(
                    update={
                        "id": UUID(int=20_000 + index),
                        "subject": f"project {index}",
                        "statement": f"User requested project {index}.",
                        "store_position": index + 2,
                    }
                )
            )
    result = await MemorySearchTool(retriever).execute(
        {"text": "user saffron meals", "scope": "project-a"}, tool_context()
    )
    assert result.structured is not None
    assert str(target.id) in result.structured["belief_ids"]
    assert isinstance(result.content[0], TextPart)
    assert 'policy="retrieval@7' in result.content[0].text


@pytest.mark.parametrize("terms", ["SAFFRON saffron meals", "saffron meals", "saffron-meals"])
async def test_query_repetition_case_and_whole_lexemes_preserve_best_match(terms: str) -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    # PostgreSQL simple FTS indexes a compound and its component lexemes.
    # An unhyphenated source would not satisfy the compound query's full lexeme.
    target = memory(statement="Saffron-meals are requested.")
    near = memory(belief_id=505, statement="Saffronmeals are a project name.")
    async with factory() as uow:
        await uow.memories.upsert_belief(target)
        await uow.memories.upsert_belief(near)
        rows = await uow.memories.query(
            recall_query(text=terms).model_copy(update={"candidate_selection": "relevance"})
        )
    assert [row.id for row in rows] == [target.id]
