from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import (
    BeliefType,
    MemoryRecord,
    MemoryStatus,
    RecalledBelief,
    RecallMoment,
    RecallTrace,
)
from agent_core.domain.runs import FailureReason, RunFailure, RunStatus
from agent_core.evals import memory_benchmark_driver, memory_benchmark_live
from agent_core.evals.memory_benchmark import (
    BASELINE_PATH,
    BENCHMARK_VERSION,
    CORPUS_PATH,
    BenchmarkProbe,
    BenchmarkScenario,
    BenchmarkSession,
    BenchmarkTurn,
    CategoryMetrics,
    DeterministicBenchmarkResult,
    DeterministicScenarioResult,
    FormationMetrics,
    LabeledBelief,
    MemoryBenchmarkBaseline,
    MemoryBenchmarkCorpus,
    ProbeAnswer,
    ProbeCategory,
    ProbeRetrievalResult,
    aggregate_deterministic,
    baseline_probe_rows,
    compare_to_baseline,
    load_baseline,
    load_corpus,
    probe_run_facts,
    ratios,
    score_formation,
    score_probe,
)
from agent_core.evals.memory_benchmark_driver import MemoryBenchmarkResult
from agent_core.evals.memory_benchmark_live import (
    LIVE_COST_CEILING_USD,
    AnswerScore,
    LiveArm,
    LiveProbeArmResult,
    LiveProbeResult,
    MemoryBenchmarkEvidence,
    MemoryBenchmarkLiveMetrics,
    minimum_live_lift,
    score_answer,
)
from agent_core.memory.formation import FORMATION_POLICY_VERSION
from agent_core.memory.provider_extraction import PROVIDER_FORMATION_POLICY_VERSION
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION
from tests.contract.memory_fixtures import memory, recalled, trace
from tests.integration.m2_support import memory_settings

_START = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
_FORTY_DAYS = 40 * 24 * 60 * 60


class _StoppedClock:
    """A Clock that never moves, so a recorded timestamp is assertable."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError("the benchmark's recording clock never sleeps")


class _LiveStandIn(BaseModel):
    """Stand in for the live metrics Task 2 adds, to exercise the validator."""

    probe_count: int = 0


_LIVE_STAND_IN = _LiveStandIn()


def _sessions(
    *,
    first_scope: str | None = None,
    second_scope: str | None = None,
    second_advance: int = _FORTY_DAYS,
) -> list[BenchmarkSession]:
    return [
        BenchmarkSession(
            id="s01",
            project_scope=first_scope,
            turns=[
                BenchmarkTurn(text="I prefer concise answers. I live in Seattle."),
                BenchmarkTurn(text="We decided to deploy on Fridays."),
            ],
        ),
        BenchmarkSession(
            id="s02",
            project_scope=second_scope,
            advance_seconds=second_advance,
            turns=[BenchmarkTurn(text="I live in Portland now.")],
        ),
    ]


def _beliefs() -> list[LabeledBelief]:
    return [
        LabeledBelief(
            label="pref_concise",
            session="s01",
            belief_type="preference",
            subjects=["answer style"],
            statements=["User prefers concise answers."],
        ),
        LabeledBelief(
            label="deploy_fridays",
            session="s01",
            belief_type="fact",
            subjects=["project decision"],
            statements=["The team decided to deploy on Fridays."],
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


def _probe(
    category: ProbeCategory,
    *,
    answer: ProbeAnswer,
    probe_id: str = "p01",
    question: str = "What do you already know about me?",
    needed: Sequence[str] = (),
    forbidden: Sequence[str] = (),
    project_scope: str | None = None,
    advance_seconds: int = 0,
) -> BenchmarkProbe:
    return BenchmarkProbe(
        id=probe_id,
        category=category,
        question=question,
        project_scope=project_scope,
        advance_seconds=advance_seconds,
        needed=list(needed),
        answer=answer,
        forbidden_statements=list(forbidden),
    )


def _valid_probe(category: ProbeCategory, *, probe_id: str = "p01") -> BenchmarkProbe:
    match category:
        case "single_hop" | "preference":
            return _probe(
                category,
                probe_id=probe_id,
                needed=["pref_concise"],
                answer=ProbeAnswer(kind="alternatives", values=["concise"]),
            )
        case "multi_hop":
            return _probe(
                category,
                probe_id=probe_id,
                needed=["pref_concise", "deploy_fridays"],
                answer=ProbeAnswer(kind="all_of", values=["concise", "Fridays"]),
            )
        case "temporal":
            return _probe(
                category,
                probe_id=probe_id,
                needed=["deploy_fridays"],
                answer=ProbeAnswer(kind="alternatives", values=["Fridays"]),
            )
        case "update" | "correction":
            return _probe(
                category,
                probe_id=probe_id,
                needed=["home_portland"],
                forbidden=["User lives in Seattle."],
                answer=ProbeAnswer(kind="alternatives", values=["Portland"]),
            )
        case "transfer":
            return _probe(
                category,
                probe_id=probe_id,
                needed=["pref_concise"],
                project_scope="beta",
                answer=ProbeAnswer(kind="alternatives", values=["concise"]),
            )
        case "abstention":
            return _probe(
                category,
                probe_id=probe_id,
                forbidden=["REDACTED-TOKEN"],
                answer=ProbeAnswer(kind="abstain"),
            )


def _scenario(
    *probes: BenchmarkProbe,
    scenario_id: str = "mb-bench-001",
    sessions: Sequence[BenchmarkSession] | None = None,
    beliefs: Sequence[LabeledBelief] | None = None,
    protected: Sequence[str] = (),
    start_at: datetime = _START,
) -> BenchmarkScenario:
    return BenchmarkScenario(
        id=scenario_id,
        title="Benchmark scenario",
        start_at=start_at,
        sessions=list(_sessions() if sessions is None else sessions),
        beliefs=list(_beliefs() if beliefs is None else beliefs),
        protected_statements=list(protected),
        probes=list(probes),
    )


def test_probe_category_rules_are_enforced() -> None:
    scenario = _scenario(
        _valid_probe("multi_hop"),
        _valid_probe("abstention", probe_id="p02"),
        _valid_probe("update", probe_id="p03"),
        _valid_probe("temporal", probe_id="p04"),
        _valid_probe("transfer", probe_id="p05"),
    )
    assert [probe.id for probe in scenario.probes] == ["p01", "p02", "p03", "p04", "p05"]

    with pytest.raises(ValidationError, match="multi_hop"):
        _scenario(
            _probe(
                "multi_hop",
                needed=["pref_concise"],
                answer=ProbeAnswer(kind="all_of", values=["concise", "Fridays"]),
            )
        )
    with pytest.raises(ValidationError, match="update"):
        _scenario(
            _probe(
                "update",
                needed=["home_portland"],
                answer=ProbeAnswer(kind="alternatives", values=["Portland"]),
            )
        )
    with pytest.raises(ValidationError, match="transfer"):
        _scenario(
            _probe(
                "transfer",
                needed=["pref_concise"],
                answer=ProbeAnswer(kind="alternatives", values=["concise"]),
            )
        )
    with pytest.raises(ValidationError, match="temporal"):
        _scenario(_valid_probe("temporal"), sessions=_sessions(second_advance=0))
    with pytest.raises(ValidationError, match="abstention"):
        _scenario(
            _probe(
                "abstention",
                needed=["pref_concise"],
                forbidden=["REDACTED-TOKEN"],
                answer=ProbeAnswer(kind="abstain"),
            )
        )


_PLAN: tuple[tuple[ProbeCategory, ...], ...] = (
    ("abstention", "single_hop"),
    ("abstention", "single_hop"),
    ("abstention", "single_hop"),
    ("abstention", "multi_hop"),
    ("multi_hop", "multi_hop"),
    ("temporal", "temporal"),
    ("temporal", "update"),
    ("update", "update"),
    ("correction", "correction"),
    ("correction", "preference"),
    ("preference", "preference"),
    ("transfer", "transfer", "transfer"),
)


def _corpus(
    plan: Sequence[Sequence[ProbeCategory]] = _PLAN,
    *,
    protected_indexes: Sequence[int] = (0, 1, 2, 3),
) -> MemoryBenchmarkCorpus:
    scenarios = [
        _scenario(
            *(
                _valid_probe(category, probe_id=f"p{position + 1:02d}")
                for position, category in enumerate(categories)
            ),
            scenario_id=f"mb-bench-{index + 1:03d}",
            protected=["REDACTED-TOKEN"] if index in protected_indexes else (),
            start_at=_START + timedelta(days=index),
        )
        for index, categories in enumerate(plan)
    ]
    return MemoryBenchmarkCorpus(
        probe_instruction="Answer in one line using only what you already know about me.",
        abstain_phrase="I do not have that information.",
        scenarios=scenarios,
    )


def test_corpus_requires_category_and_protected_coverage() -> None:
    corpus = _corpus()

    assert len(corpus.scenarios) == 12
    assert sum(len(scenario.probes) for scenario in corpus.scenarios) == 25

    without_transfer: tuple[tuple[ProbeCategory, ...], ...] = (
        *_PLAN[:-1],
        ("preference", "preference", "preference"),
    )
    with pytest.raises(ValidationError, match="transfer"):
        _corpus(without_transfer)

    with pytest.raises(ValidationError, match="protected"):
        _corpus(protected_indexes=(0, 1, 2))


def _record(
    *,
    belief_id: int,
    subject: str,
    statement: str,
    belief_type: BeliefType = BeliefType.PREFERENCE,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryRecord:
    return memory(belief_id=belief_id, statement=statement).model_copy(
        update={"subject": subject, "belief_type": belief_type, "status": status}
    )


def _recalled_belief(
    *,
    belief_id: int,
    subject: str,
    statement: str,
    belief_type: BeliefType = BeliefType.PREFERENCE,
) -> RecalledBelief:
    return recalled(belief_id=belief_id, statement=statement, subject=subject).model_copy(
        update={"belief_type": belief_type}
    )


def _recall_trace(
    *,
    trace_id: int,
    moment: RecallMoment,
    beliefs: Sequence[RecalledBelief],
    dropped: Sequence[int] = (),
    blocked: Sequence[int] = (),
    rendered: str = "<memory></memory>",
) -> RecallTrace:
    return trace().model_copy(
        update={
            "id": UUID(int=trace_id),
            "moment": moment,
            "beliefs": list(beliefs),
            "returned": [belief.belief_id for belief in beliefs],
            "dropped_for_budget": [UUID(int=value) for value in dropped],
            "blocked": [UUID(int=value) for value in blocked],
            "rendered": rendered,
            "rendered_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        }
    )


_SCORING_NAMES = ("alpha", "bravo", "charlie", "delta")


def _scoring_scenario() -> BenchmarkScenario:
    beliefs = [
        LabeledBelief(
            label=f"pref_{name}",
            session="s01",
            belief_type="preference",
            subjects=["answer style"],
            statements=[f"User prefers {name}."],
        )
        for name in _SCORING_NAMES
    ]
    probe = _probe(
        "multi_hop",
        needed=[f"pref_{name}" for name in _SCORING_NAMES],
        answer=ProbeAnswer(kind="all_of", values=["alpha", "bravo"]),
    )
    return _scenario(probe, beliefs=beliefs)


def test_score_probe_attributes_snapshot_vs_in_turn() -> None:
    scenario = _scoring_scenario()
    probe = scenario.probes[0]
    alpha = _recalled_belief(belief_id=701, subject="answer style", statement="User prefers alpha.")
    bravo = _recalled_belief(belief_id=702, subject="answer style", statement="User prefers bravo.")
    charlie = _recalled_belief(
        belief_id=703, subject="answer style", statement="User prefers charlie."
    )
    snapshot = _recall_trace(
        trace_id=901,
        moment=RecallMoment.SNAPSHOT,
        beliefs=[alpha, charlie],
        dropped=[704],
    )
    in_turn = _recall_trace(
        trace_id=902,
        moment=RecallMoment.IN_TURN,
        beliefs=[bravo, charlie],
        dropped=[705],
        blocked=[702],
    )

    result = score_probe(
        probe,
        scenario,
        store_live=[
            _record(belief_id=701, subject="answer style", statement="User prefers alpha."),
            _record(belief_id=702, subject="answer style", statement="User prefers bravo."),
            _record(belief_id=703, subject="answer style", statement="User prefers charlie."),
        ],
        snapshot=snapshot,
        in_turn=[in_turn],
        distinct_prefixes=1,
        policy_failures=0,
        run_completed=True,
        snapshot_trace_id=snapshot.id,
        in_turn_trace_ids=[in_turn.id],
    )

    assert result.probe_id == "p01"
    assert result.category == "multi_hop"
    assert result.run_completed is True
    assert result.needed_total == 4
    assert result.needed_formed == 3
    assert result.needed_recalled == 3
    assert result.recalled_snapshot_only == 1
    assert result.recalled_in_turn_only == 1
    assert result.recalled_both == 1
    assert result.returned_snapshot == 2
    assert result.returned_in_turn == 2
    assert result.returned_total == 3
    assert result.noise_snapshot == 0
    assert result.noise_in_turn == 0
    assert result.noise_total == 0
    assert result.dropped_for_budget == 2
    assert result.blocked_rendered == 1
    assert result.currency_violations == 0
    assert result.currency_unformed == 0
    assert result.abstention_leaks == 0
    assert result.false_transfers == 0
    assert result.other_forbidden_rendered == 0
    assert result.forbidden_rendered == 0
    assert result.policy_failures == 0
    assert result.distinct_prefixes == 1
    assert result.snapshot_trace_id == snapshot.id
    assert result.in_turn_trace_ids == [in_turn.id]


def test_score_probe_counts_noise_and_forbidden_by_category() -> None:
    noise = _recalled_belief(belief_id=801, subject="hobby", statement="User goes hiking.")
    portland = _recalled_belief(
        belief_id=802,
        subject="home location",
        statement="User lives in Portland.",
        belief_type=BeliefType.USER_MODEL_ATTR,
    )
    update_probe = _valid_probe("update")
    update_scenario = _scenario(update_probe)
    seattle_rendered = (
        "<memory as_of='2026-03-02'>[m:00000abc] User lives in Seattle. (user, high)</memory>"
    )

    violation = score_probe(
        update_probe,
        update_scenario,
        store_live=[
            _record(
                belief_id=802,
                subject="home location",
                statement="User lives in Portland.",
                belief_type=BeliefType.USER_MODEL_ATTR,
            )
        ],
        snapshot=_recall_trace(
            trace_id=901, moment=RecallMoment.SNAPSHOT, beliefs=[portland, noise]
        ),
        in_turn=[
            _recall_trace(
                trace_id=902,
                moment=RecallMoment.IN_TURN,
                beliefs=[noise],
                rendered=seattle_rendered,
            )
        ],
        distinct_prefixes=1,
        policy_failures=0,
        run_completed=True,
    )

    assert violation.needed_formed == 1
    assert violation.needed_recalled == 1
    assert violation.noise_snapshot == 1
    assert violation.noise_in_turn == 1
    assert violation.noise_total == 1
    assert violation.returned_total == 2
    assert violation.currency_violations == 1
    assert violation.currency_unformed == 0

    unformed = score_probe(
        update_probe,
        update_scenario,
        store_live=[],
        snapshot=_recall_trace(trace_id=903, moment=RecallMoment.SNAPSHOT, beliefs=[noise]),
        in_turn=[
            _recall_trace(
                trace_id=904,
                moment=RecallMoment.IN_TURN,
                beliefs=[noise],
                rendered=seattle_rendered,
            )
        ],
        distinct_prefixes=1,
        policy_failures=0,
        run_completed=True,
    )

    assert unformed.needed_formed == 0
    assert unformed.currency_violations == 0
    assert unformed.currency_unformed == 1

    abstention_probe = _valid_probe("abstention", probe_id="p02")
    abstention_scenario = _scenario(abstention_probe, protected=["REDACTED-TOKEN"])
    leak = score_probe(
        abstention_probe,
        abstention_scenario,
        store_live=[],
        snapshot=_recall_trace(
            trace_id=905,
            moment=RecallMoment.SNAPSHOT,
            beliefs=[
                _recalled_belief(belief_id=803, subject="credential", statement="REDACTED-TOKEN")
            ],
        ),
        in_turn=[],
        distinct_prefixes=1,
        policy_failures=0,
        run_completed=True,
    )

    assert leak.needed_total == 0
    assert leak.returned_total == 1
    assert leak.noise_snapshot == 1
    assert leak.abstention_leaks == 1
    assert leak.forbidden_rendered == 1

    transfer_probe = _probe(
        "transfer",
        probe_id="p03",
        project_scope="beta",
        forbidden=["The team decided to deploy on Fridays."],
        answer=ProbeAnswer(kind="abstain"),
    )
    transfer_scenario = _scenario(transfer_probe)
    carried = score_probe(
        transfer_probe,
        transfer_scenario,
        store_live=[],
        snapshot=None,
        in_turn=[
            _recall_trace(
                trace_id=906,
                moment=RecallMoment.IN_TURN,
                beliefs=[
                    _recalled_belief(
                        belief_id=804,
                        subject="project decision",
                        statement="The team decided to deploy on Fridays.",
                        belief_type=BeliefType.FACT,
                    )
                ],
            )
        ],
        distinct_prefixes=1,
        policy_failures=0,
        run_completed=True,
    )

    assert carried.returned_snapshot == 0
    assert carried.returned_in_turn == 1
    assert carried.noise_in_turn == 1
    assert carried.false_transfers == 1
    assert carried.other_forbidden_rendered == 0


def test_score_probe_counts_noise_returned_in_both_moments_once() -> None:
    scenario = _scoring_scenario()
    probe = scenario.probes[0]
    alpha = _recalled_belief(belief_id=701, subject="answer style", statement="User prefers alpha.")
    stray = _recalled_belief(belief_id=750, subject="hobby", statement="User goes hiking.")

    result = score_probe(
        probe,
        scenario,
        store_live=[
            _record(belief_id=701, subject="answer style", statement="User prefers alpha.")
        ],
        snapshot=_recall_trace(trace_id=911, moment=RecallMoment.SNAPSHOT, beliefs=[alpha, stray]),
        in_turn=[_recall_trace(trace_id=912, moment=RecallMoment.IN_TURN, beliefs=[alpha, stray])],
        distinct_prefixes=1,
        policy_failures=0,
        run_completed=True,
    )

    assert result.noise_snapshot == 1
    assert result.noise_in_turn == 1
    assert result.noise_total == 1
    assert result.returned_snapshot == 2
    assert result.returned_in_turn == 2
    assert result.returned_total == 2

    metrics = aggregate_deterministic(
        [
            DeterministicScenarioResult(
                scenario_id="mb-bench-001",
                extractor_name="memory-extractor@1",
                formation=FormationMetrics(
                    expected=1,
                    supported=1,
                    formed=1,
                    fabricated=0,
                    stale_live=0,
                    policy_failures=0,
                ),
                probes=[result],
            )
        ]
    )

    assert metrics.noise_total <= metrics.returned_total
    assert ratios(metrics)["noise_ratio"] == "0.5000"


def test_score_formation_separates_stale_from_fabricated() -> None:
    scenario = _scenario(_valid_probe("update"), protected=["REDACTED-TOKEN"])
    live = [
        _record(
            belief_id=901,
            subject="home location",
            statement="User lives in Portland.",
            belief_type=BeliefType.USER_MODEL_ATTR,
        ),
        _record(
            belief_id=902,
            subject="home location",
            statement="User lives in Seattle.",
            belief_type=BeliefType.USER_MODEL_ATTR,
        ),
        _record(
            belief_id=903,
            subject="wealth",
            statement="User owns a private island.",
            belief_type=BeliefType.FACT,
        ),
        _record(
            belief_id=904,
            subject="credential",
            statement="The deploy token is REDACTED-TOKEN.",
            belief_type=BeliefType.FACT,
        ),
    ]

    metrics = score_formation(scenario, live, live)

    assert metrics.expected == 3
    assert metrics.supported == 1
    assert metrics.formed == 4
    assert metrics.stale_live == 1
    assert metrics.fabricated == 2
    assert metrics.policy_failures == 1

    retired = _record(
        belief_id=905,
        subject="credential",
        statement="The rotated token is REDACTED-TOKEN.",
        belief_type=BeliefType.FACT,
        status=MemoryStatus.RETIRED,
    )

    with_retired = score_formation(scenario, live, [*live, retired])

    assert with_retired.formed == 4
    assert with_retired.policy_failures == 2


def _probe_result(
    *,
    probe_id: str = "p01",
    category: ProbeCategory = "single_hop",
    run_completed: bool = True,
    needed_total: int = 0,
    needed_formed: int = 0,
    needed_recalled: int = 0,
    recalled_snapshot_only: int = 0,
    recalled_in_turn_only: int = 0,
    recalled_both: int = 0,
    returned_snapshot: int = 0,
    returned_in_turn: int = 0,
    returned_total: int = 0,
    noise_snapshot: int = 0,
    noise_in_turn: int = 0,
    noise_total: int = 0,
    dropped_for_budget: int = 0,
    blocked_rendered: int = 0,
    currency_violations: int = 0,
    currency_unformed: int = 0,
    abstention_leaks: int = 0,
    false_transfers: int = 0,
    other_forbidden_rendered: int = 0,
    forbidden_rendered: int = 0,
    policy_failures: int = 0,
    distinct_prefixes: int = 1,
) -> ProbeRetrievalResult:
    return ProbeRetrievalResult(
        probe_id=probe_id,
        category=category,
        run_completed=run_completed,
        needed_total=needed_total,
        needed_formed=needed_formed,
        needed_recalled=needed_recalled,
        recalled_snapshot_only=recalled_snapshot_only,
        recalled_in_turn_only=recalled_in_turn_only,
        recalled_both=recalled_both,
        returned_snapshot=returned_snapshot,
        returned_in_turn=returned_in_turn,
        returned_total=returned_total,
        noise_snapshot=noise_snapshot,
        noise_in_turn=noise_in_turn,
        noise_total=noise_total,
        dropped_for_budget=dropped_for_budget,
        blocked_rendered=blocked_rendered,
        currency_violations=currency_violations,
        currency_unformed=currency_unformed,
        abstention_leaks=abstention_leaks,
        false_transfers=false_transfers,
        other_forbidden_rendered=other_forbidden_rendered,
        forbidden_rendered=forbidden_rendered,
        policy_failures=policy_failures,
        distinct_prefixes=distinct_prefixes,
    )


def _scenario_results() -> list[DeterministicScenarioResult]:
    return [
        DeterministicScenarioResult(
            scenario_id="mb-bench-001",
            extractor_name="memory-extractor@1",
            formation=FormationMetrics(
                expected=3, supported=2, formed=4, fabricated=1, stale_live=1, policy_failures=1
            ),
            probes=[
                _probe_result(
                    probe_id="p01",
                    category="single_hop",
                    needed_total=1,
                    needed_formed=1,
                    needed_recalled=1,
                    recalled_snapshot_only=1,
                    returned_snapshot=2,
                    returned_total=2,
                    noise_snapshot=1,
                    noise_total=1,
                ),
                _probe_result(
                    probe_id="p02",
                    category="abstention",
                    run_completed=False,
                    returned_in_turn=1,
                    returned_total=1,
                    noise_in_turn=1,
                    noise_total=1,
                    abstention_leaks=1,
                    forbidden_rendered=1,
                    policy_failures=2,
                    distinct_prefixes=2,
                ),
            ],
        ),
        DeterministicScenarioResult(
            scenario_id="mb-bench-002",
            extractor_name="memory-extractor@1",
            formation=FormationMetrics(
                expected=2, supported=2, formed=2, fabricated=0, stale_live=0, policy_failures=0
            ),
            probes=[
                _probe_result(
                    probe_id="p01",
                    category="single_hop",
                    needed_total=2,
                    needed_formed=2,
                    needed_recalled=2,
                    recalled_in_turn_only=1,
                    recalled_both=1,
                    returned_in_turn=3,
                    returned_total=3,
                    noise_in_turn=1,
                    noise_total=1,
                    dropped_for_budget=2,
                    blocked_rendered=1,
                    currency_violations=1,
                    forbidden_rendered=1,
                ),
            ],
        ),
    ]


def test_aggregate_sums_per_category() -> None:
    metrics = aggregate_deterministic(_scenario_results())

    assert metrics.scenario_count == 2
    assert metrics.probe_count == 3
    assert metrics.expected_beliefs == 5
    assert metrics.supported_beliefs == 4
    assert metrics.formed_beliefs == 6
    assert metrics.fabricated_beliefs == 1
    assert metrics.stale_live_beliefs == 1
    assert metrics.formation_policy_failures == 1
    assert metrics.needed_total == 3
    assert metrics.needed_formed == 3
    assert metrics.needed_recalled == 3
    assert metrics.recalled_snapshot_only == 1
    assert metrics.recalled_in_turn_only == 1
    assert metrics.recalled_both == 1
    assert metrics.returned_total == 6
    assert metrics.noise_total == 3
    assert metrics.dropped_for_budget == 2
    assert metrics.blocked_rendered == 1
    assert metrics.currency_violations == 1
    assert metrics.currency_unformed == 0
    assert metrics.abstention_leaks == 1
    assert metrics.false_transfers == 0
    assert metrics.run_policy_failures == 2
    assert metrics.probe_runs_completed == 2
    assert metrics.max_distinct_prefixes_per_probe == 2
    assert list(metrics.per_category) == ["abstention", "single_hop"]
    assert metrics.per_category["single_hop"] == CategoryMetrics(
        probes=2,
        needed_total=3,
        needed_formed=3,
        needed_recalled=3,
        returned_total=5,
        noise_total=2,
        forbidden_rendered=1,
    )
    assert metrics.per_category["abstention"] == CategoryMetrics(
        probes=1,
        needed_total=0,
        needed_formed=0,
        needed_recalled=0,
        returned_total=1,
        noise_total=1,
        forbidden_rendered=1,
    )


def test_ratios_handle_zero_denominators() -> None:
    assert ratios(aggregate_deterministic([])) == {
        "formation_precision": "n/a",
        "formation_recall": "n/a",
        "end_to_end_recall": "n/a",
        "retrieval_recall_given_formed": "n/a",
        "noise_ratio": "n/a",
        "snapshot_share": "n/a",
    }
    assert ratios(aggregate_deterministic(_scenario_results())) == {
        "formation_precision": "0.6667",
        "formation_recall": "0.8000",
        "end_to_end_recall": "1.0000",
        "retrieval_recall_given_formed": "1.0000",
        "noise_ratio": "0.5000",
        "snapshot_share": "0.6667",
    }


def _event(*, sequence: int, event_type: str, payload: dict[str, object]) -> EventEnvelope:
    return EventEnvelope(
        id=sequence,
        session_id=UUID(int=20),
        run_id=UUID(int=30),
        sequence=sequence,
        event_type=event_type,
        payload_schema_version=1,
        actor_type="agent",
        actor_id=None,
        payload=payload,
        trace_id=None,
        created_at=_START,
    )


def test_probe_run_facts_counts_prefixes_and_policy_failures() -> None:
    events = [
        _event(sequence=1, event_type="model.request.started", payload={"prefix_sha256": "a" * 64}),
        _event(sequence=2, event_type="model.request.started", payload={"prefix_sha256": "a" * 64}),
        _event(sequence=3, event_type="model.request.started", payload={"prefix_sha256": "b" * 64}),
        _event(sequence=4, event_type="model.request.started", payload={}),
        _event(sequence=5, event_type="tool.call.denied", payload={"reason_code": "tool.unknown"}),
        _event(sequence=6, event_type="run.failed", payload={"reason_code": "policy.hardline"}),
        _event(
            sequence=7,
            event_type="run.step.completed",
            payload={"reason_code": "budget.exhausted"},
        ),
    ]

    assert probe_run_facts(events, RunStatus.COMPLETED) == (2, 2, True)
    assert probe_run_facts(events, RunStatus.FAILED) == (2, 2, False)
    assert probe_run_facts([], RunStatus.COMPLETED) == (0, 0, True)


_DIGEST = "0" * 64
_OTHER_DIGEST = "1" * 64


def _result(
    scenarios: Sequence[DeterministicScenarioResult] | None = None,
    *,
    corpus_sha256: str = _DIGEST,
    extractor_name: str = "memory-extractor@1",
    provider_formation_policy_version: str = "provider-formation@1",
) -> DeterministicBenchmarkResult:
    values = list(_scenario_results() if scenarios is None else scenarios)
    return DeterministicBenchmarkResult(
        benchmark_version=BENCHMARK_VERSION,
        corpus_sha256=corpus_sha256,
        formation_policy_version="formation@1",
        provider_formation_policy_version=provider_formation_policy_version,
        retrieval_policy_version="retrieval@1",
        extractor_name=extractor_name,
        scenarios=values,
        metrics=aggregate_deterministic(values),
    )


def _baseline_of(result: DeterministicBenchmarkResult) -> MemoryBenchmarkBaseline:
    return MemoryBenchmarkBaseline(
        benchmark_version=result.benchmark_version,
        corpus_sha256=result.corpus_sha256,
        formation_policy_version=result.formation_policy_version,
        provider_formation_policy_version=result.provider_formation_policy_version,
        retrieval_policy_version=result.retrieval_policy_version,
        extractor_name=result.extractor_name,
        build_ref="0123456789ab",
        recorded_at=_START,
        metrics=result.metrics,
        probes=baseline_probe_rows(result),
    )


def _worse_results() -> list[DeterministicScenarioResult]:
    first, second = _scenario_results()
    lowered = second.probes[0].model_copy(
        update={"needed_recalled": 1, "recalled_both": 0, "noise_in_turn": 2, "noise_total": 2}
    )
    return [first, second.model_copy(update={"probes": [lowered]})]


def test_compare_to_baseline_reports_regression_drift_and_improvement() -> None:
    baseline = _baseline_of(_result())

    identical = compare_to_baseline(_result(), baseline)

    assert identical.drift == []
    assert identical.regressions == []
    assert identical.improvements == []

    drifted = compare_to_baseline(
        _result(corpus_sha256=_OTHER_DIGEST, extractor_name="memory-extractor@2"), baseline
    )

    assert any(entry.startswith("corpus_sha256") for entry in drifted.drift)
    assert any(entry.startswith("extractor_name") for entry in drifted.drift)
    assert drifted.regressions == []
    assert drifted.improvements == []

    shrunk = compare_to_baseline(_result(_scenario_results()[:1]), baseline)

    assert any(entry.startswith("scenario_count") for entry in shrunk.drift)
    assert any(entry.startswith("probe_count") for entry in shrunk.drift)
    assert any(entry.startswith("needed_total") for entry in shrunk.drift)

    regressed = compare_to_baseline(_result(_worse_results()), baseline)

    assert regressed.drift == []
    assert any(
        entry.startswith("needed_recalled regressed: baseline 3") for entry in regressed.regressions
    )
    # An attribution count falling alongside recall is reported as a shift; it
    # is a bucket of `needed_recalled`, not a metric with a good direction.
    assert any(entry.startswith("recalled_both") for entry in regressed.shifts)
    assert any(entry.startswith("noise_total") for entry in regressed.regressions)
    assert any(
        entry.startswith("mb-bench-002/p01 needed_recalled") for entry in regressed.regressions
    )
    assert regressed.improvements == []

    improved = compare_to_baseline(_result(), _baseline_of(_result(_worse_results())))

    assert improved.drift == []
    assert improved.regressions == []
    assert any(entry.startswith("needed_recalled improved") for entry in improved.improvements)
    assert any(entry.startswith("noise_total") for entry in improved.improvements)
    assert any(
        entry.startswith("mb-bench-002/p01 needed_recalled") for entry in improved.improvements
    )


def test_compare_to_baseline_reports_provider_formation_version_drift() -> None:
    baseline = _baseline_of(_result())

    drifted = compare_to_baseline(
        _result(provider_formation_policy_version="provider-formation@2"), baseline
    )

    assert any(entry.startswith("provider_formation_policy_version") for entry in drifted.drift)
    assert drifted.regressions == []


def test_load_corpus_rejects_paths_outside_repository(tmp_path: Path) -> None:
    document = _corpus().model_dump_json()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / CORPUS_PATH.name).write_text(document, encoding="utf-8")
    escaping = tmp_path / "escaping"
    (escaping / CORPUS_PATH.parent.parent).mkdir(parents=True)
    (escaping / CORPUS_PATH.parent).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes the repository"):
        load_corpus(escaping)

    repository = tmp_path / "repository"
    (repository / CORPUS_PATH.parent).mkdir(parents=True)
    (repository / CORPUS_PATH).write_text(document, encoding="utf-8")

    corpus, digest = load_corpus(repository)

    assert len(corpus.scenarios) == 12
    assert digest == hashlib.sha256((repository / CORPUS_PATH).read_bytes()).hexdigest()


def test_load_baseline_returns_none_until_it_is_recorded(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / BASELINE_PATH.parent).mkdir(parents=True)

    assert load_baseline(repository) is None

    recorded = _baseline_of(_result())
    (repository / BASELINE_PATH).write_text(recorded.model_dump_json(), encoding="utf-8")

    assert load_baseline(repository) == recorded


async def test_run_benchmark_fails_on_regression_and_writes_baseline_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    (repository / BASELINE_PATH.parent).mkdir(parents=True)
    (repository / BASELINE_PATH).write_text(
        _baseline_of(_result()).model_dump_json(), encoding="utf-8"
    )
    regressed = _result(_worse_results())

    async def run_deterministic_benchmark(
        _root: Path, **_kwargs: object
    ) -> DeterministicBenchmarkResult:
        return regressed

    monkeypatch.setattr(
        memory_benchmark_driver, "run_deterministic_benchmark", run_deterministic_benchmark
    )
    output = tmp_path / "recorded.json"

    result = await memory_benchmark_driver.run_benchmark(
        repository,
        deterministic_only=True,
        model_policy="balanced",
        policy_profile="default",
        build_ref="0123456789ab",
        output=None,
        baseline_output=output,
        clock=_StoppedClock(_START),
    )

    assert result is not None
    assert result.passed is False
    assert result.failure_summary is not None
    assert "needed_recalled regressed" in result.failure_summary
    assert result.deterministic == regressed
    assert result.baseline is not None
    assert result.baseline.drift == []
    assert result.baseline.regressions
    assert result.live is None
    assert result.evidence is None

    recorded = MemoryBenchmarkBaseline.model_validate_json(output.read_text(encoding="utf-8"))

    assert recorded.build_ref == "0123456789ab"
    assert recorded.corpus_sha256 == regressed.corpus_sha256
    assert recorded.extractor_name == regressed.extractor_name
    assert recorded.metrics == regressed.metrics
    assert recorded.probes == baseline_probe_rows(regressed)
    assert recorded.recorded_at == _START

    with pytest.raises(ValueError, match="refusing to overwrite"):
        await memory_benchmark_driver.run_benchmark(
            repository,
            deterministic_only=True,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=None,
            baseline_output=output,
            clock=_StoppedClock(_START),
        )


async def test_run_benchmark_passes_when_no_baseline_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run_deterministic_benchmark(
        _root: Path, **_kwargs: object
    ) -> DeterministicBenchmarkResult:
        return _result()

    monkeypatch.setattr(
        memory_benchmark_driver, "run_deterministic_benchmark", run_deterministic_benchmark
    )

    result = await memory_benchmark_driver.run_benchmark(
        tmp_path,
        deterministic_only=True,
        model_policy="balanced",
        policy_profile="default",
        build_ref="0123456789ab",
        output=None,
        baseline_output=None,
    )

    assert result is not None
    assert result.passed is True
    assert result.failure_summary is None
    assert result.baseline is None
    assert not any(tmp_path.iterdir())


async def test_run_benchmark_hands_the_live_arm_to_the_live_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[dict[str, object]] = []
    live = MemoryBenchmarkResult(
        passed=False,
        failure_summary="lift 0 below the minimum of 11",
        deterministic=_result(),
        live=_LIVE_STAND_IN,
    )

    async def run_live_benchmark(
        root: Path,
        *,
        model_policy: str,
        policy_profile: str,
        build_ref: str,
        output: Path,
    ) -> MemoryBenchmarkResult:
        observed.append(
            {
                "root": root,
                "model_policy": model_policy,
                "policy_profile": policy_profile,
                "build_ref": build_ref,
                "output": output,
            }
        )
        return live

    monkeypatch.setattr(memory_benchmark_live, "run_live_benchmark", run_live_benchmark)

    result = await memory_benchmark_driver.run_benchmark(
        tmp_path,
        deterministic_only=False,
        model_policy="balanced",
        policy_profile="default",
        build_ref="0123456789ab",
        output=tmp_path / "evidence.json",
        baseline_output=None,
    )

    assert result is live
    assert observed == [
        {
            "root": tmp_path,
            "model_policy": "balanced",
            "policy_profile": "default",
            "build_ref": "0123456789ab",
            "output": tmp_path / "evidence.json",
        }
    ]

    with pytest.raises(ValueError, match="output"):
        await memory_benchmark_driver.run_benchmark(
            tmp_path,
            deterministic_only=False,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=None,
            baseline_output=None,
        )

    with pytest.raises(ValueError, match="output"):
        await memory_benchmark_driver.run_benchmark(
            tmp_path,
            deterministic_only=True,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=tmp_path / "evidence.json",
            baseline_output=None,
        )


def test_memory_benchmark_result_requires_evidence_only_for_a_live_arm() -> None:
    deterministic = _result()

    passing = MemoryBenchmarkResult(passed=True, failure_summary=None, deterministic=deterministic)

    assert passing.evidence is None

    with pytest.raises(ValidationError, match="failure summary"):
        MemoryBenchmarkResult(passed=False, failure_summary=None, deterministic=deterministic)

    with pytest.raises(ValidationError, match="failure summary"):
        MemoryBenchmarkResult(passed=True, failure_summary="drifted", deterministic=deterministic)

    with pytest.raises(ValidationError, match="activation evidence"):
        MemoryBenchmarkResult(
            passed=True,
            failure_summary=None,
            deterministic=deterministic,
            live=_LIVE_STAND_IN,
        )

    with pytest.raises(ValidationError, match="activation evidence"):
        MemoryBenchmarkResult(
            passed=False,
            failure_summary="live arm failed",
            deterministic=deterministic,
            live=_LIVE_STAND_IN,
            evidence=_LIVE_STAND_IN,
        )


def test_probe_prompt_appends_the_instruction_only_for_the_live_arm() -> None:
    corpus = _corpus()
    probe = corpus.scenarios[0].probes[0]

    assert memory_benchmark_driver.probe_prompt(probe, corpus, live=False) == probe.question
    assert memory_benchmark_driver.probe_prompt(probe, corpus, live=True) == (
        f"{probe.question}\n{corpus.probe_instruction}"
    )


async def test_run_deterministic_benchmark_stamps_the_identity_it_ran_under(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    (repository / CORPUS_PATH.parent).mkdir(parents=True)
    (repository / CORPUS_PATH).write_text(_corpus().model_dump_json(), encoding="utf-8")
    observed: list[str] = []

    async def run_deterministic_scenario(
        _settings: object, scenario: BenchmarkScenario, **_kwargs: object
    ) -> DeterministicScenarioResult:
        observed.append(scenario.id)
        return DeterministicScenarioResult(
            scenario_id=scenario.id,
            extractor_name="provider-assisted-test-v1",
            formation=FormationMetrics(
                expected=0, supported=0, formed=0, fabricated=0, stale_live=0, policy_failures=0
            ),
        )

    monkeypatch.setattr(
        memory_benchmark_driver, "run_deterministic_scenario", run_deterministic_scenario
    )

    result = await memory_benchmark_driver.run_deterministic_benchmark(
        repository, settings=memory_settings()
    )

    assert observed == [scenario.id for scenario in _corpus().scenarios]
    assert result.extractor_name == "provider-assisted-test-v1"
    assert result.formation_policy_version == FORMATION_POLICY_VERSION
    assert result.provider_formation_policy_version == PROVIDER_FORMATION_POLICY_VERSION
    assert result.retrieval_policy_version == RETRIEVAL_POLICY_VERSION
    assert result.benchmark_version == BENCHMARK_VERSION
    assert result.corpus_sha256 == load_corpus(repository)[1]
    assert result.metrics.scenario_count == 12


async def test_run_benchmark_defaults_its_recording_clock_to_the_composition_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run_deterministic_benchmark(
        _root: Path, **_kwargs: object
    ) -> DeterministicBenchmarkResult:
        return _result()

    monkeypatch.setattr(
        memory_benchmark_driver, "run_deterministic_benchmark", run_deterministic_benchmark
    )
    output = tmp_path / "recorded.json"

    await memory_benchmark_driver.run_benchmark(
        tmp_path,
        deterministic_only=True,
        model_policy="balanced",
        policy_profile="default",
        build_ref="0123456789ab",
        output=None,
        baseline_output=output,
    )

    recorded = MemoryBenchmarkBaseline.model_validate_json(output.read_text(encoding="utf-8"))

    assert recorded.recorded_at.tzinfo is not None
    assert recorded.recorded_at.utcoffset() == timedelta(0)


_ABSTAIN_PHRASE = "I do not have that information."
_REPOSITORY = Path(__file__).resolve().parents[2]


def _score(
    answer: str | None,
    expected: ProbeAnswer,
    *,
    protected: Sequence[str] = (),
) -> AnswerScore:
    return score_answer(answer, expected, abstain_phrase=_ABSTAIN_PHRASE, protected=protected)


def test_score_answer_token_bounded_alternatives_all_of_and_abstain() -> None:
    alternatives = ProbeAnswer(kind="alternatives", values=["Portland", "Portland, Oregon"])
    all_of = ProbeAnswer(kind="all_of", values=["concise", "Fridays"])
    abstain = ProbeAnswer(kind="abstain")

    assert _score("Portland.", alternatives).correct is True
    assert _score("You live in Portland, Oregon", alternatives).correct is True
    assert _score("Portlandia", alternatives).correct is False
    assert _score("Portland\nand nothing else", alternatives).correct is False
    assert _score(None, alternatives).correct is False
    assert _score("", alternatives).correct is False

    assert _score("Keep it concise and deploy on Fridays.", all_of).correct is True
    assert _score("Keep it concise.", all_of).correct is False

    abstained = _score("i do not have that information", abstain)

    assert (abstained.correct, abstained.abstained) == (True, True)
    assert _score("Portland", abstain).correct is False

    answered = _score(_ABSTAIN_PHRASE, alternatives)

    assert (answered.correct, answered.abstained) == (False, True)

    leaked = _score("The token is REDACTED-TOKEN.", alternatives, protected=["REDACTED-TOKEN"])

    assert leaked.leaked_protected is True
    assert _score("Portland", alternatives, protected=["REDACTED-TOKEN"]).leaked_protected is False


def _live_metrics(**overrides: object) -> MemoryBenchmarkLiveMetrics:
    fields: dict[str, object] = {
        "probe_count": 20,
        "answerable_probe_count": 15,
        "abstain_expected": 5,
        "with_memory_correct": 15,
        "without_memory_correct": 8,
        "lift": 7,
        "recoverable_probe_count": 10,
        "recoverable_correct": 9,
        "abstain_with_memory_correct": 5,
        "protected_leaks_in_answers": 0,
        "with_memory_policy_failures": 0,
        "without_memory_policy_failures": 0,
        "incomplete_runs": 0,
        "ceiling_hits": 0,
        "total_cost_usd": Decimal("1.25"),
        "p50_latency_ms": 900,
        "p95_latency_ms": 1800,
    }
    fields.update(overrides)
    return MemoryBenchmarkLiveMetrics(**fields)  # type: ignore[arg-type]


def _evidence_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "benchmark_version": BENCHMARK_VERSION,
        "corpus_sha256": _DIGEST,
        "build_ref": "0123456789ab",
        "model_policy": "balanced",
        "provider": "openai",
        "model": "gpt-memory",
        "policy_profile": "default",
        "policy_version": "default@profile+hline",
        "formation_policy_version": "formation@1",
        "retrieval_policy_version": "retrieval@1",
        "cost_ceiling_usd": LIVE_COST_CEILING_USD,
        "minimum_lift": 3,
        "minimum_recoverable_correct": 8,
        "minimum_abstain_correct": 4,
        "deterministic": _result().metrics,
        "live": _live_metrics(),
        "evaluated_at": _START,
    }
    fields.update(overrides)
    return fields


def test_evidence_accepts_a_run_that_met_every_pass_condition() -> None:
    evidence = MemoryBenchmarkEvidence(**_evidence_fields())  # type: ignore[arg-type]

    assert evidence.schema_version == 1
    assert evidence.minimum_lift == minimum_live_lift(15) == 3
    assert evidence.live.lift >= evidence.minimum_lift
    assert MemoryBenchmarkEvidence.model_validate_json(evidence.model_dump_json()) == evidence


@pytest.mark.parametrize(
    ("evidence_overrides", "live_overrides", "match"),
    [
        pytest.param({}, {"with_memory_correct": 10, "lift": 2}, "lift", id="lift-below-floor"),
        pytest.param({"minimum_lift": 2}, {}, "minimum lift", id="wrong-minimum-lift"),
        pytest.param(
            {"minimum_recoverable_correct": 7}, {}, "recoverable", id="wrong-minimum-recoverable"
        ),
        pytest.param({"minimum_abstain_correct": 3}, {}, "abstain", id="wrong-minimum-abstain"),
        pytest.param({}, {"recoverable_correct": 7}, "recoverable", id="recoverable-below-floor"),
        pytest.param({}, {"abstain_with_memory_correct": 3}, "abstain", id="abstain-below-floor"),
        pytest.param({}, {"total_cost_usd": Decimal("4.01")}, "cost", id="cost-over-ceiling"),
        pytest.param({"cost_ceiling_usd": Decimal("9.00")}, {}, "ceiling", id="wrong-cost-ceiling"),
        pytest.param({}, {"ceiling_hits": 1}, "ceiling", id="ceiling-hit"),
        pytest.param({}, {"stopped_by": "cost_ceiling"}, "stopped", id="stopped-early"),
        pytest.param(
            {},
            {"with_memory_policy_failures": 2, "without_memory_policy_failures": 1},
            "policy",
            id="policy-regression",
        ),
        pytest.param({}, {"protected_leaks_in_answers": 1}, "protected", id="protected-leak"),
        pytest.param({}, {"incomplete_runs": 1}, "incomplete", id="incomplete-run"),
        pytest.param(
            {},
            {"failure_classes": {"ModelStreamError": 1}},
            "failure class",
            id="histogram-disagrees-with-incomplete-runs",
        ),
        pytest.param({}, {"retried_runs": 41}, "retried", id="retried-over-total-runs"),
        pytest.param({}, {"lift": 9}, "lift", id="inconsistent-lift"),
        pytest.param(
            {}, {"answerable_probe_count": 14}, "answerable", id="inconsistent-answerable"
        ),
        pytest.param(
            {}, {"recoverable_probe_count": 20}, "recoverable", id="recoverable-over-answerable"
        ),
        pytest.param(
            {"evaluated_at": datetime(2026, 8, 22, 9, 0)},
            {},
            "timezone-aware",
            id="naive-datetime",
        ),
    ],
)
def test_evidence_rejects_failing_pass_conditions(
    evidence_overrides: dict[str, object],
    live_overrides: dict[str, object],
    match: str,
) -> None:
    fields = _evidence_fields(**evidence_overrides)
    if live_overrides:
        fields["live"] = _live_metrics(**live_overrides)

    with pytest.raises(ValidationError, match=match):
        MemoryBenchmarkEvidence(**fields)  # type: ignore[arg-type]


_IDENTITY = ("openai", "gpt-memory", "default@profile+hline")
_UNRESOLVED: tuple[str | None, str | None, str | None] = (None, None, None)


def _live_arm(
    arm: LiveArm,
    identity: tuple[str | None, str | None, str | None] = _IDENTITY,
    *,
    correct: bool = True,
) -> LiveProbeArmResult:
    provider, model, policy_version = identity
    return LiveProbeArmResult(
        arm=arm,
        answer="Portland",
        score=AnswerScore(correct=correct, abstained=False, leaked_protected=False),
        run_status=RunStatus.COMPLETED,
        model_calls=1,
        cost_usd=Decimal("0.01"),
        latency_ms=100,
        policy_failures=0,
        provider=provider,
        model=model,
        policy_version=policy_version,
        retrieval=(
            _probe_result(needed_total=1, needed_formed=1, needed_recalled=1)
            if arm == "with_memory"
            else None
        ),
    )


def _one_probe_row(
    *,
    with_memory: LiveProbeArmResult | None = None,
    without_memory: LiveProbeArmResult | None = None,
) -> LiveProbeResult:
    return LiveProbeResult(
        scenario_id="mb-bench-001",
        probe_id="p01",
        category="single_hop",
        with_memory=_live_arm("with_memory") if with_memory is None else with_memory,
        without_memory=(
            _live_arm("without_memory", correct=False) if without_memory is None else without_memory
        ),
        lift=1,
    )


def _one_probe_evidence(row: LiveProbeResult) -> dict[str, object]:
    return _evidence_fields(
        minimum_lift=1,
        minimum_recoverable_correct=1,
        minimum_abstain_correct=0,
        live=_live_metrics(
            probe_count=1,
            answerable_probe_count=1,
            abstain_expected=0,
            with_memory_correct=1,
            without_memory_correct=0,
            lift=1,
            recoverable_probe_count=1,
            recoverable_correct=1,
            abstain_with_memory_correct=0,
            probes=[row],
        ),
    )


def test_evidence_requires_one_identity_across_every_probe_row() -> None:
    evidence = MemoryBenchmarkEvidence(**_one_probe_evidence(_one_probe_row()))  # type: ignore[arg-type]

    assert (evidence.provider, evidence.model, evidence.policy_version) == _IDENTITY

    foreign_provider = _one_probe_row(
        without_memory=_live_arm(
            "without_memory",
            ("anthropic", "gpt-memory", "default@profile+hline"),
            correct=False,
        )
    )
    foreign_policy = _one_probe_row(
        with_memory=_live_arm("with_memory", ("openai", "gpt-memory", "eval.default@other+hline"))
    )
    unresolved = _one_probe_row(with_memory=_live_arm("with_memory", _UNRESOLVED))

    with pytest.raises(ValidationError, match="different provider"):
        MemoryBenchmarkEvidence(**_one_probe_evidence(foreign_provider))  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="different provider"):
        MemoryBenchmarkEvidence(**_one_probe_evidence(foreign_policy))  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="did not resolve"):
        MemoryBenchmarkEvidence(**_one_probe_evidence(unresolved))  # type: ignore[arg-type]


def test_terminal_failure_class_names_the_class_and_never_the_message() -> None:
    """A failed run is diagnosable by class name alone, with no content in it."""

    assert memory_benchmark_live._terminal_failure_class(None) is None

    failure = RunFailure(
        reason=FailureReason.INTERNAL_ERROR,
        error_class="ModelStreamError",
        message="the principal lives at 5 Elm Street",
        occurred_at=_START,
    )

    assert memory_benchmark_live._terminal_failure_class(failure) == "ModelStreamError"


def test_incomplete_run_diagnostics_name_every_failed_arm_content_free() -> None:
    """Every incomplete run prints what it was, why it ended, and what it cost."""

    failed = _live_arm("without_memory", correct=False).model_copy(
        update={
            "run_status": RunStatus.FAILED,
            "answer": "the principal lives at 5 Elm Street",
            "failure_class": "ModelStreamError",
            "model_calls": 1,
            "cost_usd": Decimal("0.01071"),
            "retried": True,
        }
    )
    metrics = _live_metrics(
        incomplete_runs=1,
        retried_runs=1,
        failure_classes={"ModelStreamError": 1},
        probes=[_one_probe_row(without_memory=failed)],
    )

    lines = memory_benchmark_live.incomplete_run_diagnostics(metrics)

    assert len(lines) == 1
    assert "mb-bench-001/p01" in lines[0]
    assert "without_memory" in lines[0]
    assert "FAILED" in lines[0]
    assert "ModelStreamError" in lines[0]
    assert "model_calls=1" in lines[0]
    assert "0.01071" in lines[0]
    assert "retried=True" in lines[0]
    assert "Elm Street" not in lines[0]
    assert memory_benchmark_live.incomplete_run_diagnostics(_live_metrics()) == []


def test_failure_classes_keep_one_namespace_for_class_names_and_statuses() -> None:
    """A status is never mistaken for a class name in the histogram."""

    named = _live_arm("with_memory").model_copy(
        update={"run_status": RunStatus.FAILED, "failure_class": "ModelStreamError"}
    )
    unnamed = _live_arm("without_memory", correct=False).model_copy(
        update={"run_status": RunStatus.CANCELLED, "failure_class": None}
    )

    histogram = memory_benchmark_live._failure_classes([named, unnamed])

    assert histogram == {"ModelStreamError": 1, "run_status:CANCELLED": 1}


class TestBenchmarkEvidencePublicationGate:
    """The two publication gates: evidence only on a pass, and the cost ceiling."""

    @staticmethod
    def _deterministic(digest: str) -> DeterministicBenchmarkResult:
        return _result(corpus_sha256=digest)

    @staticmethod
    def _arm_evaluator(
        calls: list[tuple[str, str, str]],
        *,
        cost: Decimal = Decimal("0.01"),
        with_memory_knows: bool = True,
        status: RunStatus = RunStatus.COMPLETED,
        failures: dict[tuple[str, str, str], int] | None = None,
        failure_class: str = "ModelStreamError",
        failure_reason: str | None = None,
        raises: bool = False,
    ) -> object:
        remaining = dict(failures or {})

        async def evaluate(
            _settings: object,
            corpus: MemoryBenchmarkCorpus,
            scenario: BenchmarkScenario,
            probe: BenchmarkProbe,
            *,
            arm: str,
            model_policy: str,
            policy_profile: str,
            scenario_context: object,
        ) -> LiveProbeArmResult:
            assert (model_policy, policy_profile) == ("balanced", "default")
            assert scenario_context is not None
            calls.append((scenario.id, probe.id, arm))
            if remaining.get((scenario.id, probe.id, arm), 0) > 0:
                remaining[(scenario.id, probe.id, arm)] -= 1
                if raises:
                    raise TimeoutError("scripted transport failure")
                return LiveProbeArmResult(
                    arm=arm,  # type: ignore[arg-type]
                    answer=None,
                    score=AnswerScore(correct=False, abstained=False, leaked_protected=False),
                    run_status=RunStatus.FAILED,
                    model_calls=0,
                    cost_usd=cost,
                    latency_ms=10,
                    policy_failures=0,
                    provider="openai",
                    model="gpt-memory",
                    policy_version="default@profile+hline",
                    failure_class=failure_class,
                    failure_reason=failure_reason,
                )
            knows = arm == "with_memory" and with_memory_knows
            if probe.answer.kind == "abstain":
                answer = corpus.abstain_phrase
            elif knows:
                answer = " and ".join(probe.answer.values)
            else:
                answer = "I would have to guess."
            return LiveProbeArmResult(
                arm=arm,  # type: ignore[arg-type]
                answer=answer,
                score=score_answer(
                    answer,
                    probe.answer,
                    abstain_phrase=corpus.abstain_phrase,
                    protected=scenario.protected_statements,
                ),
                run_status=status,
                model_calls=1,
                cost_usd=cost,
                latency_ms=120 if arm == "with_memory" else 80,
                policy_failures=0,
                provider="openai",
                model="gpt-memory",
                policy_version="default@profile+hline",
                retrieval=(
                    None
                    if arm != "with_memory"
                    else _probe_result(
                        probe_id=probe.id,
                        category=probe.category,
                        needed_total=len(probe.needed),
                        needed_formed=len(probe.needed),
                        needed_recalled=len(probe.needed),
                    )
                ),
            )

        return evaluate

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        calls: list[tuple[str, str, str]],
        cost: Decimal = Decimal("0.01"),
        with_memory_knows: bool = True,
        failures: dict[tuple[str, str, str], int] | None = None,
        failure_class: str = "ModelStreamError",
        failure_reason: str | None = None,
        raises: bool = False,
    ) -> tuple[MemoryBenchmarkCorpus, str]:
        corpus, digest = load_corpus(_REPOSITORY)
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_benchmark_live, "load_settings", memory_settings)

        async def run_deterministic_benchmark(
            _root: Path, **_kwargs: object
        ) -> DeterministicBenchmarkResult:
            return self._deterministic(digest)

        monkeypatch.setattr(
            memory_benchmark_live, "run_deterministic_benchmark", run_deterministic_benchmark
        )
        monkeypatch.setattr(
            memory_benchmark_live,
            "_evaluate_probe_live",
            self._arm_evaluator(
                calls,
                cost=cost,
                with_memory_knows=with_memory_knows,
                failures=failures,
                failure_class=failure_class,
                failure_reason=failure_reason,
                raises=raises,
            ),
        )
        return corpus, digest

    async def test_pass_publishes_exact_tuple(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        corpus, digest = self._wire(monkeypatch, calls=calls)
        probes = [probe for scenario in corpus.scenarios for probe in scenario.probes]
        abstain_expected = sum(probe.answer.kind == "abstain" for probe in probes)
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is True
        assert result.failure_summary is None
        assert result.evidence is not None
        assert len(calls) == 2 * len(probes)
        evidence = MemoryBenchmarkEvidence.model_validate_json(output.read_text(encoding="utf-8"))
        assert evidence == result.evidence
        assert (
            evidence.model_policy,
            evidence.provider,
            evidence.model,
            evidence.policy_profile,
            evidence.policy_version,
            evidence.build_ref,
        ) == (
            "balanced",
            "openai",
            "gpt-memory",
            "default",
            "default@profile+hline",
            "0123456789ab",
        )
        assert evidence.corpus_sha256 == digest
        assert evidence.benchmark_version == BENCHMARK_VERSION
        assert evidence.cost_ceiling_usd == LIVE_COST_CEILING_USD
        assert evidence.deterministic == self._deterministic(digest).metrics
        assert evidence.live.probe_count == len(probes) == 66
        assert evidence.live.abstain_expected == abstain_expected == 12
        assert evidence.live.answerable_probe_count == 54
        assert evidence.live.with_memory_correct == 66
        assert evidence.live.without_memory_correct == abstain_expected
        assert evidence.live.lift == 54
        assert evidence.minimum_lift == 11
        assert evidence.live.recoverable_probe_count == 54
        assert evidence.minimum_recoverable_correct == 44
        assert evidence.minimum_abstain_correct == 10
        assert evidence.live.total_cost_usd == Decimal("0.01") * 132
        assert evidence.live.ceiling_hits == 0
        assert evidence.live.stopped_by is None
        assert evidence.live.per_category["abstention"].with_memory_correct == 8
        assert len(evidence.live.probes) == 66

        with pytest.raises(ValueError, match="refusing to overwrite"):
            await memory_benchmark_live.run_live_benchmark(
                _REPOSITORY,
                model_policy="balanced",
                policy_profile="default",
                build_ref="0123456789ab",
                output=output,
            )

        assert len(calls) == 2 * len(probes)

    async def test_a_failed_probe_arm_is_retried_once_and_then_publishes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        corpus, _digest = load_corpus(_REPOSITORY)
        first = (corpus.scenarios[0].id, corpus.scenarios[0].probes[0].id, "with_memory")
        self._wire(monkeypatch, calls=calls, failures={first: 1})
        probes = [probe for scenario in corpus.scenarios for probe in scenario.probes]
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is True
        assert result.evidence is not None
        assert calls.count(first) == 2
        assert len(calls) == 2 * len(probes) + 1
        evidence = MemoryBenchmarkEvidence.model_validate_json(output.read_text(encoding="utf-8"))
        assert evidence.live.incomplete_runs == 0
        assert evidence.live.retried_runs == 1
        assert evidence.live.total_cost_usd == Decimal("0.01") * (2 * len(probes) + 1)
        retried = [
            arm
            for row in evidence.live.probes
            for arm in (row.with_memory, row.without_memory)
            if arm.retried
        ]
        assert len(retried) == 1
        assert retried[0].run_status is RunStatus.COMPLETED

    async def test_a_run_the_runtime_ended_on_its_own_limits_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A budget kill is the ceiling working, not a failure to re-roll.

        Re-asking a probe the per-run budget already stopped would let one
        probe spend twice its per-run ceiling, so the four terminations the
        runtime decides on — budget, steps, context overflow, and a permanent
        model error — are kept on the first attempt with their class.
        """

        calls: list[tuple[str, str, str]] = []
        corpus, _digest = load_corpus(_REPOSITORY)
        first = (corpus.scenarios[0].id, corpus.scenarios[0].probes[0].id, "with_memory")
        self._wire(
            monkeypatch,
            calls=calls,
            failures={first: 1},
            failure_class="BudgetExceededError",
            failure_reason=FailureReason.BUDGET_EXCEEDED.value,
        )
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is False
        assert result.evidence is None
        assert not output.exists()
        assert calls.count(first) == 1
        assert isinstance(result.live, MemoryBenchmarkLiveMetrics)
        assert result.live.retried_runs == 0
        assert result.live.incomplete_runs == 1
        assert result.live.failure_classes == {"BudgetExceededError": 1}

    async def test_a_second_failure_leaves_the_run_incomplete_and_named(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        corpus, _digest = load_corpus(_REPOSITORY)
        first = (corpus.scenarios[0].id, corpus.scenarios[0].probes[0].id, "with_memory")
        self._wire(monkeypatch, calls=calls, failures={first: 2})
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is False
        assert result.evidence is None
        assert not output.exists()
        assert calls.count(first) == 2
        assert isinstance(result.live, MemoryBenchmarkLiveMetrics)
        assert result.live.incomplete_runs == 1
        assert result.live.retried_runs == 1
        assert result.live.failure_classes == {"ModelStreamError": 1}
        assert result.failure_summary is not None
        assert "incomplete runs 1" in result.failure_summary

    async def test_an_arm_that_throws_is_a_failed_arm_named_by_its_exception(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        corpus, _digest = load_corpus(_REPOSITORY)
        first = (corpus.scenarios[0].id, corpus.scenarios[0].probes[0].id, "without_memory")
        self._wire(monkeypatch, calls=calls, failures={first: 2}, raises=True)
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is False
        assert result.evidence is None
        assert isinstance(result.live, MemoryBenchmarkLiveMetrics)
        assert result.live.incomplete_runs == 1
        assert result.live.retried_runs == 1
        assert result.live.failure_classes == {"TimeoutError": 1}
        thrown = [
            arm
            for row in result.live.probes
            for arm in (row.with_memory, row.without_memory)
            if arm.failure_class is not None
        ]
        assert len(thrown) == 1
        assert (thrown[0].model_calls, thrown[0].cost_usd) == (0, Decimal("0"))
        assert thrown[0].answer is None

    async def test_a_retry_that_would_cross_the_ceiling_is_not_admitted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        corpus, _digest = load_corpus(_REPOSITORY)
        first = (corpus.scenarios[0].id, corpus.scenarios[0].probes[0].id, "with_memory")
        self._wire(monkeypatch, calls=calls, cost=Decimal("3.96"), failures={first: 1})
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is False
        assert result.evidence is None
        assert not output.exists()
        assert calls == [first]
        assert isinstance(result.live, MemoryBenchmarkLiveMetrics)
        assert result.live.stopped_by == "cost_ceiling"
        assert result.live.ceiling_hits == 1
        assert result.live.total_cost_usd == Decimal("3.96")
        assert result.failure_summary is not None
        assert "cost_ceiling" in result.failure_summary

    async def test_failure_returns_diagnostics_and_leaves_no_artifact(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        self._wire(monkeypatch, calls=calls, with_memory_knows=False)
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is False
        assert result.failure_summary is not None
        assert "lift" in result.failure_summary
        assert "11" in result.failure_summary
        assert result.evidence is None
        assert isinstance(result.live, MemoryBenchmarkLiveMetrics)
        assert result.live.lift == 0
        assert not output.exists()

    async def test_cost_ceiling_stops_before_exceeding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        self._wire(monkeypatch, calls=calls, cost=Decimal("0.50"))
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is False
        assert result.evidence is None
        assert not output.exists()
        assert isinstance(result.live, MemoryBenchmarkLiveMetrics)
        assert result.live.ceiling_hits == 1
        assert result.live.stopped_by == "cost_ceiling"
        assert result.live.total_cost_usd == Decimal("4.00")
        assert result.live.total_cost_usd <= LIVE_COST_CEILING_USD
        assert len(calls) == 8
        assert len(result.live.probes) == 4
        assert result.failure_summary is not None
        assert "cost_ceiling" in result.failure_summary

    async def test_zero_cost_model_aborts_with_unenforceable_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        self._wire(monkeypatch, calls=calls, cost=Decimal("0"))
        output = tmp_path / "memory-benchmark-evidence.json"

        result = await memory_benchmark_live.run_live_benchmark(
            _REPOSITORY,
            model_policy="balanced",
            policy_profile="default",
            build_ref="0123456789ab",
            output=output,
        )

        assert result is not None
        assert result.passed is False
        assert result.failure_summary is not None
        assert "model pricing unavailable; ceiling unenforceable" in result.failure_summary
        assert result.evidence is None
        assert not output.exists()
        assert len(calls) == 1

    async def test_opt_in_gate_returns_none_without_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        self._wire(monkeypatch, calls=calls)
        monkeypatch.delenv("RUN_LIVE_MODEL_TESTS", raising=False)
        output = tmp_path / "memory-benchmark-evidence.json"

        assert (
            await memory_benchmark_live.run_live_benchmark(
                _REPOSITORY,
                model_policy="balanced",
                policy_profile="default",
                build_ref="0123456789ab",
                output=output,
            )
            is None
        )
        assert calls == []
        assert not output.exists()


def test_compare_to_baseline_sorts_an_attribution_partition_move_as_a_shift() -> None:
    """A recalled belief changing which arm found it is neither better nor worse.

    The three attribution counts partition `needed_recalled`; one rising while
    another falls says where a belief was found, not how much was recalled.
    """

    baseline = _baseline_of(_result())
    first, second = _scenario_results()
    moved = second.probes[0].model_copy(update={"recalled_in_turn_only": 0, "recalled_both": 2})

    comparison = compare_to_baseline(
        _result([first, second.model_copy(update={"probes": [moved]})]), baseline
    )

    assert comparison.drift == []
    assert comparison.regressions == []
    assert comparison.improvements == []
    assert sorted(entry.split()[0] for entry in comparison.shifts) == [
        "recalled_both",
        "recalled_in_turn_only",
    ]
