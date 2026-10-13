import json
from datetime import UTC, datetime, timedelta

import pytest

from agent_core.evals.general_recall_corpus import (
    HistoryTurn,
    RecallFixture,
    TaskInput,
    score_answer,
    validate_corpus,
)

AT = datetime(2026, 10, 12, tzinfo=UTC)


def fixture() -> RecallFixture:
    return RecallFixture(
        id="pickup-route",
        family="preferences",
        split="development",
        scenario_lineage="school-access",
        query_lineage="plan-pickup-route",
        implicit_task=True,
        history=(
            HistoryTurn(
                id="h1",
                session="s1",
                at=AT - timedelta(days=14),
                content="I cannot use stairs while recovering. Please choose step-free routes.",
            ),
        ),
        task=TaskInput(
            question="Route me to pickup: the footbridge has stairs; the east ramp is step-free.",
            at=AT,
            output_fields={"route": "The chosen route"},
            output_kind="proposed_tool",
        ),
        expected_action="answer",
        expected_values={"route": ("east ramp",)},
    )


def test_only_task_and_output_contract_reach_reader() -> None:
    case = fixture()
    actual = case.reader_input()
    assert actual == case.task.model_dump(mode="json")
    encoded = json.dumps(actual)
    assert "expected_values" not in encoded and "school-access" not in encoded
    assert "recovering" not in encoded, "History must reach the reader through actual retrieval"


@pytest.mark.parametrize("mutation", ["scenario", "template", "history", "time", "output"])
def test_corpus_rejects_variant_leakage_and_unscorable_inputs(mutation: str) -> None:
    first = fixture()
    second = first.model_copy(
        update={
            "id": "other",
            "scenario_lineage": "other",
            "query_lineage": "other",
            "split": "holdout",
            "history": (
                first.history[0].model_copy(
                    update={"content": "I take the east ramp because the bridge is closed."}
                ),
            ),
        }
    )
    if mutation == "scenario":
        second = second.model_copy(update={"scenario_lineage": first.scenario_lineage})
    elif mutation == "template":
        second = second.model_copy(update={"query_lineage": first.query_lineage})
    elif mutation == "history":
        second = second.model_copy(update={"history": first.history})
    elif mutation == "time":
        second = second.model_copy(
            update={"task": second.task.model_copy(update={"at": AT - timedelta(days=30)})}
        )
    else:
        second = second.model_copy(update={"expected_values": {"unknown_slot": ("east ramp",)}})
    with pytest.raises(ValueError):
        validate_corpus((first, second))


@pytest.mark.parametrize(
    "value,correct",
    [
        ("east ramp", True),
        (" EAST  RAMP ", True),
        ("Not east ramp", False),
        ("footbridge", False),
        (None, False),
    ],
)
def test_task_scoring_requires_exact_supported_slot_value(value: str | None, correct: bool) -> None:
    score = score_answer(fixture(), json.dumps({"action": "answer", "values": {"route": value}}))
    assert score.correct is correct
    assert score.answer_atoms == (("route",) if correct else ())
    assert bool(score.unsupported_atoms) is (value is not None and not correct)


@pytest.mark.parametrize(
    "raw",
    [
        "Not JSON",
        '{"action":"answer","values":{"route":"east ramp","route":"footbridge"}}',
        '{"action":"answer","values":{"route":"east ramp","extra":"fabricated"}}',
    ],
)
def test_malformed_or_unrequested_outputs_cannot_pass(raw: str) -> None:
    score = score_answer(fixture(), raw)
    assert not score.correct and score.unsupported_atoms


def test_unknown_requires_empty_values_and_correct_action() -> None:
    case = fixture().model_copy(
        update={"expected_action": "clarify", "expected_values": {"route": ()}}
    )
    assert score_answer(case, '{"action":"clarify","values":{"route":null}}').correct
    assert not score_answer(case, '{"action":"answer","values":{"route":null}}').correct
    assert not score_answer(case, '{"action":"clarify","values":{"route":"footbridge"}}').correct


def test_small_pilot_cannot_claim_release_census() -> None:
    validate_corpus((fixture(),))
    with pytest.raises(ValueError, match="400"):
        validate_corpus((fixture(),), release=True)


@pytest.mark.parametrize("change", ["absent_source", "duplicate", "required_invalid"])
def test_evidence_labels_require_unambiguous_source_provenance(change: str) -> None:
    from agent_core.evals.general_recall_corpus import EvidenceFixture

    evidence = EvidenceFixture(
        id="route",
        source_ids=("h1",),
        subject="accessibility",
        statement="User requires step-free routes",
    )
    labels: tuple[EvidenceFixture, ...] = (evidence,)
    if change == "absent_source":
        labels = (evidence.model_copy(update={"source_ids": ("missing",)}),)
    elif change == "duplicate":
        labels = (evidence, evidence)
    else:
        labels = (evidence.model_copy(update={"invalid_current": True}),)
    with pytest.raises(ValueError, match="evidence"):
        validate_corpus((fixture().model_copy(update={"evidence": labels}),))


def test_superseded_seed_requires_a_dated_replacement() -> None:
    from agent_core.domain.memory import MemoryStatus
    from agent_core.evals.general_recall_corpus import EvidenceFixture

    label = EvidenceFixture(
        id="old",
        source_ids=("h1",),
        subject="meeting",
        statement="The meeting starts at 18:30.",
        status=MemoryStatus.SUPERSEDED,
        required=False,
        invalid_current=True,
    )
    with pytest.raises(ValueError, match="superseded"):
        validate_corpus((fixture().model_copy(update={"evidence": (label,)}),))


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_source",
        "invalid_slot",
        "backward_close",
        "self_replace",
        "replacement_before_close",
    ],
)
def test_rejects_unscorable_lifecycle_and_invalid_answer_labels(change: str) -> None:
    from agent_core.domain.memory import MemoryStatus
    from agent_core.evals.general_recall_corpus import EvidenceFixture

    original = EvidenceFixture(
        id="old",
        source_ids=("h1",),
        subject="route",
        statement="User used the west ramp",
        required=False,
        status=MemoryStatus.SUPERSEDED,
        invalid_current=True,
        valid_to=AT - timedelta(days=1),
        superseded_by="new",
        invalid_answer_values={"route": ("west ramp",)},
    )
    replacement = EvidenceFixture(
        id="new", source_ids=("h2",), subject="route", statement="User uses the east ramp"
    )
    turns = (
        *fixture().history,
        HistoryTurn(
            id="h2", session="s2", at=AT - timedelta(days=1), content="Use the east ramp now."
        ),
    )
    if change == "duplicate_source":
        replacement = replacement.model_copy(update={"source_ids": ("h2", "h2")})
    elif change == "invalid_slot":
        original = original.model_copy(
            update={"invalid_answer_values": {"not_a_requested_field": ("west ramp",)}}
        )
    elif change == "backward_close":
        original = original.model_copy(update={"valid_to": AT - timedelta(days=30)})
    elif change == "self_replace":
        original = original.model_copy(update={"superseded_by": "old"})
    else:
        replacement = replacement.model_copy(update={"source_ids": ("h1",)})
    case = fixture().model_copy(update={"history": turns, "evidence": (original, replacement)})
    with pytest.raises(ValueError):
        validate_corpus((case,))


@pytest.mark.parametrize("omission", ["incident", "implicit_cell", None])
def test_release_census_requires_incidents_and_implicit_tasks_in_every_cell(
    omission: str | None,
) -> None:
    from agent_core.evals.general_memory_recall import FAMILIES

    # Fabricated shape data tests validation only; these are not independent
    # authored scenarios or release observations.
    cases = []
    original = fixture()
    for family in sorted(FAMILIES):
        for index in range(50):
            key = f"{family}-{index}"
            cases.append(
                original.model_copy(
                    update={
                        "id": key,
                        "family": family,
                        "split": "development" if index < 25 else "holdout",
                        "scenario_lineage": key,
                        "query_lineage": key,
                        "history": (original.history[0].model_copy(update={"content": key}),),
                        "known_incident": omission != "incident" and key == "attributes-0",
                        "implicit_task": not (
                            omission == "implicit_cell" and family == "routines" and index < 25
                        ),
                    }
                )
            )
    if omission is not None:
        with pytest.raises(ValueError, match=r"incident|implicit"):
            validate_corpus(tuple(cases), release=True)
    else:
        validate_corpus(tuple(cases), release=True)
