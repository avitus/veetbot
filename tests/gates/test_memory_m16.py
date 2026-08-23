"""Milestone 16 memory-benchmark gates."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.config import Settings
from agent_core.domain.context import WorkingState
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryStatus,
    RecallMoment,
    RecallProfile,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    TextPart,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus
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
from agent_core.memory.formation import (
    FORMATION_POLICY_VERSION,
    MAX_INFERRED_CONFIDENCE,
    SESSION_IDLE_SECONDS,
    DeterministicCandidateExtractor,
)
from agent_core.memory.profiles import (
    FormationProfile,
    MemoryProfiles,
    RetrievalProfile,
    SnapshotProfiles,
)
from agent_core.runtime.worker import MaintenanceWorker
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


async def test_memory_profiles_are_loaded_and_mirror_shipped_defaults(tmp_path: Path) -> None:
    """The composition reads `memory/profiles.yaml`, defaults included."""

    shipped_settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")
    async with build(
        settings=shipped_settings,
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
        clock=FixedClock(_START),
    ) as composition:
        assert composition.memory_profiles == MemoryProfiles()
        assert composition.memory_retriever.retrieval_profile == RetrievalProfile()
        assert composition.memory.formation_profile == FormationProfile()

    config_dir = tmp_path / "config"
    overlay = config_dir / "memory" / "profiles.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        "retrieval:\n  reciprocal_rank_fusion_k: 7\ntraces:\n  operator_retention_days: 7\n",
        encoding="utf-8",
    )
    overlaid_settings = replace(shipped_settings, config_dir=config_dir)

    async with build(
        settings=overlaid_settings,
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
        clock=FixedClock(_START),
    ) as composition:
        assert composition.memory_profiles.retrieval.reciprocal_rank_fusion_k == 7
        assert composition.memory_retriever.retrieval_profile.reciprocal_rank_fusion_k == 7
        # One retention number governs every path that writes a recall trace.
        assert composition.memory_profiles.traces.operator_retention_days == 7
        assert composition.knowledge.trace_retention.operator_retention_days == 7
        assert composition.memory_profiles.formation == FormationProfile()
        assert composition.memory_profiles.snapshots == SnapshotProfiles()


def _session_boundary_settings(tmp_path: Path, *, enabled: bool) -> Settings:
    """Settings whose memory profile switches the session-boundary consolidations."""

    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")
    if enabled:
        return settings
    config_dir = tmp_path / "config"
    overlay = config_dir / "memory" / "profiles.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("formation:\n  session_boundary_enabled: false\n", encoding="utf-8")
    return replace(settings, config_dir=config_dir)


@pytest.mark.parametrize("enabled", [True, False])
async def test_session_boundary_switch_governs_both_consolidation_paths(
    tmp_path: Path, enabled: bool
) -> None:
    """`session_boundary_enabled` false stops the close hook and the idle sweep.

    Two sessions state two different things, so each consolidation path is
    observed on its own: the first is consolidated by closing it, the second by
    the maintenance sweep once it has been idle long enough.
    """

    clock = FixedClock(_START)
    async with build(
        settings=_session_boundary_settings(tmp_path, enabled=enabled),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="Noted.")], on_exhausted="repeat_last"),
        clock=clock,
    ) as composition:
        assert composition.memory_profiles.formation.session_boundary_enabled is enabled
        closed = await composition.runs.get(await composition.runs.submit("I have an Apple Watch."))
        idle = await composition.runs.get(await composition.runs.submit("I have a BMW X3."))
        assert closed.session_id != idle.session_id

        # The terminal-run flag is not part of the switch; it is recorded either way.
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(idle.session_id, 0, composition.principal)
        assert [
            event.event_type for event in events if event.event_type == "memory.formation.requested"
        ] == ["memory.formation.requested"]

        await composition.services.sessions.close(composition.principal, closed.session_id)
        after_close = {belief.subject for belief in await composition.memory.list_memories()}
        assert after_close == ({"Apple Watch"} if enabled else set())

        clock.advance(timedelta(seconds=SESSION_IDLE_SECONDS))
        maintenance = cast(MaintenanceWorker, composition.maintenance_factory())
        await maintenance.run_once()
        after_sweep = {belief.subject for belief in await composition.memory.list_memories()}
        assert after_sweep == ({"Apple Watch", "BMW X3"} if enabled else set())


async def test_session_project_scope_governs_consolidation_and_in_turn_recall(
    tmp_path: Path,
) -> None:
    """Both consolidation paths and the in-turn query read the session's project.

    One session is consolidated by closing it and one by the idle sweep, so
    neither path can borrow the other's scope, and a third session's turn shows
    the in-turn recall query carrying the project rather than `general`.
    """

    clock = FixedClock(_START)
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="Noted.")], on_exhausted="repeat_last"),
        clock=clock,
    ) as composition:
        closed = await composition.services.sessions.create(
            composition.principal, "general", {"project_scope": "atlas"}
        )
        idle = await composition.services.sessions.create(
            composition.principal, "general", {"project_scope": "borealis"}
        )
        await composition.runs.wait_terminal(
            await composition.runs.submit("I have an Apple Watch.", closed.id)
        )
        await composition.runs.wait_terminal(
            await composition.runs.submit("I have a BMW X3.", idle.id)
        )

        await composition.services.sessions.close(composition.principal, closed.id)
        clock.advance(timedelta(seconds=SESSION_IDLE_SECONDS))
        maintenance = cast(MaintenanceWorker, composition.maintenance_factory())
        await maintenance.run_once()

        scopes = {
            belief.subject: belief.scope for belief in await composition.memory.list_memories()
        }
        assert scopes == {"Apple Watch": "atlas", "BMW X3": "borealis"}

        probe = await composition.services.sessions.create(
            composition.principal, "general", {"project_scope": "atlas"}
        )
        await composition.runs.wait_terminal(
            await composition.runs.submit("Which watch do I have?", probe.id)
        )
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(probe.id, 0, composition.principal)
            traces = [
                await uow.traces.get(UUID(str(event.payload["trace_id"])), composition.principal)
                for event in events
                if event.event_type == "memory.recalled"
            ]
        in_turn = [trace for trace in traces if trace.moment is RecallMoment.IN_TURN]
        assert in_turn
        assert {trace.query.current_scope for trace in in_turn} == {"atlas"}


async def _stated_belief(
    composition: Any,
    session_id: UUID,
    statement: str,
    subject: str,
    *,
    belief_type: BeliefType = BeliefType.PREFERENCE,
    explicit: bool = True,
    confidence: float | None = None,
) -> Any:
    """State one belief the way a user turn would, with real provenance."""

    async with composition.uow_factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=composition.principal.principal_id,
                payload={"content": statement},
            )
        )
    return await composition.memory.remember(
        session_id=session_id,
        run_id=None,
        statement=statement,
        subject=subject,
        scope="general",
        belief_type=belief_type,
        sensitivity=Sensitivity.INTERNAL,
        source_event_ids=[event.sequence],
        explicit=explicit,
        confidence=confidence,
    )


async def test_trace_operator_fields_expire_on_schedule(tmp_path: Path) -> None:
    """The operator tier of a recall trace expires; the user tier does not.

    Two beliefs compete for one item of budget, so the trace carries a dropped
    identifier the sweep must turn into a count. One recall is left to age past
    the profile's retention and one is taken after it, so a single maintenance
    pass is observed sweeping the expired trace and leaving the fresh one whole.
    """

    clock = FixedClock(_START)
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
        clock=clock,
    ) as composition:
        session_id = await composition.sessions.create()
        await _stated_belief(
            composition, session_id, "User prefers concise answers", "answer style"
        )
        await _stated_belief(composition, session_id, "User prefers dark themes", "theme")
        query = RecallQuery(
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            current_scope="general",
            text="prefers",
            budget_tokens=500,
            max_items=1,
            min_score=0.1,
        )
        expiring_turn = UUID(int=0xE1)
        fresh_turn = UUID(int=0xE2)
        expiring = await composition.memory_retriever.recall(
            query, session_id=session_id, turn_id=expiring_turn
        )

        retention = composition.memory_profiles.traces.operator_retention_days
        clock.advance(timedelta(days=retention, seconds=1))
        fresh = await composition.memory_retriever.recall(
            query, session_id=session_id, turn_id=fresh_turn
        )
        maintenance = cast(MaintenanceWorker, composition.maintenance_factory())
        await maintenance.run_once()

        async with composition.uow_factory() as uow:
            swept = await uow.traces.get(expiring.trace_id, composition.principal)
            untouched = await uow.traces.get(fresh.trace_id, composition.principal)
            view = await uow.traces.user_view(expiring_turn, "private", "restricted")
        returned = [item.belief_id for item in expiring.items]
        assert swept.arm_latencies_ms == {}
        assert swept.candidates == 0
        assert swept.dropped_for_budget == []
        assert swept.dropped_for_budget_count == 1
        assert [item.belief_id for item in swept.beliefs] == returned
        assert swept.returned == returned
        assert swept.rendered == expiring.rendered
        assert untouched.arm_latencies_ms != {}
        assert untouched.candidates == 2
        assert len(untouched.dropped_for_budget) == 1
        assert [belief.statement for belief in view.beliefs] == [
            item.statement for item in expiring.items
        ]
        assert view.considered_not_shown == 1

        # A second pass has nothing left to null on the trace it already swept.
        await maintenance.run_once()
        async with composition.uow_factory() as uow:
            assert await uow.traces.get(expiring.trace_id, composition.principal) == swept


async def test_decay_sweep_lowers_unused_provisional_and_retires_below_floor(
    tmp_path: Path,
) -> None:
    """Maintenance decays idle provisional beliefs and retires the spent ones.

    Four beliefs sit in one session: an idle provisional fact, a provisional
    fact one step above the floor, an explicit user statement, and a
    provisional fact corroborated inside its time constant. Advancing the clock
    past the fact tau and running one maintenance pass shows the sweep on its
    timer touching exactly the first two.
    """

    clock = FixedClock(_START)
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
        clock=clock,
    ) as composition:
        session_id = await composition.sessions.create()
        idle = await _stated_belief(
            composition,
            session_id,
            "The deploy pipeline gates on CI.",
            "deploy gating",
            belief_type=BeliefType.FACT,
            explicit=False,
        )
        spent = await _stated_belief(
            composition,
            session_id,
            "The staging cluster runs in us-east-2.",
            "staging cluster",
            belief_type=BeliefType.FACT,
            explicit=False,
            confidence=0.2,
        )
        stated = await _stated_belief(
            composition, session_id, "User prefers concise answers", "answer style"
        )
        fading = await _stated_belief(
            composition,
            session_id,
            "Retries are capped at three.",
            "retry policy",
            belief_type=BeliefType.FACT,
            explicit=False,
            confidence=0.3,
        )
        assert composition.memory_profiles.formation.scheduled_enabled is True
        tau_days = composition.memory_profiles.retrieval.decay_tau_days.fact

        clock.advance(timedelta(days=tau_days + 1))
        reinforced = await _stated_belief(
            composition,
            session_id,
            "Retries are capped at three.",
            "retry policy",
            belief_type=BeliefType.FACT,
            explicit=False,
        )
        assert reinforced.id == fading.id
        clock.advance(timedelta(days=2))
        maintenance = cast(MaintenanceWorker, composition.maintenance_factory())
        await maintenance.run_once()

        beliefs = {
            belief.id: belief
            for belief in await composition.memory.list_memories(include_inactive=True)
        }
        assert beliefs[idle.id].confidence == pytest.approx(0.5)
        assert beliefs[idle.id].status is MemoryStatus.PROVISIONAL
        assert beliefs[idle.id].valid_to is None
        assert beliefs[spent.id].confidence == pytest.approx(0.15)
        assert beliefs[spent.id].status is MemoryStatus.RETIRED
        assert beliefs[spent.id].valid_to == clock.now()
        assert beliefs[stated.id] == stated
        assert beliefs[fading.id] == reinforced

        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        decay_events = {
            event.event_type: event.payload["belief"]["id"]
            for event in events
            if event.event_type in {"memory.decayed", "memory.retired"}
        }
        assert decay_events == {
            "memory.decayed": str(idle.id),
            "memory.retired": str(spent.id),
        }

        # The sweep runs on its interval, so a second pass in the same window
        # leaves the store exactly as the first one left it.
        await maintenance.run_once()
        assert {
            belief.id: belief
            for belief in await composition.memory.list_memories(include_inactive=True)
        } == beliefs


async def test_usage_feedback_marks_cited_and_never_raises_confidence(tmp_path: Path) -> None:
    """Citing a belief resists decay, moves utility, and leaves confidence alone.

    One turn recalls two stated preferences and the answer cites one of them by
    the identifier memory rendered, so the completion hook is observed marking
    that belief used on the turn's trace, raising its utility, and moving its
    reinforcement instant a day forward, while the belief that was returned and
    never used loses utility instead. Neither confidence moves: usage is
    evidence of usefulness, never of truth. Completing the run again finds its
    own citation event and changes nothing.
    """

    clock = FixedClock(_START)
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=clock,
    ) as composition:
        session_id = await composition.sessions.create()
        cited = await _stated_belief(
            composition, session_id, "User prefers concise answers", "answer style"
        )
        uncited = await _stated_belief(composition, session_id, "User prefers dark themes", "theme")
        clock.advance(timedelta(days=1))
        # The model answers with the citation form the renderer emits, which is
        # the only signal the hook reads out of a completed run.
        script.turns[0] = ScriptedTurn(text=f"Per [m:{str(cited.id)[:8]}]: dark, and briefly.")

        run_id = await composition.runs.submit("Theme and answer style?", session_id)
        run = await composition.runs.get(run_id)
        assert run.final_message is not None
        assert str(cited.id)[:8] in run.final_message

        async with composition.uow_factory() as uow:
            view = await uow.traces.user_view(run_id, "private", "restricted")
            events = await uow.events.list_after(session_id, 0, composition.principal)
        used = {belief.belief_id: belief.used for belief in view.beliefs}
        assert used == {cited.id: True, uncited.id: False}

        beliefs = {belief.id: belief for belief in await composition.memory.list_memories()}
        assert beliefs[cited.id].utility > 0
        assert beliefs[cited.id].confidence == cited.confidence
        assert beliefs[cited.id].last_reinforced_at == clock.now()
        assert beliefs[cited.id].last_reinforced_at > cited.last_reinforced_at
        assert beliefs[uncited.id].utility < 0
        assert beliefs[uncited.id].confidence == uncited.confidence
        assert beliefs[uncited.id].last_reinforced_at == uncited.last_reinforced_at

        citations = [event for event in events if event.event_type == "memory.cited"]
        assert [event.payload["cited"] for event in citations] == [[str(cited.id)]]
        assert citations[0].payload["uncited"] == [str(uncited.id)]

        # The re-entrant completion path re-enters the hook with the same run.
        repeated = await composition.memory.record_usage(
            session_id=session_id, run_id=run_id, final_text=run.final_message
        )
        assert (repeated.cited, repeated.uncited, repeated.traces) == (0, 0, 0)
        assert {belief.id: belief for belief in await composition.memory.list_memories()} == beliefs


def _memory_blocks(request: Any) -> list[str]:
    """The memory-trust messages of one request's body, envelopes and all.

    The frozen prefix carries the session-open snapshot as a memory-trust
    message of its own, so the body begins after the region-A items the
    request counted.
    """

    body = request.conversation[int(request.metadata["region_a_items"]) :]
    return [
        "\n".join(part.text for part in item.content if isinstance(part, TextPart))
        for item in body
        if isinstance(item, UserMessage) and item.trust is TrustLevel.MEMORY
    ]


async def _in_turn_traces(composition: Any, session_id: UUID, run_id: UUID) -> list[Any]:
    """Every in-turn recall trace one run recorded, in event order."""

    async with composition.uow_factory() as uow:
        events = await uow.events.list_after(session_id, 0, composition.principal)
        traces = [
            await uow.traces.get(UUID(str(event.payload["trace_id"])), composition.principal)
            for event in events
            if event.event_type == "memory.recalled" and event.run_id == run_id
        ]
    return [trace for trace in traces if trace.moment is RecallMoment.IN_TURN]


async def test_recall_delta_surfaces_post_snapshot_beliefs(tmp_path: Path) -> None:
    """A belief written after the snapshot reaches the next turn through the delta.

    The session's first turn freezes the prefix and its snapshot. The belief
    stated afterwards is invisible to that snapshot and to a base recall the
    turn's own words never reach, so the only thing that can carry it is the
    delta query bounded by the snapshot watermark — and it arrives without the
    cached prefix being rewritten.
    """

    clock = FixedClock(_START)
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
        clock=clock,
    ) as composition:
        provider = composition.executor._model_provider
        planner = composition.executor._context_planner
        assert isinstance(provider, FakeModelProvider)
        session_id = await composition.sessions.create()
        before = await _stated_belief(composition, session_id, "User prefers dark themes", "theme")
        await composition.runs.wait_terminal(
            await composition.runs.submit("How should you answer?", session_id)
        )
        opening = provider.requests[-1]
        plan = await planner.current(session_id)
        assert plan is not None and plan.snapshot_id is not None
        assert plan.snapshot_watermark == before.store_position

        stated = await _stated_belief(
            composition, session_id, "User prefers concise answers", "answer style"
        )
        assert stated.store_position > plan.snapshot_watermark

        later_run = await composition.runs.wait_terminal(
            await composition.runs.submit("What is the plan for tomorrow?", session_id)
        )
        latest = provider.requests[-1]

        # The prefix is frozen: the delta is a Region B addition, never a rewrite.
        assert latest.metadata["prefix_sha256"] == opening.metadata["prefix_sha256"]
        unrotated = await planner.current(session_id)
        assert unrotated is not None
        assert unrotated.snapshot_id == plan.snapshot_id
        assert any(stated.statement in block for block in _memory_blocks(latest))

        traces = await _in_turn_traces(composition, session_id, later_run.id)
        base = [trace for trace in traces if trace.query.text is not None]
        delta = [trace for trace in traces if trace.query.text is None]
        assert len(base) == 1
        assert len(delta) == 1
        # The turn's own words never reached the belief; the delta did, and it
        # reached only what the snapshot had not already seen.
        assert stated.id not in base[0].returned
        assert delta[0].returned == [stated.id]
        assert delta[0].query.profile is RecallProfile.CORE
        assert delta[0].query.min_store_position == plan.snapshot_watermark
        assert before.id not in delta[0].returned


async def test_snapshot_correction_lines_never_yield_and_prefix_is_stable(
    tmp_path: Path,
) -> None:
    """A superseded snapshot member is corrected in the next turn, and stays corrected.

    The snapshot lives in the cached prefix and is never rewritten, so the
    correction line is the only thing that can say the belief it renders no
    longer holds. It is fixed body: budget pressure that drops both recall
    blocks leaves it in place, and it is never offered for yielding.
    """

    clock = FixedClock(_START)
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
        clock=clock,
    ) as composition:
        provider = composition.executor._model_provider
        planner = composition.executor._context_planner
        builder = composition.executor._context_builder
        assert isinstance(provider, FakeModelProvider)
        session_id = await composition.sessions.create()
        stated = await _stated_belief(
            composition, session_id, "User prefers concise answers", "answer style"
        )
        await composition.runs.wait_terminal(
            await composition.runs.submit("How should you answer?", session_id)
        )
        opening = provider.requests[-1]
        plan = await planner.current(session_id)
        assert plan is not None
        assert f"[m:{str(stated.id)[:8]}]" in plan.memory_snapshot

        clock.advance(timedelta(days=1))
        replacement = await _stated_belief(
            composition, session_id, "User prefers detailed answers", "answer style"
        )
        assert replacement.id != stated.id
        beliefs = {
            belief.id: belief
            for belief in await composition.memory.list_memories(include_inactive=True)
        }
        assert beliefs[stated.id].status is MemoryStatus.SUPERSEDED
        assert beliefs[stated.id].store_position > plan.snapshot_watermark

        run_id = await composition.runs.submit("What now?", session_id)
        later_run = await composition.runs.wait_terminal(run_id)
        latest = provider.requests[-1]
        line = (
            f"correction: [m:{str(stated.id)[:8]}] no longer holds as of "
            f"{clock.now().isoformat().replace('+00:00', 'Z')}; "
            f"superseded by [m:{str(replacement.id)[:8]}]."
        )

        assert latest.metadata["prefix_sha256"] == opening.metadata["prefix_sha256"]
        assert plan.memory_snapshot in "\n".join(
            "\n".join(part.text for part in item.content if isinstance(part, TextPart))
            for item in latest.conversation
            if isinstance(item, UserMessage)
        )
        assert any(line in block for block in _memory_blocks(latest))

        # Budget pressure that drops both recall blocks leaves the correction.
        async with composition.uow_factory() as uow:
            agent = await uow.agents.get_version(later_run.agent_id, later_run.agent_version)
        checkpoint = RunCheckpoint(
            run_id=later_run.id,
            version=1,
            status=RunStatus.RUNNING,
            conversation=[UserMessage(content=[TextPart(text="What now?")])],
            created_at=clock.now(),
        )
        roomy = await builder.assemble(later_run, checkpoint, agent, composition.principal)
        crowded = checkpoint.model_copy(
            update={
                "conversation": [
                    UserMessage(
                        content=[
                            TextPart(text="What now? " + "context filler. " * 20_000),
                        ]
                    )
                ]
            }
        )
        squeezed = await builder.assemble(later_run, crowded, agent, composition.principal)

        assert roomy.pressure.yield_steps == ()
        assert any(line in block for block in _memory_blocks(roomy.request))
        assert squeezed.pressure.yield_steps[0] == "recall"
        assert "corrections" not in squeezed.pressure.yield_steps
        # Every memory block the squeezed body still carries is the correction:
        # the recall blocks yielded, the override did not.
        squeezed_blocks = _memory_blocks(squeezed.request)
        assert len(squeezed_blocks) == 1
        assert line in squeezed_blocks[0]
        assert "<memory" not in squeezed_blocks[0]


# Session events are allocated in order from one: the session's creation, the
# tool result seeded before the run, then the run's own user message. The test
# asserts both, so a change in that order fails loudly instead of silently
# re-pointing the scripted provenance.
_TOOL_EVENT_SEQUENCE = 2
_USER_EVENT_SEQUENCE = 3


async def test_established_facts_enter_formation_as_affirmed_candidates(tmp_path: Path) -> None:
    """A fact the run established from a user event forms; a tool-sourced one does not.

    One run writes two established facts through the control tool: one whose
    provenance is the user's own message and one whose provenance is a tool
    result already in the session. After the idle boundary consolidates the
    session, exactly the first is a belief — affirmed, provisional, capped at
    the inferred confidence, and carrying the fact's provenance rather than
    the window's — while the tool-sourced fact is never proposed at all.
    """

    clock = FixedClock(_START)
    helios = "The Helios deploy gate requires two approvals."
    ares = "The Ares cluster belongs to the platform team."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="context.update_working_state",
                        call_id="establish-facts",
                        arguments={
                            "add_facts": [
                                {
                                    "statement": helios,
                                    "source_event_ids": [_USER_EVENT_SEQUENCE],
                                },
                                {
                                    "statement": ares,
                                    "source_event_ids": [_TOOL_EVENT_SEQUENCE],
                                },
                            ]
                        },
                    )
                ]
            ),
            ScriptedTurn(text="Noted."),
        ],
        on_exhausted="repeat_last",
    )
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=clock,
    ) as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            tool_event = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="tool.call.completed",
                    actor_type="tool",
                    payload={
                        "name": "web.fetch",
                        "call_id": "seeded-tool-call",
                        "reason_code": "tool.succeeded",
                        "result_item": ToolResultItem(
                            call_id="seeded-tool-call",
                            content=[TextPart(text=ares)],
                            trust=TrustLevel.EXTERNAL_UNTRUSTED,
                        ).model_dump(mode="json"),
                    },
                )
            )
        assert tool_event.sequence == _TOOL_EVENT_SEQUENCE
        run = await composition.runs.wait_terminal(
            await composition.runs.submit(helios, session_id)
        )
        assert run.status is RunStatus.COMPLETED

        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        user_event = next(event for event in events if event.event_type == "user.message.created")
        assert user_event.sequence == _USER_EVENT_SEQUENCE
        state_event = next(
            event for event in events if event.event_type == "context.working_state.updated"
        )
        state = WorkingState.model_validate(state_event.payload["working_state"])
        assert [fact.statement for fact in state.established_facts] == [helios, ares]
        assert await composition.memory.list_memories() == []

        clock.advance(timedelta(seconds=SESSION_IDLE_SECONDS + 1))
        maintenance = cast(MaintenanceWorker, composition.maintenance_factory())
        await maintenance.run_once()

        beliefs = await composition.memory.list_memories()
        audits = await composition.memory.list_consolidations(session_id=session_id)

    assert [(belief.subject, belief.statement) for belief in beliefs] == [("Helios", helios)]
    formed = beliefs[0]
    assert formed.authority is MemoryAuthority.AFFIRMED
    assert formed.status is MemoryStatus.PROVISIONAL
    assert formed.confidence <= MAX_INFERRED_CONFIDENCE
    assert formed.belief_type is BeliefType.FACT
    assert formed.source_event_ids == [_USER_EVENT_SEQUENCE]
    assert [
        (audit.candidates_proposed, audit.committed, audit.rejected, audit.policy_version)
        for audit in audits
    ] == [(1, 1, 0, FORMATION_POLICY_VERSION)]
