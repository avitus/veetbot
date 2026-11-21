"""Owner recall questions: what a question may hold and what an edit may change."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.domain.recall_questions import (
    RECALL_QUESTION_FAMILIES,
    QuestionEvidence,
    QuestionSplit,
    RecallQuestion,
    RecallQuestionContent,
)
from agent_core.evals.general_memory_recall import FAMILIES
from agent_core.evals.owner_recall_probes import (
    EvidenceObservation,
    PathObservation,
    Probe,
    ProbeObservation,
    ProbeReport,
    summarize,
)
from agent_core.evals.owner_recall_questions import (
    content_from_probe,
    loaded_from_questions,
    probe_from_question,
    results_from_report,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _content(**overrides: object) -> RecallQuestionContent:
    values: dict[str, object] = {
        "question": "Where do I live?",
        "family": "attributes",
        "expected": ({"statement_contains": "Example Town"},),
    }
    values.update(overrides)
    return RecallQuestionContent.model_validate(values)


def test_question_families_are_the_probe_families() -> None:
    assert RECALL_QUESTION_FAMILIES == FAMILIES


@pytest.mark.parametrize(
    "evidence",
    [{}, {"belief_id": str(UUID(int=7)), "statement_contains": "Example Town"}],
)
def test_evidence_names_exactly_one_of_a_belief_or_a_text(evidence: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        QuestionEvidence.model_validate(evidence)


def test_an_unknown_family_is_refused() -> None:
    with pytest.raises(ValidationError, match="family"):
        _content(family="hobbies")


def test_a_question_that_measures_nothing_is_refused() -> None:
    with pytest.raises(ValidationError, match="measures nothing"):
        _content(expected=())
    # A name the delivered recall must render is a measure on its own.
    assert _content(expected=(), delivered_must_contain=("Erin",)).delivered_must_contain


@pytest.mark.parametrize(
    "field",
    ["forbidden", "delivered_must_contain", "answer_must_contain", "answer_must_not_contain"],
)
def test_a_blank_text_in_a_list_is_refused(field: str) -> None:
    with pytest.raises(ValidationError, match="blank"):
        _content(**{field: ("  ",)})


def test_the_split_is_not_part_of_what_an_edit_can_change() -> None:
    with pytest.raises(ValidationError, match="split"):
        _content(split="development")


def _question(key: str = "home-city", **content: object) -> RecallQuestion:
    return RecallQuestion(
        id=UUID(int=11),
        tenant_id="tenant-a",
        principal_id="principal-a",
        key=key,
        split=QuestionSplit.HOLDOUT,
        content=_content(**content),
        created_at=NOW,
        updated_at=NOW,
    )


def test_a_question_becomes_a_probe_the_driver_can_run() -> None:
    question = _question(
        expected=(
            {"statement_contains": "Example Town"},
            {"belief_id": str(UUID(int=9)), "required": False},
        ),
        forbidden=("Old Town",),
        delivered_must_contain=("Erin",),
        answer_must_contain=("Example",),
        answer_must_not_contain=("Old",),
        search_text="home city",
        notes="chat 10-20",
    )
    probe = probe_from_question(question)

    assert (probe.id, probe.family, probe.question) == (
        "home-city",
        "attributes",
        "Where do I live?",
    )
    assert probe.incident == "owner-questions-holdout" and probe.search_text == "home city"
    assert [
        (item.statement_contains, item.belief_id, item.required) for item in probe.expected
    ] == [
        ("Example Town", None, True),
        (None, UUID(int=9), False),
    ]
    # Only the required facts set the bar a run has to clear.
    assert probe.min_required_rendered == 1
    assert [item.statement_contains for item in probe.forbidden] == ["Old Town"]
    assert probe.delivered_must_contain == ("Erin",)
    assert (probe.answer_must_contain, probe.answer_must_not_contain) == (("Example",), ("Old",))
    assert probe.paths == ("automatic", "deliberate") and probe.write is None


def test_a_probe_file_line_imports_as_question_content_and_back() -> None:
    question = _question(forbidden=("Old Town",), delivered_must_contain=("Erin",))
    probe = probe_from_question(question)

    assert content_from_probe(probe) == question.content


def test_a_save_probe_is_not_a_question() -> None:
    probe = Probe.model_validate(
        {
            "id": "owner-save",
            "incident": "save",
            "family": "temporal",
            "question": "Remember that I prune roses in January.",
            "write": {"statement": "User prunes roses in January.", "subject": "roses"},
        }
    )
    with pytest.raises(ValueError, match="save probe"):
        content_from_probe(probe)


def test_questions_load_as_one_digest_bound_set_in_key_order() -> None:
    first = loaded_from_questions([_question("b-two"), _question("a-one")], label="holdout")
    again = loaded_from_questions([_question("a-one"), _question("b-two")], label="holdout")

    assert [probe.id for probe in first.probes] == ["a-one", "b-two"]
    assert first.digest == again.digest and first.path == "database:owner-questions:holdout"
    with pytest.raises(ValueError, match="no questions"):
        loaded_from_questions([], label="holdout")


def _path(
    path: str, stage: str, *, delivered: int, found: tuple[bool, ...] = ()
) -> PathObservation:
    return PathObservation.model_validate(
        {
            "path": path,
            "run_id": None,
            "run_status": "COMPLETED",
            "trace_ids": (),
            "delivered_trace_ids": (),
            "arms": ("lexical",),
            "elapsed_ms": 5,
            "recall_latency_ms": 3,
            "within_latency_ceiling": None,
            "evidence": (
                EvidenceObservation.model_validate(
                    {"expected_index": 0, "belief_id": None, "required": True, "stage": stage}
                ),
            ),
            "delivered_found": found,
            "required_delivered": delivered,
            "forbidden_delivered": 0,
            "answer_correct": None,
        }
    )


def test_a_probe_report_becomes_each_questions_latest_result() -> None:
    observations = (
        ProbeObservation(
            probe_id="home-city",
            incident="owner-questions-holdout",
            family="attributes",
            content_sha256="c" * 64,
            paths=(
                _path("automatic", "ranked", delivered=0, found=(True,)),
                _path("deliberate", "delivered", delivered=1, found=(True,)),
            ),
        ),
        # A run that cannot say what content it tested is bound to no question.
        ProbeObservation(
            probe_id="unbound",
            incident="owner-questions-holdout",
            family="attributes",
            paths=(_path("automatic", "delivered", delivered=1),),
        ),
        # An incident probe that happens to be in the same report is not a question.
        ProbeObservation(
            probe_id="car",
            incident="car",
            family="attributes",
            paths=(_path("automatic", "delivered", delivered=1),),
        ),
    )
    report = ProbeReport(
        generated_at=NOW,
        probe_digest=None,
        live_model=False,
        implementation={},
        store={},
        probes=observations,
        summary=summarize(observations),
    )

    results = results_from_report(report, report_digest="b" * 64, recorded_at=NOW)

    assert set(results) == {"home-city"}
    result = results["home-city"]
    assert [(item.path, item.stages, item.passed) for item in result.paths] == [
        ("automatic", ("ranked",), False),
        ("deliberate", ("delivered",), True),
    ]
    # One path that missed is a miss: the automatic path is what a chat gets unasked.
    assert result.passed is False and result.report_digest == "b" * 64
    assert result.content_sha256 == "c" * 64
