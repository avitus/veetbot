"""Milestone 16 memory-benchmark gates."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_core.config import Settings
from agent_core.evals.memory_benchmark import (
    PROBE_CATEGORIES,
    BenchmarkProbe,
    BenchmarkScenario,
    BenchmarkSession,
    BenchmarkTurn,
    DeterministicBenchmarkResult,
    LabeledBelief,
    MemoryBenchmarkCorpus,
    ProbeAnswer,
    ProbeCategory,
    baseline_probe_rows,
    compare_to_baseline,
    load_baseline,
    load_corpus,
)
from agent_core.evals.memory_benchmark_driver import (
    run_deterministic_benchmark,
    run_deterministic_scenario,
)
from agent_core.memory.formation import DeterministicCandidateExtractor
from tests.integration.m2_support import memory_settings

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO_COUNT = 16
_PROBE_CAP = 80
_MINIMUM_CROSS_PROJECT_SCENARIOS = 4
_MINIMUM_PROTECTED_SCENARIOS = 4
_MINIMUM_PROBES_PER_CATEGORY = 3
_BENCHMARK_RUNS: dict[str, DeterministicBenchmarkResult] = {}

_START = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
_ONE_DAY = 24 * 60 * 60
_THREE_DAYS = 3 * _ONE_DAY
_FORTY_DAYS = 40 * _ONE_DAY

_FILLER_SESSIONS = [
    BenchmarkSession(
        id="s01",
        turns=[BenchmarkTurn(text="I prefer concise answers. I live in Seattle.")],
    ),
    BenchmarkSession(
        id="s02",
        advance_seconds=_FORTY_DAYS,
        turns=[BenchmarkTurn(text="I live in Portland now.")],
    ),
]
_FILLER_BELIEFS = [
    LabeledBelief(
        label="pref_concise",
        session="s01",
        belief_type="preference",
        subjects=["answer style"],
        statements=["User prefers concise answers."],
    ),
    LabeledBelief(
        label="home_seattle",
        session="s01",
        belief_type="user_model_attr",
        subjects=["home location"],
        statements=["User lives in Seattle."],
    ),
    LabeledBelief(
        label="home_portland",
        session="s02",
        supersedes="home_seattle",
        belief_type="user_model_attr",
        subjects=["home location"],
        statements=["User lives in Portland."],
    ),
]
_ANSWERABLE: tuple[ProbeCategory, ...] = (
    "single_hop",
    "preference",
    "multi_hop",
    "temporal",
    "update",
    "correction",
)
_PROTECTED: tuple[ProbeCategory, ...] = ("transfer", "abstention")


def _filler_probe(category: ProbeCategory, position: int) -> BenchmarkProbe:
    """Build the smallest probe that satisfies one category's corpus rule."""

    identifier = f"p{position + 1:02d}"
    match category:
        case "multi_hop":
            return BenchmarkProbe(
                id=identifier,
                category=category,
                question="What do you know about me?",
                needed=["pref_concise", "home_portland"],
                answer=ProbeAnswer(kind="all_of", values=["concise", "Portland"]),
            )
        case "update" | "correction":
            return BenchmarkProbe(
                id=identifier,
                category=category,
                question="Where do I live now?",
                needed=["home_portland"],
                answer=ProbeAnswer(kind="alternatives", values=["Portland"]),
                forbidden_statements=["User lives in Seattle."],
            )
        case "transfer":
            return BenchmarkProbe(
                id=identifier,
                category=category,
                question="Which answer style do I prefer?",
                project_scope="beta",
                needed=["pref_concise"],
                answer=ProbeAnswer(kind="alternatives", values=["concise"]),
            )
        case "abstention":
            return BenchmarkProbe(
                id=identifier,
                category=category,
                question="What is the deploy token?",
                answer=ProbeAnswer(kind="abstain"),
                forbidden_statements=["REDACTED-TOKEN"],
            )
        case _:
            return BenchmarkProbe(
                id=identifier,
                category=category,
                question="How do I like my answers formatted?",
                needed=["pref_concise"],
                answer=ProbeAnswer(kind="alternatives", values=["concise"]),
            )


def _corpus() -> MemoryBenchmarkCorpus:
    """The smallest corpus the schema accepts, as the driver's prompt source.

    The deterministic arm sends the bare question, so only the corpus envelope
    matters here; the scenarios exist to satisfy the category and protected
    coverage rules the corpus enforces.  Task 1c replaces this with the corpus
    the repository checks in.
    """

    scenarios = [
        BenchmarkScenario(
            id=f"mb-filler-{index + 1:03d}",
            title="Corpus filler",
            start_at=_START + timedelta(days=index),
            sessions=_FILLER_SESSIONS,
            beliefs=_FILLER_BELIEFS,
            protected_statements=["REDACTED-TOKEN"] if index >= 6 else [],
            probes=[
                _filler_probe(category, position)
                for position, category in enumerate(_ANSWERABLE if index < 6 else _PROTECTED)
            ],
        )
        for index in range(12)
    ]
    return MemoryBenchmarkCorpus(
        probe_instruction="Answer in one line using only what you already know about me.",
        abstain_phrase="I do not have that information.",
        scenarios=scenarios,
    )


def _driven_scenario() -> BenchmarkScenario:
    return BenchmarkScenario(
        id="mb-gate-driver-001",
        title="Home, answer style, and a spouse across two sessions",
        start_at=_START,
        sessions=[
            BenchmarkSession(
                id="s01",
                turns=[
                    BenchmarkTurn(text="Hi! Quick context before we start. I live in Seattle."),
                    BenchmarkTurn(
                        text="By the way, I prefer concise answers.", advance_seconds=120
                    ),
                ],
            ),
            BenchmarkSession(
                id="s02",
                advance_seconds=_THREE_DAYS,
                turns=[BenchmarkTurn(text="My wife is Morgan.")],
            ),
        ],
        beliefs=[
            LabeledBelief(
                label="home_seattle",
                session="s01",
                belief_type="user_model_attr",
                subjects=["home location"],
                statements=["User lives in Seattle."],
            ),
            LabeledBelief(
                label="pref_concise",
                session="s01",
                belief_type="preference",
                subjects=["answer style"],
                statements=["User prefers concise answers."],
            ),
            LabeledBelief(
                label="wife_morgan",
                session="s02",
                belief_type="relationship",
                subjects=["wife"],
                statements=["User's wife is Morgan."],
            ),
        ],
        probes=[
            BenchmarkProbe(
                id="p01",
                category="single_hop",
                advance_seconds=_ONE_DAY,
                question="Where do I live?",
                needed=["home_seattle"],
                answer=ProbeAnswer(kind="alternatives", values=["Seattle"]),
            ),
            BenchmarkProbe(
                id="p02",
                category="preference",
                question="How do I like my answers formatted?",
                needed=["pref_concise"],
                answer=ProbeAnswer(kind="alternatives", values=["concise"]),
            ),
        ],
    )


async def test_bench_single_scenario_forms_and_recalls(tmp_path: Path) -> None:
    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")

    result = await run_deterministic_scenario(settings, _driven_scenario(), corpus=_corpus())

    assert result.scenario_id == "mb-gate-driver-001"
    assert result.extractor_name == DeterministicCandidateExtractor.name
    assert [counts.session_id for counts in result.consolidations] == ["s01", "s02"]
    assert [counts.scope for counts in result.consolidations] == ["general", "general"]
    assert [counts.committed for counts in result.consolidations] == [2, 1]
    assert result.formation.expected == 3
    assert result.formation.supported == 3
    assert result.formation.fabricated == 0
    assert result.formation.policy_failures == 0
    assert {belief.statement for belief in result.beliefs} == {
        "User lives in Seattle.",
        "User prefers concise answers.",
        "User's wife is Morgan.",
    }
    assert [probe.probe_id for probe in result.probes] == ["p01", "p02"]
    for probe in result.probes:
        assert probe.run_completed
        assert probe.snapshot_trace_id is not None
        assert probe.needed_total == 1
        assert probe.needed_formed == 1
        assert probe.needed_recalled == 1
        assert probe.distinct_prefixes == 1
        assert probe.policy_failures == 0
        assert probe.forbidden_rendered == 0


@pytest.fixture(scope="module")
def benchmark_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings every gate in this file shares, over one scratch artifact root."""

    return replace(memory_settings(), artifact_root=tmp_path_factory.mktemp("memory-benchmark"))


async def _benchmark(settings: Settings) -> DeterministicBenchmarkResult:
    """Run the checked-in corpus once and serve that run to every gate below.

    The deterministic arm is a pure function of the corpus and the code, so one
    run answers every question the gates ask of it and the whole file costs
    about one benchmark pass; only the reproducibility gate pays for a second.
    """

    if "run" not in _BENCHMARK_RUNS:
        _BENCHMARK_RUNS["run"] = await run_deterministic_benchmark(
            _REPOSITORY_ROOT, settings=settings
        )
    return _BENCHMARK_RUNS["run"]


def test_bench_corpus_shape() -> None:
    corpus, digest = load_corpus(_REPOSITORY_ROOT)

    assert len(digest) == 64
    assert len(corpus.scenarios) == _SCENARIO_COUNT
    assert len({scenario.start_at for scenario in corpus.scenarios}) == _SCENARIO_COUNT
    probes = [probe for scenario in corpus.scenarios for probe in scenario.probes]
    assert len(probes) <= _PROBE_CAP
    for scenario in corpus.scenarios:
        assert len(scenario.sessions) >= 2
        assert 3 <= len(scenario.probes) <= 5
    counts = Counter(probe.category for probe in probes)
    thin = [
        category for category in PROBE_CATEGORIES if counts[category] < _MINIMUM_PROBES_PER_CATEGORY
    ]
    assert thin == []
    protected = [
        scenario
        for scenario in corpus.scenarios
        if scenario.protected_statements
        and any(probe.category == "abstention" for probe in scenario.probes)
    ]
    assert len(protected) >= _MINIMUM_PROTECTED_SCENARIOS
    cross_project = [
        scenario
        for scenario in corpus.scenarios
        if len({session.project_scope for session in scenario.sessions} - {None}) >= 2
    ]
    assert len(cross_project) >= _MINIMUM_CROSS_PROJECT_SCENARIOS


async def test_bench_deterministic_arm_is_reproducible(benchmark_settings: Settings) -> None:
    first = await _benchmark(benchmark_settings)

    second = await run_deterministic_benchmark(_REPOSITORY_ROOT, settings=benchmark_settings)

    assert second.metrics == first.metrics
    assert baseline_probe_rows(second) == baseline_probe_rows(first)
    assert second.corpus_sha256 == first.corpus_sha256
    assert second.extractor_name == first.extractor_name
    assert first.metrics.max_distinct_prefixes_per_probe == 1
    assert first.metrics.probe_runs_completed == first.metrics.probe_count
    assert first.metrics.run_policy_failures == 0


async def test_bench_deterministic_metrics_do_not_regress_baseline(
    benchmark_settings: Settings,
) -> None:
    result = await _benchmark(benchmark_settings)
    baseline = load_baseline(_REPOSITORY_ROOT)
    assert baseline is not None

    comparison = compare_to_baseline(result, baseline)

    assert comparison.drift == []
    assert comparison.regressions == []


async def test_bench_baseline_matches_current_run_exactly(benchmark_settings: Settings) -> None:
    result = await _benchmark(benchmark_settings)
    baseline = load_baseline(_REPOSITORY_ROOT)
    assert baseline is not None

    assert result.metrics == baseline.metrics
    assert baseline_probe_rows(result) == baseline.probes
    assert result.benchmark_version == baseline.benchmark_version
    assert result.corpus_sha256 == baseline.corpus_sha256
    assert result.formation_policy_version == baseline.formation_policy_version
    assert result.provider_formation_policy_version == baseline.provider_formation_policy_version
    assert result.retrieval_policy_version == baseline.retrieval_policy_version
    assert result.extractor_name == baseline.extractor_name
    assert compare_to_baseline(result, baseline).improvements == []


async def test_bench_protected_content_never_forms_or_renders(
    benchmark_settings: Settings,
) -> None:
    corpus, _digest = load_corpus(_REPOSITORY_ROOT)
    result = await _benchmark(benchmark_settings)

    assert result.metrics.formation_policy_failures == 0
    assert result.metrics.abstention_leaks == 0
    protected = {
        scenario.id: [fragment.casefold() for fragment in scenario.protected_statements]
        for scenario in corpus.scenarios
    }
    for scenario_result in result.scenarios:
        fragments = protected[scenario_result.scenario_id]
        for belief in scenario_result.beliefs:
            statement = belief.statement.casefold()
            assert [fragment for fragment in fragments if fragment in statement] == []
        for probe in scenario_result.probes:
            if probe.category == "abstention":
                assert probe.forbidden_rendered == 0


async def test_bench_formed_updates_never_render_superseded(benchmark_settings: Settings) -> None:
    result = await _benchmark(benchmark_settings)

    assert result.metrics.currency_violations == 0
    corrections = [
        probe
        for scenario in result.scenarios
        for probe in scenario.probes
        if probe.category in {"update", "correction"}
    ]
    assert len(corrections) >= 2 * _MINIMUM_PROBES_PER_CATEGORY
    for probe in corrections:
        assert probe.currency_violations == 0
