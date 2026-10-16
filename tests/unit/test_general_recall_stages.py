import hashlib
from uuid import UUID

import pytest

from agent_core.evals.general_recall_corpus import EvidenceFixture
from agent_core.evals.general_recall_stages import evidence_labels
from tests.contract.memory_fixtures import memory
from tests.contract.support import SESSION_ID
from tests.unit.test_general_recall_corpus import fixture


def test_stage_labels_require_both_supported_claim_and_exact_source() -> None:
    label = EvidenceFixture(
        id="route",
        source_ids=("h1",),
        subject="accessibility",
        statement="User requires step-free routes",
    )
    case = fixture().model_copy(update={"evidence": (label,)})
    record = memory(statement="User requires step-free routes").model_copy(
        update={
            "subject": "accessibility",
            "source_event_ids": [7],
        }
    )
    unrelated = record.model_copy(update={"id": UUID(int=502), "source_event_ids": [8]})
    negated = record.model_copy(
        update={"id": UUID(int=503), "statement": "User does not require step-free routes"}
    )
    assert evidence_labels(case, [record, unrelated, negated], {"h1": (SESSION_ID, 7)}) == {
        record.id: ("route",),
        unrelated.id: (),
        negated.id: (),
    }


def test_authored_equivalents_do_not_leak_to_the_model() -> None:
    label = EvidenceFixture(
        id="route",
        source_ids=("h1",),
        subject="accessibility",
        statement="User requires step-free routes",
        equivalent_statements=("User avoids stairs",),
    )
    case = fixture().model_copy(update={"evidence": (label,)})
    record = memory(statement="User avoids stairs").model_copy(
        update={
            "subject": "accessibility",
            "source_event_ids": [7],
        }
    )
    assert evidence_labels(case, [record], {"h1": (SESSION_ID, 7)}) == {record.id: ("route",)}
    assert "evidence" not in case.reader_input()


def test_atomic_label_requires_every_authored_source_for_a_multi_source_claim() -> None:
    label = EvidenceFixture(
        id="route",
        source_ids=("h1", "h2"),
        subject="accessibility",
        statement="User requires step-free routes",
    )
    case = fixture().model_copy(update={"evidence": (label,)})
    partial = memory(statement=label.statement).model_copy(
        update={"subject": label.subject, "source_event_ids": [7]}
    )
    complete = partial.model_copy(update={"id": UUID(int=505), "source_event_ids": [7, 8]})
    assert evidence_labels(
        case, [partial, complete], {"h1": (SESSION_ID, 7), "h2": (SESSION_ID, 8)}
    ) == {partial.id: (), complete.id: ("route",)}


def test_stale_answer_value_is_reported_even_when_not_retrieved() -> None:
    from agent_core.evals.general_recall_stages import answer_violations

    label = EvidenceFixture(
        id="old",
        source_ids=("h1",),
        subject="review",
        statement="The review starts at 18:30.",
        required=False,
        invalid_current=True,
        invalid_answer_values={"route": ("18:30",)},
    )
    case = fixture().model_copy(update={"evidence": (label,)})
    assert answer_violations(case, '{"action":"answer","values":{"route":"18:30"}}') == ("old",)
    assert answer_violations(case, '{"action":"answer","values":{"route":"17:15"}}') == ()


def test_authored_subject_alias_requires_the_same_source_and_claim() -> None:
    label = EvidenceFixture(
        id="rationale",
        source_ids=("h1",),
        subject="pier material",
        statement="The team decided limestone for the pier because salt spray corrodes steel.",
    ).model_copy(update={"equivalent_subjects": ("project decision",)})
    case = fixture().model_copy(update={"evidence": (label,)})
    record = memory(statement=label.statement).model_copy(
        update={
            "subject": "project decision",
            "source_event_ids": [7],
        }
    )
    wrong = record.model_copy(
        update={"id": UUID(int=504), "statement": "The team decided steel for the pier."}
    )
    assert evidence_labels(case, [record, wrong], {"h1": (SESSION_ID, 7)}) == {
        record.id: ("rationale",),
        wrong.id: (),
    }


@pytest.mark.parametrize("fits", [False, True])
async def test_source_witnesses_record_exact_read_and_actual_context_separately(fits: bool) -> None:
    from agent_core.adapters.determinism import SequenceIdFactory
    from agent_core.evals.general_recall_stages import recovered_source_witnesses
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from agent_core.memory.source_recovery import BoundedSourceRecovery
    from tests.contract.memory_fixtures import formation_stack, recall_query, user_event
    from tests.contract.support import principal

    clock, factory, _, _ = await formation_stack()
    case = fixture()
    source = case.history[0]
    sequence = await user_event(factory, source.content)
    anchor = memory(statement="The user is recovering.").model_copy(
        update={"subject": "recovery", "scope": "general", "source_event_ids": [sequence]}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(anchor)
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8000, 8020)),
        principal(),
        source_recovery=BoundedSourceRecovery(principal(), admitted=lambda: True),
    )
    query = recall_query(text="recovering", budget_tokens=2000 if fits else 100).model_copy(
        update={"current_scope": "general"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID)
    observed = recovered_source_witnesses(case, [result], [result], {"h1": (SESSION_ID, sequence)})
    assert len(observed) == 1
    witness = observed[0]
    assert witness.source_id == "h1" and witness.anchor_rank == 1
    assert witness.text_sha256 == hashlib.sha256(source.content.encode()).hexdigest()
    assert witness.rendered is fits
    # Distinct searches have independent ranking scales. Taking each one's best
    # rank would grant multiple separate top-twenty budgets to one observation.
    assert result.stages is not None
    later = result.model_copy(
        update={
            "trace_id": UUID(int=8888),
            "stages": result.stages.model_copy(
                update={"ranked_ids": (UUID(int=8889), *result.stages.ranked_ids)}
            ),
        }
    )
    combined = recovered_source_witnesses(
        case, [result, later], [result], {"h1": (SESSION_ID, sequence)}
    )
    assert combined[0].anchor_rank is None, "Separate query ranks are not a global rank"
    dropped = recovered_source_witnesses(case, [result], [], {"h1": (SESSION_ID, sequence)})
    if fits:
        missing_quote = result.model_copy(update={"rendered": "The user is recovering."})
        with pytest.raises(ValueError, match="rendered source"):
            recovered_source_witnesses(
                case, [missing_quote], [missing_quote], {"h1": (SESSION_ID, sequence)}
            )
    assert dropped[0].rendered is False
    changed = case.model_copy(
        update={"history": (source.model_copy(update={"content": "Changed"}),)}
    )
    with pytest.raises(ValueError, match="source digest"):
        recovered_source_witnesses(changed, [result], [result], {"h1": (SESSION_ID, sequence)})


def test_rendered_noise_includes_unlabeled_people_and_deduplicates_repeated_results() -> None:
    from agent_core.domain.memory import RecallResult, Sensitivity, TracedPersonContext
    from agent_core.evals.general_recall_stages import rendered_noise_items

    person = TracedPersonContext(
        record_id=UUID(int=900),
        revision=1,
        person_ids=[UUID(int=901)],
        kind="person",
        text="An unrelated colleague",
        sensitivity=Sensitivity.SENSITIVE,
    )
    first = RecallResult(
        items=[],
        people=[person],
        rendered="An unrelated colleague",
        tokens=10,
        truncated=False,
        trace_id=UUID(int=902),
    )
    another = first.model_copy(
        update={
            "people": [person.model_copy(update={"record_id": UUID(int=903)})],
            "trace_id": UUID(int=904),
        }
    )
    assert (
        rendered_noise_items(
            [first, first, another], labels={}, required=set(), useful_anchors=set()
        )
        == 2
    )
