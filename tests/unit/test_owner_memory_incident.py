"""Synthetic regressions for owner-memory writes and question formation."""

from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from agent_core.bootstrap import build
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import BeliefType
from agent_core.memory.retrieval import DeterministicQueryFormer
from tests.contract.memory_fixtures import user_event
from tests.contract.owner_memory_incident_cases import (
    deleted_correction_does_not_restore_old_claim,
    owner_formation_lifecycle,
    owner_question_recall,
)
from tests.contract.support import SESSION_ID, principal, session, tool_context
from tests.gates.test_runtime_tool_budget import _settings


@pytest.mark.parametrize("people_enabled", [False, True])
@pytest.mark.parametrize("scope", [None, "explicit-project"])
async def test_remember_defaults_to_authenticated_session_scope(
    people_enabled: bool, scope: str | None
) -> None:
    async with build(
        settings=replace(_settings(), people_enabled=people_enabled), principal=principal()
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(
                session().model_copy(update={"metadata": {"project_scope": "workshop"}})
            )
        await user_event(app.uow_factory, "My studio is in Alder Bay.")
        tool = app.tool_pipeline._registry.get("memory.remember")
        arguments = {"statement": "User's studio is in Alder Bay.", "subject": "studio"}
        if scope is not None:
            arguments["scope"] = scope
        errors = list(Draft202012Validator(tool.spec.input_schema).iter_errors(arguments))
        assert not errors, "Ordinary memory writes must not require a guessed project"
        result = await tool.execute(arguments, tool_context())
        assert result.ok and result.structured
        from uuid import UUID

        async with app.uow_factory() as uow:
            stored = await uow.memories.get(UUID(result.structured["belief_id"]), principal())
        assert stored.scope == (scope or "workshop")
        for version in ("1.0.0", "1.0.1", *(["2.0.0"] if people_enabled else [])):
            pinned = app.tool_pipeline._registry.get("memory.remember", version)
            assert "scope" in pinned.spec.input_schema["required"]


async def test_remember_refuses_a_foreign_session_before_defaulting_scope() -> None:
    async with build(settings=_settings(), principal=principal()) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(
                session().model_copy(update={"principal_id": "another-owner"})
            )
        tool = app.tool_pipeline._registry.get("memory.remember")
        with pytest.raises(NotFoundError):
            await tool.execute(
                {"statement": "User lives in Alder Bay.", "subject": "home"}, tool_context()
            )


@pytest.mark.parametrize(
    ("question", "types"),
    [
        ("Tell me everything you know about me. Even things you aren't certain about", set()),
        ("Nothing about my family?", {BeliefType.RELATIONSHIP}),
        ("What car do I drive?", {BeliefType.USER_MODEL_ATTR}),
        ("Where do I live?", {BeliefType.USER_MODEL_ATTR, BeliefType.FACT}),
    ],
)
def test_owner_questions_do_not_turn_sentence_openers_into_entities(
    question: str, types: set[BeliefType]
) -> None:
    query = DeterministicQueryFormer(principal()).from_text(question)[0]
    assert not set(query.subjects) & {"Tell", "Even", "Nothing", "What", "Where"}
    assert set(query.structured_belief_types) == types


def test_query_keeps_actual_names_and_does_not_invent_owner_identity() -> None:
    query = DeterministicQueryFormer(principal()).from_text("What did Morgan say about Alder?")[0]
    assert set(query.subjects) == {"Morgan", "Alder"}
    assert query.structured_belief_types == []


async def test_owner_question_recall_memory() -> None:
    async with build(settings=_settings(), principal=principal()) as app:
        await owner_question_recall(app)


async def test_successful_empty_recall_describes_coverage_without_inventing_outage() -> None:
    from tests.contract.memory_fixtures import formation_stack

    _, _, _, retriever = await formation_stack()
    query = DeterministicQueryFormer(principal()).from_text("Where do I live?")[0]
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert 'search_status="succeeded"' in result.rendered
    assert 'coverage="bounded"' in result.rendered
    assert result.tokens > 0


async def test_owner_formation_lifecycle_memory() -> None:
    async with build(settings=_settings(), principal=principal()) as app:
        await owner_formation_lifecycle(app)


@pytest.mark.parametrize("session_delete", [False, True])
async def test_deleted_correction_does_not_restore_old_claim_memory(session_delete: bool) -> None:
    async with build(settings=_settings(), principal=principal()) as app:
        await deleted_correction_does_not_restore_old_claim(app, session_delete=session_delete)


async def test_empty_coverage_notice_cannot_exceed_a_tiny_budget() -> None:
    from tests.contract.memory_fixtures import formation_stack

    _, _, _, retriever = await formation_stack()
    query = DeterministicQueryFormer(principal(), budget_tokens=1).from_text("Where do I live?")[0]
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert result.tokens <= query.budget_tokens
