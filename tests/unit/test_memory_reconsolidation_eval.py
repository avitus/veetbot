"""Adversarial checks for the dreaming yardstick, not its runtime implementation."""

from __future__ import annotations

from pathlib import Path
from shutil import copyfile

import pytest

from agent_core.evals.memory_reconsolidation import (
    CONTROL_PATHS,
    CORPUS_PATH,
    MANIFEST_PATH,
    SCORER_PATHS,
    Answer,
    Case,
    Corpus,
    Hypothesis,
    Observation,
    load_corpus,
    score_case,
    score_split,
)

ROOT = Path(__file__).resolve().parents[2]


def example() -> Case:
    return Case.model_validate(
        {
            "id": "development-equivalent",
            "split": "development",
            "category": "equivalent",
            "seeds": [
                {
                    "id": key,
                    "statement": statement,
                    "subject": "units",
                    "session": key,
                    "event": 1,
                    "evidence_at": "2026-10-01T00:00:00Z",
                }
                for key, statement in (
                    ("a", "Use Celsius."),
                    ("b", "Use Celsius."),
                    ("c", "Use kilometres."),
                )
            ],
            "duplicate_classes": [["a", "b"]],
            "required_survivors": ["a", "b", "c"],
            "hypotheses": [{"statement": "May prefer metric units.", "support": ["a", "c"]}],
            "probes": [
                {
                    "id": "q1",
                    "question": "First unit?",
                    "answer": {"kind": "exact", "values": ["Celsius"]},
                }
            ],
        }
    )


def test_transitive_false_merge_and_lost_fact_are_not_hidden() -> None:
    score = score_case(
        example(),
        Observation(
            case_id="development-equivalent",
            merges=(("a", "b"), ("b", "c")),
            surviving_ids=("a", "b"),
        ),
    )
    assert score.false_merge_pairs == 2
    assert score.lost_facts == 1
    assert score.covered_duplicate_sets == 0


def test_duplicate_hypotheses_cannot_inflate_precision_or_recall() -> None:
    good = Hypothesis(statement="May prefer metric units.", support=("c", "a"))
    wrong = Hypothesis(statement="May NOT prefer metric units.", support=("a", "c"))
    score = score_case(example(), Observation(case_id=example().id, hypotheses=(good, good, wrong)))
    assert (score.hypothesis_labels, score.hypothesis_outputs, score.hypothesis_matches) == (
        1,
        3,
        1,
    )


def test_abstention_missing_answers_and_unknown_ids_do_not_pass() -> None:
    score = score_case(
        example(),
        Observation(case_id=example().id, merges=(("a", "foreign"),), surviving_ids=("foreign",)),
    )
    assert score.invalid_observations > 0
    assert score.false_merge_pairs == 1
    assert score.lost_facts == 3
    assert (score.hypothesis_matches, score.covered_probes) == (0, 0)
    assert (score.hypothesis_labels, score.probes) == (1, 1)


def test_valid_merge_and_exact_answer_preserve_denominators() -> None:
    score = score_case(
        example(),
        Observation(
            case_id=example().id,
            merges=(("a", "b"),),
            surviving_ids=("a", "b", "c"),
            answers=(Answer(probe_id="q1", text="Celsius"),),
        ),
    )
    assert score.false_merge_pairs == score.lost_facts == score.invalid_observations == 0
    assert (score.duplicate_sets, score.covered_duplicate_sets) == (1, 1)
    assert (score.probes, score.covered_probes) == (1, 1)


def test_duplicate_answers_are_invalid_not_extra_coverage() -> None:
    answer = Answer(probe_id="q1", text="Celsius")
    score = score_case(example(), Observation(case_id=example().id, answers=(answer, answer)))
    assert score.invalid_observations == 1
    assert score.covered_probes == 0


def test_wrong_hypothesis_support_does_not_match() -> None:
    wrong = Hypothesis(statement="May prefer metric units.", support=("a", "b"))
    assert (
        score_case(
            example(), Observation(case_id=example().id, hypotheses=(wrong,))
        ).hypothesis_matches
        == 0
    )


def test_frozen_corpus_and_control_references_are_intact() -> None:
    corpus = load_corpus(ROOT)
    assert len(corpus.cases) == 24
    for split in ("development", "holdout"):
        assert len([case for case in corpus.cases if case.split == split]) == 12


def test_split_requires_complete_unique_observations() -> None:
    corpus = load_corpus(ROOT)
    with pytest.raises(ValueError, match="complete"):
        score_split(corpus, [], "holdout")
    observations = [
        Observation(case_id=case.id) for case in corpus.cases if case.split == "holdout"
    ]
    with pytest.raises(ValueError, match="unique"):
        score_split(corpus, [*observations, observations[0]], "holdout")
    score = score_split(corpus, observations, "holdout")
    assert score.hypothesis_labels > 0 and score.hypothesis_matches == 0
    assert score.probes == 12 and score.covered_probes == 0


def test_labels_reject_duplicate_evidence_and_disagreeing_equivalence() -> None:
    raw = example().model_dump()
    raw["seeds"][2]["statement"] = "Use Celsius."
    raw["seeds"][2]["session"] = "a"
    with pytest.raises(ValueError, match="independent"):
        Case.model_validate(raw)
    raw = example().model_dump()
    raw["duplicate_classes"] = [["a", "c"]]
    with pytest.raises(ValueError, match="meaning"):
        Case.model_validate(raw)


def test_case_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="case"):
        score_case(example(), Observation(case_id="another-case"))


def test_case_identifiers_cannot_overlap_splits() -> None:
    corpus = load_corpus(ROOT)
    with pytest.raises(ValueError, match="overlap"):
        Corpus.model_validate({"schema_version": 1, "cases": [*corpus.cases, corpus.cases[0]]})


@pytest.mark.parametrize("changed", [str(CORPUS_PATH), *CONTROL_PATHS, *SCORER_PATHS])
def test_frozen_input_drift_is_refused(tmp_path: Path, changed: str) -> None:
    for path in (str(MANIFEST_PATH), str(CORPUS_PATH), *CONTROL_PATHS, *SCORER_PATHS):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ROOT / path, destination)
    target = tmp_path / changed
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="frozen evaluation input changed"):
        load_corpus(tmp_path)
