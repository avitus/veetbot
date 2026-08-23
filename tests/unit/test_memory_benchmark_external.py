"""External benchmark adapters: LongMemEval, LoCoMo, and HaluMem.

Every fixture under `tests/fixtures/memory_benchmark_external/` is invented for
these tests and shaped like the dataset it stands in for.  No dataset file, and
no derivative of one, is ever committed to this repository.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent_core.evals import memory_benchmark_external
from agent_core.evals.memory_benchmark import (
    BenchmarkProbe,
    BenchmarkScenario,
    BenchmarkSession,
    BenchmarkTurn,
    EvidenceRef,
    LabeledBelief,
    MemoryBenchmarkCorpus,
    ProbeAnswer,
    SessionEvents,
    evidence_provenance_recalled,
    score_answer_f1,
)
from agent_core.evals.memory_benchmark_driver import run_deterministic_scenario
from agent_core.evals.memory_benchmark_external import (
    DATASET_CAVEATS,
    DATASET_LICENSES,
    LABEL_FREE_CAVEAT,
    NEVER_COMMIT_GLOBS,
    ExternalDataset,
    MemoryBenchmarkExternalResult,
    load_halumem,
    load_locomo,
    load_longmemeval,
)
from tests.contract.memory_fixtures import memory, recalled, trace
from tests.integration.m2_support import memory_settings

_REPOSITORY = Path(__file__).resolve().parents[2]
_FIXTURES = _REPOSITORY / "tests" / "fixtures" / "memory_benchmark_external"
_LONGMEMEVAL = _FIXTURES / "synthetic-longmemeval.json"
_LOCOMO = _FIXTURES / "synthetic-locomo.json"
_HALUMEM = _FIXTURES / "synthetic-halumem.json"

_SKIPPED_DIRECTORIES = frozenset(
    {".git", ".venv", "venv", "node_modules", "site", "dist", "build", "__pycache__", ".mypy_cache"}
)


def _repository_files() -> Iterator[Path]:
    """Walk the working tree, skipping generated and vendored directories."""

    stack = [_REPOSITORY]
    while stack:
        for entry in stack.pop().iterdir():
            if entry.is_dir():
                if entry.name not in _SKIPPED_DIRECTORIES:
                    stack.append(entry)
            else:
                yield entry


def _probe_of(scenario: BenchmarkScenario, probe_id: str) -> BenchmarkProbe:
    return next(probe for probe in scenario.probes if probe.id == probe_id)


def test_adapters_round_trip_synthetic_fixtures() -> None:
    """The gate: each loader maps its dataset's shape, and none is vendored."""

    longmemeval = load_longmemeval(_LONGMEMEVAL, sample=None, seed=1)
    locomo = load_locomo(_LOCOMO, principal_speaker="a", sample=None, seed=1)
    halumem = load_halumem(_HALUMEM, sample=None, seed=1)

    # LongMemEval: one instance is one scenario, dated by its haystack dates.
    assert [scenario.id for scenario in longmemeval] == [
        "mb-longmemeval-syn-user-01-001",
        "mb-longmemeval-syn-multi-02-002",
        "mb-longmemeval-syn-temporal-03-003",
        "mb-longmemeval-syn-update-04-004",
        "mb-longmemeval-syn-pref-05-005",
        "mb-longmemeval-syn-assistant-06-006",
        "mb-longmemeval-syn-abstain-07-007",
    ]
    first = longmemeval[0]
    assert first.start_at == datetime(2024, 3, 1, 10, 0, tzinfo=UTC)
    assert [session.id for session in first.sessions] == ["s01", "s02"]
    assert [session.advance_seconds for session in first.sessions] == [0, 376200]
    assert [turn.role for turn in first.sessions[0].turns] == ["user", "assistant"]
    assert first.beliefs == []
    probe = _probe_of(first, "p01")
    assert probe.category == "single_hop"
    assert probe.source_dataset == "longmemeval"
    assert probe.source_category == "single-session-user"
    assert probe.advance_seconds == 1089900
    assert probe.needed == []
    assert probe.answer == ProbeAnswer(kind="alternatives", values=["Bubbles"])
    assert probe.evidence == [EvidenceRef(session_id="s01", turn_index=0)]
    assert not probe.excluded_by_design

    categories = {
        scenario.id.rsplit("-", 1)[0]: _probe_of(scenario, "p01").category
        for scenario in longmemeval
    }
    assert categories == {
        "mb-longmemeval-syn-user-01": "single_hop",
        "mb-longmemeval-syn-multi-02": "multi_hop",
        "mb-longmemeval-syn-temporal-03": "temporal",
        "mb-longmemeval-syn-update-04": "update",
        "mb-longmemeval-syn-pref-05": "preference",
        "mb-longmemeval-syn-assistant-06": "single_hop",
        "mb-longmemeval-syn-abstain-07": "abstention",
    }
    assert _probe_of(longmemeval[5], "p01").excluded_by_design
    assert _probe_of(longmemeval[1], "p01").evidence == [
        EvidenceRef(session_id="s01", turn_index=0),
        EvidenceRef(session_id="s03", turn_index=0),
    ]
    abstention = _probe_of(longmemeval[6], "p01")
    assert abstention.answer == ProbeAnswer(kind="abstain", values=[])
    assert abstention.evidence == []

    # LoCoMo: the principal speaker's turns are the only formation sources.
    assert [scenario.id for scenario in locomo] == [
        "mb-locomo-syn-conv-1-001",
        "mb-locomo-syn-conv-2-002",
    ]
    conversation = locomo[0]
    assert conversation.start_at == datetime(2024, 5, 4, 10, 15, tzinfo=UTC)
    assert [session.advance_seconds for session in conversation.sessions] == [0, 638100]
    assert [turn.role for turn in conversation.sessions[0].turns] == [
        "user",
        "assistant",
        "user",
    ]
    assert [(probe.category, probe.source_category) for probe in conversation.probes] == [
        ("single_hop", "4"),
        ("multi_hop", "1"),
        ("temporal", "2"),
        ("abstention", "5"),
    ]
    assert _probe_of(conversation, "p02").evidence == [
        EvidenceRef(session_id="s01", turn_index=2),
        EvidenceRef(session_id="s02", turn_index=1),
    ]
    assert _probe_of(conversation, "p01").answer == ProbeAnswer(
        kind="alternatives", values=["Marmalade"]
    )
    assert _probe_of(conversation, "p04").answer == ProbeAnswer(kind="abstain", values=[])

    # HaluMem: memory points are labeled beliefs and updates are supersessions.
    assert [scenario.id for scenario in halumem] == [
        "mb-halumem-syn-user-1-001",
        "mb-halumem-syn-user-2-002",
    ]
    user = halumem[0]
    assert user.start_at == datetime(2024, 4, 2, 9, 0, tzinfo=UTC)
    assert [session.advance_seconds for session in user.sessions] == [0, 1503000]
    assert [turn.role for turn in user.sessions[0].turns] == ["user", "assistant", "user"]
    assert [
        (belief.label, belief.belief_type, belief.session, belief.supersedes)
        for belief in user.beliefs
    ] == [
        ("m001", "user_model_attr", "s01", None),
        ("m002", "relationship", "s01", None),
        ("m003", "user_model_attr", "s02", "m001"),
    ]
    assert user.beliefs[2].statements == ["The user restores pianos."]
    assert [(probe.id, probe.category) for probe in user.probes] == [
        ("p01", "update"),
        ("p02", "single_hop"),
    ]

    # Every adapter names its license, and no dataset file is in the tree.
    assert set(DATASET_LICENSES) == {"longmemeval", "locomo", "halumem"}
    assert DATASET_LICENSES["locomo"].license == "CC BY-NC 4.0"
    ignored = (_REPOSITORY / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert set(NEVER_COMMIT_GLOBS) <= set(ignored)
    vendored = sorted(
        str(path.relative_to(_REPOSITORY))
        for path in _repository_files()
        if any(path.match(glob) for glob in NEVER_COMMIT_GLOBS)
    )
    assert vendored == []


def test_longmemeval_sampling_is_deterministic_and_stratified() -> None:
    """One seed always draws the same instances, spread across question types."""

    everything = load_longmemeval(_LONGMEMEVAL, sample=None, seed=11)
    sampled = load_longmemeval(_LONGMEMEVAL, sample=4, seed=11)
    again = load_longmemeval(_LONGMEMEVAL, sample=4, seed=11)

    assert len(everything) == 7
    assert [scenario.id for scenario in sampled] == [scenario.id for scenario in again]
    assert len(sampled) == 4
    assert {scenario.id for scenario in sampled} <= {scenario.id for scenario in everything}
    types = {_probe_of(scenario, "p01").source_category for scenario in sampled}
    assert len(types) == 4
    assert len(load_longmemeval(_LONGMEMEVAL, sample=99, seed=11)) == 7


def test_locomo_only_keeps_qa_whose_evidence_is_principal_turns() -> None:
    """A question is scored only when every dia_id it cites is a source turn."""

    speaker_a = load_locomo(_LOCOMO, principal_speaker="a", sample=None, seed=3)
    speaker_b = load_locomo(_LOCOMO, principal_speaker="b", sample=None, seed=3)

    assert [probe.question for probe in speaker_a[0].probes] == [
        "What did Ana name her cat?",
        "What did the cat knock over, and where does she nap?",
        "How long after adopting the cat did the fern fall?",
        "Which breed of dog does Ana walk on Sundays?",
    ]
    assert [probe.question for probe in speaker_b[0].probes] == [
        "Which event did Bo sign up for?",
        "Which breed of dog does Ana walk on Sundays?",
    ]
    assert [turn.role for turn in speaker_b[0].sessions[0].turns] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert [probe.question for probe in speaker_a[1].probes] == [
        "Where did Cyd start keeping bees?"
    ]


def test_halumem_memory_points_become_labeled_beliefs_with_updates() -> None:
    """Nested memory points map too, and a same-session update is not a supersession."""

    scenarios = load_halumem(_HALUMEM, sample=None, seed=5)

    nested = scenarios[1]
    assert [
        (belief.label, belief.belief_type, belief.session, belief.supersedes)
        for belief in nested.beliefs
    ] == [
        ("m001", "fact", "s01", None),
        ("m002", "fact", "s01", None),
        ("m003", "user_model_attr", "s02", None),
    ]
    assert [belief.subjects for belief in nested.beliefs] == [["user"], ["user"], ["user"]]
    assert nested.beliefs[1].statements == ["The user draws inland charts."]


def test_evidence_provenance_recall_counts() -> None:
    """A probe is evidence-recalled only when a returned belief came from its turn."""

    evidence_session = uuid4()
    other_session = uuid4()
    session_events = {
        "s01": SessionEvents(session_id=evidence_session, turn_sequences=[1, 2]),
        "s02": SessionEvents(session_id=other_session, turn_sequences=[1, 2]),
    }
    probe = BenchmarkProbe(
        id="p01",
        category="single_hop",
        question="Where do I live?",
        answer=ProbeAnswer(kind="alternatives", values=["Seattle"]),
        source_dataset="longmemeval",
        source_category="single-session-user",
        evidence=[EvidenceRef(session_id="s01", turn_index=1)],
    )
    returned = trace().model_copy(
        update={
            "beliefs": [recalled(belief_id=601, source_event_ids=[2])],
            "returned": [UUID(int=601)],
        }
    )
    from_evidence = memory(belief_id=601).model_copy(
        update={"source_session_id": evidence_session, "source_event_ids": [2]}
    )
    from_elsewhere = memory(belief_id=601).model_copy(
        update={"source_session_id": other_session, "source_event_ids": [2]}
    )
    wrong_turn = memory(belief_id=601).model_copy(
        update={"source_session_id": evidence_session, "source_event_ids": [1]}
    )

    assert evidence_provenance_recalled(probe, [returned], session_events, store=[from_evidence])
    assert not evidence_provenance_recalled(
        probe, [returned], session_events, store=[from_elsewhere]
    )
    assert not evidence_provenance_recalled(probe, [returned], session_events, store=[wrong_turn])
    assert not evidence_provenance_recalled(probe, [], session_events, store=[from_evidence])

    whole_session = probe.model_copy(
        update={"evidence": [EvidenceRef(session_id="s01", turn_index=None)]}
    )
    assert evidence_provenance_recalled(
        whole_session, [returned], session_events, store=[wrong_turn]
    )

    unlabeled = probe.model_copy(update={"evidence": []})
    assert not evidence_provenance_recalled(
        unlabeled, [returned], session_events, store=[from_evidence]
    )


def test_score_answer_f1() -> None:
    """Answers are scored by normalized token F1, taking the best gold value."""

    gold = ProbeAnswer(kind="alternatives", values=["Bubbles"])

    assert score_answer_f1("Bubbles", gold) == Decimal("1.0000")
    assert score_answer_f1("bubbles.", gold) == Decimal("1.0000")
    assert score_answer_f1("The starter is called Bubbles.", gold) == Decimal("0.4000")
    assert score_answer_f1("Ridgeway", gold) == Decimal("0.0000")
    assert score_answer_f1(None, gold) == Decimal("0.0000")
    assert score_answer_f1("", gold) == Decimal("0.0000")
    assert score_answer_f1(
        "the pantry", ProbeAnswer(kind="alternatives", values=["the hallway", "a pantry"])
    ) == Decimal("1.0000")
    assert score_answer_f1("anything", ProbeAnswer(kind="abstain", values=[])) == Decimal("0.0000")


def test_dataset_probes_carry_evidence_instead_of_corpus_labels() -> None:
    """The category rules bind authored probes; a dataset probe answers to its own."""

    session = BenchmarkSession(id="s01", turns=[BenchmarkTurn(text="I live in Seattle.")])
    later = BenchmarkSession(id="s02", turns=[BenchmarkTurn(text="My wife is Morgan.")])
    dataset_probe = BenchmarkProbe(
        id="p01",
        category="multi_hop",
        question="Which two rooms did I repaint?",
        answer=ProbeAnswer(kind="alternatives", values=["the hallway and the pantry"]),
        source_dataset="longmemeval",
        source_category="multi-session",
        evidence=[EvidenceRef(session_id="s01", turn_index=0)],
    )
    scenario = BenchmarkScenario(
        id="mb-longmemeval-syn-001",
        title="A dataset instance",
        start_at=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
        sessions=[session, later],
        beliefs=[],
        probes=[dataset_probe],
    )

    assert scenario.probes[0].category == "multi_hop"

    with pytest.raises(ValidationError, match="names no corpus label"):
        BenchmarkScenario(
            id="mb-longmemeval-syn-002",
            title="A dataset instance naming a corpus label",
            start_at=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            sessions=[session, later],
            beliefs=[
                LabeledBelief(
                    label="home_seattle",
                    session="s01",
                    belief_type="user_model_attr",
                    subjects=["home location"],
                    statements=["User lives in Seattle."],
                )
            ],
            probes=[dataset_probe.model_copy(update={"needed": ["home_seattle"]})],
        )


def test_authored_corpus_probes_carry_no_dataset_provenance() -> None:
    """The checked-in corpus is authored; dataset provenance may not leak into it."""

    corpus = MemoryBenchmarkCorpus.model_validate_json(
        (_REPOSITORY / "evals" / "capability" / "memory-benchmark.v1.json").read_bytes()
    )
    scenarios = list(corpus.scenarios)
    position, tagged = next(
        (index, scenario)
        for index, scenario in enumerate(scenarios)
        if any(probe.category == "abstention" for probe in scenario.probes)
    )
    scenarios[position] = tagged.model_copy(
        update={
            "probes": [
                probe.model_copy(update={"source_dataset": "locomo"})
                if probe.category == "abstention"
                else probe
                for probe in tagged.probes
            ]
        }
    )

    with pytest.raises(ValidationError, match="dataset provenance"):
        MemoryBenchmarkCorpus(
            probe_instruction=corpus.probe_instruction,
            abstain_phrase=corpus.abstain_phrase,
            scenarios=scenarios,
        )


async def test_assistant_turns_never_form_and_evidence_provenance_is_counted(
    tmp_path: Path,
) -> None:
    """The driver replays assistant turns as non-source events and scores evidence."""

    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")
    scenario = BenchmarkScenario(
        id="mb-longmemeval-driver-001",
        title="One dataset-shaped instance driven end to end",
        start_at=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
        sessions=[
            BenchmarkSession(
                id="s01",
                turns=[
                    BenchmarkTurn(text="Hi! Quick context before we start. I live in Seattle."),
                    BenchmarkTurn(
                        text="Seattle is a fine city to live in.",
                        role="assistant",
                        advance_seconds=60,
                    ),
                ],
            ),
            BenchmarkSession(
                id="s02",
                advance_seconds=3 * 24 * 60 * 60,
                turns=[BenchmarkTurn(text="The ferry timetable changed again.")],
            ),
        ],
        beliefs=[],
        probes=[
            BenchmarkProbe(
                id="p01",
                category="single_hop",
                question="Where do I live?",
                answer=ProbeAnswer(kind="alternatives", values=["Seattle"]),
                source_dataset="longmemeval",
                source_category="single-session-user",
                evidence=[EvidenceRef(session_id="s01", turn_index=0)],
            )
        ],
    )
    session_events: dict[str, SessionEvents] = {}

    result = await run_deterministic_scenario(
        settings,
        scenario,
        corpus=_corpus_for_prompting(),
        session_events=session_events,
    )

    # Opening a session appends its own event, so a turn's sequence starts at two.
    assert [events.turn_sequences for events in session_events.values()] == [[2, 3], [2]]
    assert not any("fine city" in belief.statement for belief in result.beliefs)
    assert any("Seattle" in belief.statement for belief in result.beliefs)
    assert [probe.evidence_total for probe in result.probes] == [1]
    assert [probe.evidence_recalled for probe in result.probes] == [1]


async def test_run_external_benchmark_publishes_metrics_without_dataset_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The results file names the dataset and its digest, and carries nothing of it."""

    monkeypatch.setattr(memory_benchmark_external, "load_settings", memory_settings)
    output = tmp_path / "longmemeval-metrics.json"

    async def run() -> MemoryBenchmarkExternalResult | None:
        return await memory_benchmark_external.run_external_benchmark(
            _REPOSITORY,
            dataset="longmemeval",
            path=_LONGMEMEVAL,
            sample=2,
            seed=5,
            principal_speaker="a",
            deterministic_only=True,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

    result = await run()

    assert result is not None
    assert (result.dataset, result.license) == ("longmemeval", "MIT")
    assert result.source_sha256 == hashlib.sha256(_LONGMEMEVAL.read_bytes()).hexdigest()
    assert (result.sample, result.seed) == (2, 5)
    assert result.principal_speaker is None
    assert result.deterministic.probe_count == 1
    assert result.evidence_total == 1
    assert result.excluded_by_design is not None
    assert result.excluded_by_design.probe_count == 1
    assert result.excluded_by_design.expected_beliefs == 0
    assert result.live is None
    assert result.caveats

    published = output.read_text(encoding="utf-8")
    assert MemoryBenchmarkExternalResult.model_validate_json(published) == result
    assert "Bubbles" not in published
    assert str(_LONGMEMEVAL) not in published
    with pytest.raises(ValueError, match="refusing to overwrite"):
        await run()


def _write(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_dataset_records_with_only_empty_turns_are_refused_by_name(tmp_path: Path) -> None:
    longmemeval = _write(
        tmp_path / "empty-longmemeval.json",
        [
            {
                "question_id": "empty-long",
                "question_type": "single-session-user",
                "question": "What was said?",
                "answer": "Nothing",
                "haystack_dates": ["2024/01/01 (Mon) 10:00"],
                "haystack_sessions": [[{"role": "user", "content": "   "}]],
            }
        ],
    )
    locomo = _write(
        tmp_path / "empty-locomo.json",
        [
            {
                "sample_id": "empty-locomo",
                "conversation": {
                    "speaker_a": "Ana",
                    "speaker_b": "Bo",
                    "session_1": [{"speaker": "Ana", "dia_id": "D1", "text": "   "}],
                    "session_1_date_time": "10:00 am on 4 May, 2024",
                },
                "qa": [],
            }
        ],
    )
    halumem = _write(
        tmp_path / "empty-halumem.json",
        [
            {
                "uuid": "empty-halu",
                "sessions": [
                    {
                        "session_id": "empty-halu-s1",
                        "timestamp": "2024-01-01 10:00:00",
                        "dialogue": [{"role": "user", "content": "   "}],
                    }
                ],
                "questions": [{"question": "What was said?", "answer": "Nothing"}],
            }
        ],
    )

    calls: tuple[tuple[Callable[[], list[BenchmarkScenario]], str], ...] = (
        (lambda: load_longmemeval(longmemeval), "longmemeval instance empty-long"),
        (
            lambda: load_locomo(locomo, principal_speaker="a"),
            "locomo conversation empty-locomo",
        ),
        (lambda: load_halumem(halumem), "halumem user empty-halu"),
    )
    for load, where in calls:
        with pytest.raises(ValueError, match="no non-empty session") as caught:
            load()
        assert where in str(caught.value)


def test_blank_non_abstention_answers_are_omitted_without_aborting_records(
    tmp_path: Path,
) -> None:
    longmemeval = _write(
        tmp_path / "blank-answer-longmemeval.json",
        [
            {
                "question_id": f"long-{answer or 'blank'}",
                "question_type": "single-session-user",
                "question": "What did the user name?",
                "answer": answer,
                "haystack_dates": [
                    "2024/01/01 (Mon) 10:00",
                    "2024/01/02 (Tue) 10:00",
                ],
                "haystack_sessions": [
                    [{"role": "user", "content": "A named thing."}],
                    [{"role": "assistant", "content": "Acknowledged."}],
                ],
            }
            for answer in ("Thing", "   ")
        ],
    )
    locomo = _write(
        tmp_path / "blank-answer-locomo.json",
        [
            {
                "sample_id": "locomo-answers",
                "conversation": {
                    "speaker_a": "Ana",
                    "speaker_b": "Bo",
                    "session_1": [{"speaker": "Ana", "dia_id": "D1", "text": "A named thing."}],
                    "session_1_date_time": "10:00 am on 4 May, 2024",
                    "session_2": [{"speaker": "Bo", "dia_id": "D2", "text": "Acknowledged."}],
                    "session_2_date_time": "10:00 am on 5 May, 2024",
                },
                "qa": [
                    {
                        "question": "What did Ana name?",
                        "answer": answer,
                        "evidence": ["D1"],
                        "category": 4,
                    }
                    for answer in ("Thing", None)
                ],
            }
        ],
    )
    halumem_answers: tuple[object, ...] = ("Thing", [])
    halumem = _write(
        tmp_path / "blank-answer-halumem.json",
        [
            {
                "uuid": "halu-answers",
                "sessions": [
                    {
                        "session_id": "halu-answers-s1",
                        "timestamp": "2024-01-01 10:00:00",
                        "dialogue": [{"role": "user", "content": "A named thing."}],
                    },
                    {
                        "session_id": "halu-answers-s2",
                        "timestamp": "2024-01-02 10:00:00",
                        "dialogue": [{"role": "assistant", "content": "Acknowledged."}],
                    },
                ],
                "questions": [
                    {"question": "What did the user name?", "answer": answer}
                    for answer in halumem_answers
                ],
            }
        ],
    )

    long_scenarios = load_longmemeval(longmemeval)
    locomo_scenarios = load_locomo(locomo, principal_speaker="a")
    halumem_scenarios = load_halumem(halumem)

    assert len(long_scenarios) == 1
    assert [probe.answer.values for probe in locomo_scenarios[0].probes] == [["Thing"]]
    assert [probe.answer.values for probe in halumem_scenarios[0].probes] == [["Thing"]]


def test_blank_answers_are_filtered_before_probe_capping(tmp_path: Path) -> None:
    locomo_answers: list[object] = ["Thing", None, None, None, None, None, None]
    locomo = _write(
        tmp_path / "cap-after-filter-locomo.json",
        [
            {
                "sample_id": "locomo-cap",
                "conversation": {
                    "speaker_a": "Ana",
                    "speaker_b": "Bo",
                    "session_1": [{"speaker": "Ana", "dia_id": "D1", "text": "A named thing."}],
                    "session_1_date_time": "10:00 am on 4 May, 2024",
                    "session_2": [{"speaker": "Bo", "dia_id": "D2", "text": "Acknowledged."}],
                    "session_2_date_time": "10:00 am on 5 May, 2024",
                },
                "qa": [
                    {
                        "question": f"Question {index}",
                        "answer": answer,
                        "evidence": ["D1"],
                        "category": 4,
                    }
                    for index, answer in enumerate(locomo_answers)
                ],
            }
        ],
    )
    halumem_answers: list[object] = [None, None, None, None, "Thing", None, None]
    halumem = _write(
        tmp_path / "cap-after-filter-halumem.json",
        [
            {
                "uuid": "halu-cap",
                "sessions": [
                    {
                        "session_id": "halu-cap-s1",
                        "timestamp": "2024-01-01 10:00:00",
                        "dialogue": [{"role": "user", "content": "A named thing."}],
                    },
                    {
                        "session_id": "halu-cap-s2",
                        "timestamp": "2024-01-02 10:00:00",
                        "dialogue": [{"role": "assistant", "content": "Acknowledged."}],
                    },
                ],
                "questions": [
                    {"question": f"Question {index}", "answer": answer}
                    for index, answer in enumerate(halumem_answers)
                ],
            }
        ],
    )

    [locomo_scenario] = load_locomo(locomo, principal_speaker="a", seed=0)
    [halumem_scenario] = load_halumem(halumem, seed=0)

    assert [probe.answer.values for probe in locomo_scenario.probes] == [["Thing"]]
    assert [probe.answer.values for probe in halumem_scenario.probes] == [["Thing"]]
    assert [probe.id for probe in locomo_scenario.probes] == ["p01"]
    assert [probe.id for probe in halumem_scenario.probes] == ["p01"]


def _oversized_longmemeval() -> list[dict[str, object]]:
    return [
        {
            "question_id": "syn_big_01",
            "question_type": "single-session-user",
            "question": "Which week did I repot the fig?",
            "answer": "the third",
            "question_date": "2024/12/31 (Tue) 09:00",
            "haystack_session_ids": [f"syn_big_{index}" for index in range(101)],
            "haystack_dates": [
                f"2024/01/01 (Mon) {index // 60:02d}:{index % 60:02d}" for index in range(101)
            ],
            "haystack_sessions": [
                [{"role": "user", "content": f"Note {index}."}] for index in range(101)
            ],
            "answer_session_ids": ["syn_big_0"],
        }
    ]


def _oversized_locomo() -> list[dict[str, object]]:
    conversation: dict[str, object] = {"speaker_a": "Ana", "speaker_b": "Bo"}
    for number in range(1, 102):
        conversation[f"session_{number}"] = [
            {"speaker": "Ana", "dia_id": f"D{number}:1", "text": f"Note {number}."}
        ]
        conversation[f"session_{number}_date_time"] = f"10:{number % 60:02d} am on 4 May, 2024"
    return [
        {
            "sample_id": "syn-big-1",
            "conversation": conversation,
            "qa": [
                {
                    "question": "What did Ana note first?",
                    "answer": "Note 1",
                    "evidence": ["D1:1"],
                    "category": 4,
                }
            ],
        }
    ]


def _oversized_halumem() -> list[dict[str, object]]:
    return [
        {
            "uuid": "syn-big-user",
            "sessions": [
                {
                    "session_id": f"syn-big-user-s{index}",
                    "timestamp": f"2024-01-01 {index // 60:02d}:{index % 60:02d}:00",
                    "dialogue": [{"role": "user", "content": f"Note {index}."}],
                }
                for index in range(101)
            ],
            "memory_points": [],
            "questions": [{"question": "What did the user note?", "answer": "notes"}],
        }
    ]


def test_an_instance_with_too_many_sessions_is_refused_by_name(tmp_path: Path) -> None:
    """A dataset variant a scenario cannot name is refused, and says which and why."""

    longmemeval = _write(tmp_path / "big-longmemeval.json", _oversized_longmemeval())
    locomo = _write(tmp_path / "big-locomo.json", _oversized_locomo())
    halumem = _write(tmp_path / "big-halumem.json", _oversized_halumem())

    with pytest.raises(ValueError, match="more than the 99 sessions") as longmemeval_error:
        load_longmemeval(longmemeval, sample=None, seed=1)
    with pytest.raises(ValueError, match="more than the 99 sessions") as locomo_error:
        load_locomo(locomo, principal_speaker="a", sample=None, seed=1)
    with pytest.raises(ValueError, match="more than the 99 sessions") as halumem_error:
        load_halumem(halumem, sample=None, seed=1)

    for caught, named in (
        (longmemeval_error, "longmemeval instance syn_big_01"),
        (locomo_error, "locomo conversation syn-big-1"),
        (halumem_error, "halumem user syn-big-user"),
    ):
        assert not isinstance(caught.value, ValidationError)
        assert named in str(caught.value)
        assert "run a smaller variant" in str(caught.value)


async def test_label_free_datasets_publish_the_provenance_recall_caveat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without corpus labels the needed and noise counts are undefined, and say so."""

    monkeypatch.setattr(memory_benchmark_external, "load_settings", memory_settings)

    label_free: tuple[tuple[ExternalDataset, Path], ...] = (
        ("longmemeval", _LONGMEMEVAL),
        ("locomo", _LOCOMO),
        ("halumem", _HALUMEM),
    )
    for dataset, source in label_free:
        output = tmp_path / f"{dataset}-metrics.json"
        result = await memory_benchmark_external.run_external_benchmark(
            _REPOSITORY,
            dataset=dataset,
            path=source,
            sample=1,
            seed=5,
            principal_speaker="a",
            deterministic_only=True,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )
        assert result is not None
        assert result.deterministic.needed_total == 0
        assert LABEL_FREE_CAVEAT in result.caveats
        assert LABEL_FREE_CAVEAT in output.read_text(encoding="utf-8")

    assert LABEL_FREE_CAVEAT in DATASET_CAVEATS["halumem"]


def _corpus_for_prompting() -> MemoryBenchmarkCorpus:
    """Load the authored corpus, which supplies the probe instruction only."""

    return MemoryBenchmarkCorpus.model_validate_json(
        (_REPOSITORY / "evals" / "capability" / "memory-benchmark.v1.json").read_bytes()
    )
