"""Production-derived recall failures, using synthetic personal information."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.context import WorkingState
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryClaimKind,
    MemoryDerivation,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    RecallProfile,
    RecallQuery,
    Sensitivity,
    is_owned_resource,
    recall_query_terms,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, TextPart, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
from agent_core.memory.retrieval import DeterministicQueryFormer, _arm_match, _score
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.memory_fixtures import formation_stack, memory, recall_query
from tests.contract.support import NOW, SESSION_ID, principal, run, tool_context
from tests.integration.m2_support import memory_settings

HYDRANGEA_QUESTION = "Is it worth adding iron fertilizer to my hydrangeas this late in the season?"


@pytest.mark.parametrize("deliberate", [False, True])
@pytest.mark.parametrize(
    ("question", "search", "statement"),
    [
        (
            "What octane gas should I use for my car? Does it make a difference?",
            "user car vehicle make model year engine gasoline octane",
            "User has a 2026 Aster S8.",
        ),
        (
            "Which charger should I get for my laptop?",
            "user laptop computer model charger",
            "User owns a Framework 13.",
        ),
    ],
)
async def test_personal_advice_recalls_older_owned_model_before_candidate_flood(
    deliberate: bool, question: str, search: str, statement: str
) -> None:
    clock, factory, _service, retriever = await formation_stack()
    target = memory(statement=statement).model_copy(
        update={
            "subject": "owned model",
            "belief_type": BeliefType.USER_MODEL_ATTR,
            "claim_kind": MemoryClaimKind.PROJECT_FACT,
            "status": MemoryStatus.PROVISIONAL,
            "authority": MemoryAuthority.INFERRED,
            "confidence": 0.65,
            "utility": -1.0,
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(target)
        for index in range(161):
            await uow.memories.upsert_belief(
                target.model_copy(
                    update={
                        "id": UUID(int=20000 + index),
                        "subject": f"project {index}",
                        "statement": f"User wants to improve project {index}.",
                        "store_position": index + 2,
                        "utility": 0.0,
                    }
                )
            )
    clock.advance(timedelta(days=14))
    snapshot = await retriever.snapshot(session_id=SESSION_ID, current_scope="project-a")
    assert snapshot.items == []
    if deliberate:
        result = await MemorySearchTool(retriever).execute(
            {"text": search, "scope": "project-a"}, tool_context()
        )
        assert result.structured is not None
        assert str(target.id) in result.structured["belief_ids"]
    else:
        query = DeterministicQueryFormer(principal()).from_text(question)[0]
        recalled = await retriever.recall(query, session_id=SESSION_ID)
        assert target.id in [item.belief_id for item in recalled.items]
        async with factory() as uow:
            trace = await uow.traces.get(recalled.trace_id, principal())
        assert trace.retrieval_policy_version == "retrieval@8+owned-resources@1"
        assert 'policy="retrieval@8+owned-resources@1"' in recalled.rendered


@pytest.mark.parametrize(
    "question", ["Who is my mother?", "Who is my emergency contact?", "How do I like my coffee?"]
)
def test_explicit_relationship_and_preference_queries_do_not_browse_possessions(
    question: str,
) -> None:
    assert not DeterministicQueryFormer(principal()).from_text(question)[0].include_owned_resources


@pytest.mark.parametrize(
    "statement",
    [
        "User wants a new vehicle.",
        "User no longer owns a sedan.",
        "User has no vehicle.",
        "User has to buy a new vehicle.",
        "User's friend owns a sedan.",
        "A sender reports that User owns a sedan.",
    ],
)
def test_owned_resource_anchor_does_not_infer_an_owner(statement: str) -> None:
    record = memory(statement=statement).model_copy(
        update={"belief_type": BeliefType.USER_MODEL_ATTR, "claim_kind": MemoryClaimKind.RESOURCE}
    )
    assert not is_owned_resource(record)


@pytest.mark.parametrize(
    "changes",
    [
        {"principal_id": "someone-else"},
        {"tenant_id": "somewhere-else"},
        {"sensitivity": Sensitivity.RESTRICTED},
        {"portability": Portability.LOCAL, "scope": "another-project"},
        {"status": MemoryStatus.RETIRED},
        {"expires_at": NOW - timedelta(seconds=1)},
        {"derivation": MemoryDerivation.HYPOTHESIS},
        {"polarity": Polarity.RETRACT},
    ],
)
async def test_owned_resource_anchor_keeps_visibility_and_evidence_boundaries(
    changes: dict[str, object],
) -> None:
    _clock, factory, _service, retriever = await formation_stack()
    record = memory(statement="User owns a Framework 13.").model_copy(
        update={
            "subject": "Framework 13",
            "belief_type": BeliefType.USER_MODEL_ATTR,
            "claim_kind": MemoryClaimKind.RESOURCE,
            **changes,
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    query = DeterministicQueryFormer(principal()).from_text("Charger for my laptop?")[0]
    query = query.model_copy(update={"sensitivity_ceiling": Sensitivity.INTERNAL})
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert result.items == []


async def test_personal_advice_model_request_contains_stored_model_without_snapshot_promotion(
    tmp_path: Path,
) -> None:
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
    ) as app:
        source_session = await app.sessions.create()
        target = memory(statement="User has a 2026 Aster S8.").model_copy(
            update={
                "tenant_id": app.principal.tenant_id,
                "principal_id": app.principal.principal_id,
                "source_session_id": source_session,
                "subject": "Aster S8",
                "belief_type": BeliefType.USER_MODEL_ATTR,
                "claim_kind": MemoryClaimKind.PROJECT_FACT,
                "status": MemoryStatus.PROVISIONAL,
                "authority": MemoryAuthority.INFERRED,
                "confidence": 0.65,
            }
        )
        async with app.uow_factory() as uow:
            await uow.memories.upsert_belief(target)
        session_id = await app.sessions.create()
        await app.runs.wait_terminal(
            await app.runs.submit("What octane gas should I use for my car?", session_id)
        )
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        request = provider.requests[0]
        assert any(
            target.statement in part.text
            for item in request.conversation
            if isinstance(item, UserMessage)
            for part in item.content
            if isinstance(part, TextPart)
        )
        plan = await app.executor._context_planner.current(session_id)
        assert plan is not None and target.statement not in plan.memory_snapshot
        await app.runs.wait_terminal(await app.runs.submit("Thanks", session_id))
        assert provider.requests[-1].metadata["prefix_sha256"] == request.metadata["prefix_sha256"]


@pytest.mark.parametrize(
    ("question", "statement", "belief_type"),
    [
        (
            "Which vehicle do I drive?",
            "User drives an electric cargo bike.",
            BeliefType.USER_MODEL_ATTR,
        ),
        ("Who is my partner?", "User's wife is Casey.", BeliefType.RELATIONSHIP),
        (
            "Which unit system do I prefer?",
            "User prefers metric measurements.",
            BeliefType.PREFERENCE,
        ),
        ("What did I buy last month?", "User owns a canoe.", BeliefType.USER_MODEL_ATTR),
    ],
)
async def test_explicit_personal_recall_uses_structured_profile_as_well_as_words(
    question: str, statement: str, belief_type: BeliefType
) -> None:
    _clock, factory, _service, retriever = await formation_stack()
    record = memory(statement=statement).model_copy(
        update={
            "subject": "personal detail",
            "belief_type": belief_type,
            "status": MemoryStatus.PROVISIONAL,
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    queries = DeterministicQueryFormer(principal()).form(run(), WorkingState(), question)
    result = await retriever.recall(queries[0], session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [record.id]
    assert "structured" in result.items[0].arms


@pytest.mark.parametrize(
    "question",
    [
        HYDRANGEA_QUESTION,
        "What should I plant?",
        "Which car should I buy?",
        "Who is their manager?",
        "How do I deploy this service?",
        "How do I configure PostgreSQL?",
        "How do I use PostgreSQL?",
    ],
)
async def test_advice_and_third_party_questions_do_not_browse_personal_profile(
    question: str,
) -> None:
    _clock, factory, _service, retriever = await formation_stack()
    record = memory(statement="The user wears a green scarf.")
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    query = DeterministicQueryFormer(principal()).form(run(), WorkingState(), question)[0]
    assert query.structured_belief_types == []
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert result.items == []


@pytest.mark.parametrize("question", [HYDRANGEA_QUESTION, "the this", "THE, this!"])
async def test_recall_does_not_return_memories_for_common_words(question: str) -> None:
    _clock, factory, _service, retriever = await formation_stack()
    noise = memory(statement="The user enabled this repository's notification emails.")
    async with factory() as uow:
        await uow.memories.upsert_belief(noise)
        candidates = await uow.memories.query(recall_query(text=question))
    assert candidates == []
    assert _score(noise, recall_query(text=question), now=NOW) is None
    result = await retriever.recall(recall_query(text=question), session_id=SESSION_ID)
    assert result.items == []


async def test_content_and_explicit_subject_matches_survive_common_word_filter() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    plant = memory(statement="The user grows hydrangeas.")
    named = memory(belief_id=503, statement="An explicitly named subject.").model_copy(
        update={"subject": "The", "store_position": 2}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(plant)
        await uow.memories.upsert_belief(named)
    result = await retriever.recall(recall_query(text=HYDRANGEA_QUESTION), session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [plant.id]
    result = await retriever.recall(
        recall_query(text="the this", subjects=["The"]), session_id=SESSION_ID
    )
    assert [item.belief_id for item in result.items] == [named.id]


async def test_snapshot_excludes_provisional_but_deliberate_and_delta_recall_keep_it() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    provisional = memory(statement="User lives in Exampleville.").model_copy(
        update={"status": MemoryStatus.PROVISIONAL, "subject": "home location"}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(provisional)
    snapshot = await retriever.snapshot(session_id=SESSION_ID, current_scope="project-a")
    assert snapshot.items == []
    delta = await retriever.recall(
        recall_query(text=None, profile=RecallProfile.CORE),
        session_id=SESSION_ID,
        moment="in_turn",
    )
    assert [item.belief_id for item in delta.items] == [provisional.id]
    result = await MemorySearchTool(retriever).execute(
        {"text": "home location city residence", "scope": "project-a"}, tool_context()
    )
    assert result.structured is not None
    assert result.structured["belief_ids"] == [str(provisional.id)]
    assert result.output_trust is TrustLevel.MEMORY


async def test_hydrangea_model_request_has_address_despite_provisional_candidate_flood(
    tmp_path: Path,
) -> None:
    """Exercise the real planner, query former, stores, and request builder.

    The older confirmed project fact must survive more provisional profile
    beliefs than the store's 320-candidate snapshot cap. No model is scripted
    to request the address: it must already be in its first request.
    """
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
    ) as app:
        source_session = await app.sessions.create()
        address = memory(
            belief_id=9000, statement="The home address is 123 Example Lane, Exampleville."
        ).model_copy(
            update={
                "tenant_id": app.principal.tenant_id,
                "principal_id": app.principal.principal_id,
                "source_session_id": source_session,
                "subject": "Example Owner",
                "scope": "veetbot",
                "origin_scopes": ["veetbot"],
                "belief_type": BeliefType.FACT,
                "portability": Portability.CONTEXTUAL,
                "authority": MemoryAuthority.AFFIRMED,
                "sensitivity": Sensitivity.RESTRICTED,
            }
        )
        noise = [
            address.model_copy(
                update={
                    "id": UUID(int=9100 + index),
                    "subject": f"notification preference {index}",
                    "statement": f"The user enabled repository {index} notification emails.",
                    "scope": "general",
                    "origin_scopes": ["general"],
                    "belief_type": BeliefType.USER_MODEL_ATTR,
                    "portability": Portability.PORTABLE,
                    "authority": MemoryAuthority.INFERRED,
                    "status": MemoryStatus.PROVISIONAL,
                    "confidence": 0.65,
                    "store_position": index + 2,
                }
            )
            for index in range(321)
        ]
        async with app.uow_factory() as uow:
            for record in [address, *noise]:
                await uow.memories.upsert_belief(record)
        session_id = await app.sessions.create()
        await app.runs.wait_terminal(await app.runs.submit(HYDRANGEA_QUESTION, session_id))
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        request = provider.requests[0]
        content = "\n".join(
            part.text
            for item in request.conversation
            if isinstance(item, UserMessage)
            for part in item.content
            if isinstance(part, TextPart)
        )
        assert address.statement in content
        assert not any(record.statement in content for record in noise)
        plan = await app.executor._context_planner.current(session_id)
        assert plan is not None and plan.snapshot_id is not None
        async with app.uow_factory() as uow:
            snapshot = await uow.traces.get(plan.snapshot_id, app.principal)
        assert snapshot.returned == [address.id]
        assert snapshot.candidates == 1
        assert snapshot.rendered == plan.memory_snapshot
        await app.runs.wait_terminal(await app.runs.submit("And next season?", session_id))
        assert provider.requests[-1].metadata["prefix_sha256"] == request.metadata["prefix_sha256"]


def test_memory_search_advertises_lookup_of_missing_personal_context() -> None:
    description = MemorySearchTool.spec.description
    assert "before asking" in description
    assert "home location" in description
    assert "snapshot" in description


def test_recall_filter_preserves_negation_names_and_non_english_terms() -> None:
    assert recall_query_terms("The user does not use Neovim or PostgreSQL") == [
        "neovim",
        "not",
        "postgresql",
        "use",
        "user",
    ]
    assert recall_query_terms("住所 東京") == ["住所 東京"]
    assert recall_query_terms("AI") == ["ai"]
    assert recall_query_terms("THE") == []


def test_historical_trace_queries_load_without_new_fields() -> None:
    payload = recall_query().model_dump(
        exclude={
            "include_provisional",
            "structured_belief_types",
            "include_owned_resources",
            "include_home_location",
            "people_overflow",
        }
    )
    loaded = RecallQuery.model_validate(payload)
    assert loaded.include_provisional is True
    assert loaded.structured_belief_types == []
    assert loaded.include_owned_resources is False
    assert loaded.model_dump().get("include_home_location") is False
    assert loaded.people_overflow is False


async def test_structured_profile_preserves_scope_ceiling_and_ranking() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    matched = memory(statement="User prefers concise reports.")
    background = memory(belief_id=510, statement="User prefers morning meetings.")
    hidden = memory(belief_id=511, statement="User prefers concise reports privately.").model_copy(
        update={"sensitivity": Sensitivity.RESTRICTED}
    )
    foreign = memory(belief_id=512).model_copy(update={"principal_id": "someone-else"})
    async with factory() as uow:
        for record in [matched, background, hidden, foreign]:
            await uow.memories.upsert_belief(record)
    query = recall_query(
        text="concise reports", sensitivity_ceiling=Sensitivity.INTERNAL
    ).model_copy(update={"structured_belief_types": [BeliefType.PREFERENCE]})
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [matched.id, background.id]
    assert result.items[0].score > result.items[1].score


@pytest.mark.parametrize(
    ("question", "belief_type", "statement"),
    [
        ("Who is my partner?", BeliefType.RELATIONSHIP, "User's wife is Casey."),
        ("Which vehicle do I drive?", BeliefType.USER_MODEL_ATTR, "User owns a cargo bike."),
        ("How do I like my coffee?", BeliefType.PREFERENCE, "User prefers espresso."),
    ],
)
async def test_personal_intent_survives_unrelated_type_candidate_flood(
    question: str, belief_type: BeliefType, statement: str
) -> None:
    _clock, factory, _service, retriever = await formation_stack()
    target = memory(statement=statement).model_copy(
        update={"subject": "personal detail", "belief_type": belief_type}
    )
    noise_type = (
        BeliefType.USER_MODEL_ATTR
        if belief_type is BeliefType.PREFERENCE
        else BeliefType.PREFERENCE
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(target)
        for index in range(161):
            await uow.memories.upsert_belief(
                memory(
                    belief_id=10000 + index, statement=f"Notification setting {index}."
                ).model_copy(
                    update={
                        "subject": f"setting {index}",
                        "belief_type": noise_type,
                        "store_position": index + 2,
                    }
                )
            )
    query = DeterministicQueryFormer(principal()).form(run(), WorkingState(), question)[0]
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [target.id]


GARDEN_QUESTION = "Maintain a gardening calendar based on what you know about our garden."


def _home(statement: str, *, subject: str = "home location") -> MemoryRecord:
    return memory(belief_id=7000, statement=statement).model_copy(
        update={
            "subject": subject,
            "belief_type": BeliefType.USER_MODEL_ATTR,
            "claim_kind": MemoryClaimKind.RECURRING_STATE,
            "status": MemoryStatus.PROVISIONAL,
            "authority": MemoryAuthority.INFERRED,
            "confidence": 0.6,
            "flagged_for_review": True,
            "sensitivity": Sensitivity.SENSITIVE,
            "scope": "general",
            "origin_scopes": ["general"],
        }
    )


async def _recalled_home(question: str, home: MemoryRecord) -> tuple[bool, str]:
    """Recall `question` over the home fact and a calendar-word candidate flood."""

    _clock, factory, _service, retriever = await formation_stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(home)
        for index in range(161):
            await uow.memories.upsert_belief(
                home.model_copy(
                    update={
                        "id": UUID(int=21000 + index),
                        "subject": f"calendar entry {index}",
                        "statement": f"Calendar entry {index} was added to the shared calendar.",
                        "belief_type": BeliefType.FACT,
                        "flagged_for_review": False,
                        "store_position": index + 2,
                    }
                )
            )
    query = DeterministicQueryFormer(principal(), current_scope="general").from_text(question)[0]
    recalled = await retriever.recall(query, session_id=SESSION_ID)
    async with factory() as uow:
        trace = await uow.traces.get(recalled.trace_id, principal())
    return home.id in [item.belief_id for item in recalled.items], trace.retrieval_policy_version


@pytest.mark.parametrize(
    "question",
    [
        GARDEN_QUESTION,
        "When should I prune the roses in my yard?",
        "What grows well around our house?",
        "Plan the planting calendar for my backyard.",
    ],
)
async def test_household_task_recalls_the_home_location_before_candidate_flood(
    question: str,
) -> None:
    """A task about the owner's own place needs where that place is.

    "Our garden" shares no word with "User lives in Exampleville", and no
    owner-question form names it, so only a residence anchor reaches it.
    """

    found, policy = await _recalled_home(question, _home("User lives in Exampleville, CA."))
    assert found
    assert policy.endswith("+home-location@1")


@pytest.mark.parametrize(
    "statement",
    [
        "User lives in Exampleville, CA.",
        "The user resides at 1 Example Lane, Exampleville.",
        "Example Owner's home mailing address is 1 Example Lane, Exampleville.",
        "The home address is 123 Example Lane, Exampleville.",
    ],
)
async def test_home_location_anchor_recognizes_residence_shapes(statement: str) -> None:
    found, _policy = await _recalled_home(
        GARDEN_QUESTION, _home(statement, subject="Example Owner")
    )
    assert found


@pytest.mark.parametrize(
    ("statement", "subject"),
    [
        ("User no longer lives in Oldtown.", "home location"),
        ("User lives to garden on weekends.", "home location"),
        ("Cheryl's home address is 4 Example Road.", f"person:{UUID(int=9)}:Cheryl"),
        ("User wants to visit Exampleville.", "travel"),
    ],
)
async def test_home_location_anchor_ignores_past_third_party_and_non_residence(
    statement: str, subject: str
) -> None:
    found, _policy = await _recalled_home(GARDEN_QUESTION, _home(statement, subject=subject))
    assert not found


@pytest.mark.parametrize(
    "question",
    [
        "How do I start a vegetable garden?",
        "What is the best garden hose?",
        "Where is Cheryl's house?",
        "Which house should we buy?",
    ],
)
async def test_generic_or_third_party_places_do_not_anchor_the_home_location(
    question: str,
) -> None:
    found, policy = await _recalled_home(question, _home("User lives in Exampleville, CA."))
    assert not found
    assert "home-location" not in policy


async def test_hypothesized_residence_does_not_anchor() -> None:
    home = _home("User lives in Exampleville, CA.").model_copy(
        update={"derivation": MemoryDerivation.HYPOTHESIS}
    )
    found, _policy = await _recalled_home(GARDEN_QUESTION, home)
    assert not found


BOARD_QUESTION = "Which company boards do I sit on?"


@pytest.mark.parametrize(
    ("asked", "stored", "folds"),
    [
        ("boards", "board", True),
        ("board", "boards", True),
        ("sit", "sits", True),
        ("company", "companies", True),
        ("class", "classes", True),
        ("box", "boxes", True),
        ("sit", "sites", False),
        ("status", "statu", False),
        ("analysis", "analysi", False),
        ("glass", "glas", False),
    ],
)
def test_relevance_recall_folds_a_plural_or_third_person_s(
    asked: str, stored: str, folds: bool
) -> None:
    """ADR-0259: "boards" finds "board" and "sit" finds "sits", in relevance mode only."""

    record = memory(statement=f"Example {stored} note.")
    relevance = recall_query(text=asked).model_copy(update={"candidate_selection": "relevance"})
    recent = recall_query(text=asked)
    profile = DEFAULT_RETRIEVAL_PROFILE
    folded, _arms = _arm_match(
        record, relevance, profile=profile, semantic_rank=None, semantic_only=False
    )
    exact, _arms = _arm_match(
        record, recent, profile=profile, semantic_rank=None, semantic_only=False
    )
    assert folded == (1.0 if folds else 0.0)
    # The frozen base selector keeps whole-lexeme matching.
    assert exact == 0.0


async def test_folded_words_rank_a_board_seat_claim_above_a_one_word_flood() -> None:
    """2026-10-26 probe: the learned board-seat claim ranked 53rd.

    It shared only "boards" with the question; a flood sharing only "company"
    tied it and won on recency. Folded, it shares "boards" and "sit".
    """

    _clock, factory, _service, retriever = await formation_stack()
    claim = memory(
        belief_id=7100, statement="He sits on the boards of Northwind, Contoso and Fabrikam."
    ).model_copy(update={"subject": "example.com", "scope": "general"})
    async with factory() as uow:
        await uow.memories.upsert_belief(claim)
        for index in range(161):
            await uow.memories.upsert_belief(
                claim.model_copy(
                    update={
                        "id": UUID(int=22000 + index),
                        "subject": f"portfolio note {index}",
                        "statement": f"Company {index} hired a new finance lead.",
                        "store_position": index + 2,
                    }
                )
            )
    query = DeterministicQueryFormer(principal(), current_scope="general").from_text(
        BOARD_QUESTION
    )[0]
    recalled = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in recalled.items][:1] == [claim.id]
    async with factory() as uow:
        trace = await uow.traces.get(recalled.trace_id, principal())
    assert trace.retrieval_policy_version.startswith("retrieval@8")
