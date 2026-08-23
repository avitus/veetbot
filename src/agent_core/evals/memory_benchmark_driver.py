"""Driver, runner, and baseline writer for the multi-session memory benchmark.

The pure core lives in :mod:`agent_core.evals.memory_benchmark`; this module is
the half that runs.  It composes one real application graph per scenario, feeds
the scenario's conversations through it, consolidates each session, and then
asks the scenario's probes and reads the recall traces back out.

The composition root is imported lazily inside the functions that need it, the
way the sibling memory-formation evaluation does, because nothing in production
may import the evaluation package.
"""

from __future__ import annotations

import importlib
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SerializeAsAny, model_validator

from agent_core.config import Settings, load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import MemoryRecord, RecallMoment, RecallTrace
from agent_core.domain.messages import AssistantMessage, FakeModelScript, ScriptedTurn, TextPart
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, RunLimits
from agent_core.evals.memory_benchmark import (
    BENCHMARK_VERSION,
    BaselineComparison,
    BenchmarkProbe,
    BenchmarkScenario,
    BenchmarkSession,
    BenchmarkTurn,
    ConsolidationCounts,
    DeterministicBenchmarkResult,
    DeterministicScenarioResult,
    MemoryBenchmarkBaseline,
    MemoryBenchmarkCorpus,
    ProbeRetrievalResult,
    SessionEvents,
    aggregate_deterministic,
    baseline_probe_rows,
    compare_to_baseline,
    evidence_event_refs,
    evidence_provenance_recalled,
    load_baseline,
    load_corpus,
    probe_run_facts,
    score_formation,
    score_probe,
)
from agent_core.evals.memory_formation import (
    EvaluationBelief,
    _evaluation_settings,
    _write_evidence,
)
from agent_core.memory.formation import FORMATION_POLICY_VERSION
from agent_core.memory.provider_extraction import PROVIDER_FORMATION_POLICY_VERSION
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.determinism import Clock

EVALUATION_PRINCIPAL = Principal(
    tenant_id="evaluation",
    principal_id="memory-benchmark-evaluator",
    roles={"evaluator"},
    scopes=set(PLATFORM_SCOPES),
)
DEFAULT_SCOPE = "general"
PROBE_ACK_TEXT = "benchmark-probe-ack"
PROBE_TOOLS = ["memory.search", "memory.recall_episodes"]
PROBE_RUN_COST_CEILING_USD = Decimal("0.05")
PROBE_RUN_LIMITS = RunLimits(
    max_steps=6,
    max_model_calls=3,
    max_tool_calls=3,
    max_cost=PROBE_RUN_COST_CEILING_USD,
)


class MemoryBenchmarkResult(BaseModel):
    """One benchmark command's outcome, printed as the command's document.

    `baseline` carries the comparison against the recorded baseline, or None
    when none is recorded yet.  `live` and `evidence` are set by the live arm
    in the sibling live module, and the validator states the rule they answer
    to — a live arm publishes evidence exactly when it passed, and evidence
    without a live arm is meaningless.  Both are typed as `BaseModel` here so
    that the pure driver never imports the live arm that defines them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    failure_summary: str | None
    deterministic: DeterministicBenchmarkResult
    baseline: BaselineComparison | None = None
    live: SerializeAsAny[BaseModel] | None = None
    evidence: SerializeAsAny[BaseModel] | None = None

    @model_validator(mode="after")
    def outcome_matches_evidence(self) -> MemoryBenchmarkResult:
        if self.passed and self.failure_summary is not None:
            raise ValueError("a passing memory benchmark must not carry a failure summary")
        if not self.passed and not self.failure_summary:
            raise ValueError("a failed memory benchmark requires a failure summary")
        if self.live is not None and self.passed != (self.evidence is not None):
            raise ValueError("only a passing live benchmark may carry activation evidence")
        if self.live is None and self.evidence is not None:
            raise ValueError("activation evidence requires a live benchmark arm")
        return self


async def run_deterministic_benchmark(
    repository_root: Path,
    *,
    settings: Settings | None = None,
    policy_profile: str = "default",
) -> DeterministicBenchmarkResult:
    """Run every corpus scenario and aggregate the run's integer metrics.

    Without explicit settings the run borrows the memory-formation evaluation's
    settings, which force the development deployment, the fake sandbox, and the
    deterministic extractor, over a temporary artifact root that is removed
    when the run ends.
    """

    corpus, corpus_sha256 = load_corpus(repository_root)
    if settings is not None:
        return await _benchmark(settings, corpus, corpus_sha256, policy_profile=policy_profile)
    with tempfile.TemporaryDirectory(prefix="agent-memory-benchmark-") as temporary_root:
        borrowed = _borrowed_settings(temporary_root)
        return await _benchmark(borrowed, corpus, corpus_sha256, policy_profile=policy_profile)


def _borrowed_settings(temporary_root: str) -> Settings:
    """Reuse the memory-formation evaluation's settings over a scratch root."""

    return _evaluation_settings(load_settings(), Path(temporary_root) / "artifacts")


async def _benchmark(
    settings: Settings,
    corpus: MemoryBenchmarkCorpus,
    corpus_sha256: str,
    *,
    policy_profile: str,
) -> DeterministicBenchmarkResult:
    scenarios = [
        await run_deterministic_scenario(
            settings, scenario, corpus=corpus, policy_profile=policy_profile
        )
        for scenario in corpus.scenarios
    ]
    if not scenarios:
        raise ValueError("a benchmark run needs at least one scenario to name its extractor")
    return DeterministicBenchmarkResult(
        benchmark_version=BENCHMARK_VERSION,
        corpus_sha256=corpus_sha256,
        formation_policy_version=FORMATION_POLICY_VERSION,
        provider_formation_policy_version=PROVIDER_FORMATION_POLICY_VERSION,
        retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
        # Every scenario composes from the same settings, so the first one's
        # extractor is the run's extractor.
        extractor_name=scenarios[0].extractor_name,
        scenarios=scenarios,
        metrics=aggregate_deterministic(scenarios),
    )


def write_baseline(
    result: DeterministicBenchmarkResult,
    *,
    build_ref: str,
    recorded_at: datetime,
    path: Path,
) -> MemoryBenchmarkBaseline:
    """Record a deterministic run as the baseline later runs compare against.

    The rows drop the trace identifiers, which are composition detail rather
    than measurement, and the write refuses an existing file: re-recording a
    baseline is removing the old one deliberately, in the change that moved the
    numbers.
    """

    baseline = MemoryBenchmarkBaseline(
        benchmark_version=result.benchmark_version,
        corpus_sha256=result.corpus_sha256,
        formation_policy_version=result.formation_policy_version,
        provider_formation_policy_version=result.provider_formation_policy_version,
        retrieval_policy_version=result.retrieval_policy_version,
        extractor_name=result.extractor_name,
        build_ref=build_ref,
        recorded_at=recorded_at,
        metrics=result.metrics,
        probes=baseline_probe_rows(result),
    )
    _write_evidence(path, baseline)
    return baseline


async def run_benchmark(
    repository_root: Path,
    *,
    deterministic_only: bool,
    model_policy: str,
    policy_profile: str,
    build_ref: str,
    output: Path | None,
    baseline_output: Path | None,
    clock: Clock | None = None,
) -> MemoryBenchmarkResult | None:
    """Run the benchmark the command asked for and judge it.

    This is the single entry point for both arms.  The live arm owns
    `model_policy` and `output`, publishes its own evidence, and gates itself
    on `RUN_LIVE_MODEL_TESTS`, returning None on a clean skip; it lives in a
    sibling module imported lazily here so that the pure driver never depends
    on it.  The deterministic arm writes no evidence, so an `--output` path
    handed to it is a mistake rather than something to ignore, and it is
    compared against the recorded baseline when there is one; any difference
    (drift, regression, improvement, or shift) fails the run.  `clock` stamps a
    recorded baseline and
    defaults to the composition root's wall clock, because evaluation code
    never reads the ambient one.
    """

    if not deterministic_only:
        if output is None:
            raise ValueError("the live memory benchmark arm requires an evidence output path")
        live = importlib.import_module("agent_core.evals.memory_benchmark_live")
        result: MemoryBenchmarkResult | None = await live.run_live_benchmark(
            repository_root,
            model_policy=model_policy,
            policy_profile=policy_profile,
            build_ref=build_ref,
            output=output,
        )
        return result
    if output is not None:
        raise ValueError(
            "an evidence output path belongs to the live arm; "
            "the deterministic arm records a baseline instead"
        )
    deterministic = await run_deterministic_benchmark(
        repository_root, policy_profile=policy_profile
    )
    recorded = load_baseline(repository_root)
    comparison = None if recorded is None else compare_to_baseline(deterministic, recorded)
    if baseline_output is not None:
        # Defer the composition-root import to avoid an evaluation/bootstrap cycle.
        bootstrap = importlib.import_module("agent_core.bootstrap")
        recording_clock: Clock = clock or bootstrap.system_clock()
        write_baseline(
            deterministic,
            build_ref=build_ref,
            recorded_at=recording_clock.now(),
            path=baseline_output,
        )
    failures = (
        []
        if comparison is None
        else [
            *comparison.drift,
            *comparison.regressions,
            *comparison.improvements,
            *comparison.shifts,
        ]
    )
    return MemoryBenchmarkResult(
        passed=not failures,
        failure_summary="; ".join(failures) if failures else None,
        deterministic=deterministic,
        baseline=comparison,
    )


def probe_prompt(probe: BenchmarkProbe, corpus: MemoryBenchmarkCorpus, *, live: bool) -> str:
    """Compose the prompt one probe sends.

    The deterministic arm sends the bare question: it scores retrieval, and the
    corpus instruction would only dilute the lexical query the in-turn former
    builds from the turn.  The live arm appends the instruction, because there
    an answer is scored and the abstain phrasing has to be stated.
    """

    if not live:
        return probe.question
    return f"{probe.question}\n{corpus.probe_instruction}"


@asynccontextmanager
async def scenario_composition(
    settings: Settings,
    scenario: BenchmarkScenario,
    *,
    policy_profile: str,
    script: FakeModelScript | None = None,
    model_policy: str | None = None,
    replay_sessions: bool = True,
    session_events: dict[str, SessionEvents] | None = None,
) -> AsyncIterator[tuple[Any, list[ConsolidationCounts]]]:
    """Compose one scenario's application graph and leave it open for probes.

    Sessions are consolidated by calling the memory service directly rather
    than by closing them, because the close hook and the idle sweep both
    consolidate at the `general` scope and a project-scoped scenario closed
    through them would form nothing; this is the documented harness path.
    Maintenance runs exactly once, after the last session and before the first
    probe, so that no probe can change the store the next probe reads.  The
    live arm reuses this with a real `model_policy` and no script, and its
    without-memory arm reuses it with `replay_sessions=False`, which composes
    the same graph over a store the scenario never spoke to.
    """

    # Defer the composition-root import to avoid an evaluation/bootstrap cycle.
    bootstrap = importlib.import_module("agent_core.bootstrap")
    consolidations: list[ConsolidationCounts] = []
    async with bootstrap.build(
        settings=settings,
        storage="memory",
        script=script,
        principal=EVALUATION_PRINCIPAL,
        policy_profile=policy_profile,
        fixed_clock_at=scenario.start_at,
        sequential_ids=True,
        enabled_tools=list(PROBE_TOOLS),
        limits=PROBE_RUN_LIMITS,
        model_policy=model_policy,
    ) as composition:
        if replay_sessions:
            for session in scenario.sessions:
                consolidations.append(
                    await _converse(composition, composition.clock, session, session_events)
                )
            await composition.maintenance_factory().run_once()
        yield composition, consolidations


async def run_deterministic_scenario(
    settings: Settings,
    scenario: BenchmarkScenario,
    *,
    corpus: MemoryBenchmarkCorpus,
    policy_profile: str = "default",
    session_events: dict[str, SessionEvents] | None = None,
) -> DeterministicScenarioResult:
    """Run one scenario's sessions and probes against a real composition.

    One composition serves the whole scenario, so every probe reads the store
    the scenario's own conversations built, and the scripted model keeps the
    arm off the network.  A caller that passes `session_events` is handed the
    events each replayed turn appended and gets the evidence-provenance counts
    on every probe; the authored corpus names labels rather than evidence and
    passes nothing.
    """

    script = FakeModelScript(turns=[ScriptedTurn(text=PROBE_ACK_TEXT)], on_exhausted="repeat_last")
    probes: list[ProbeRetrievalResult] = []
    async with scenario_composition(
        settings,
        scenario,
        policy_profile=policy_profile,
        script=script,
        session_events=session_events,
    ) as (composition, consolidations):
        clock = composition.clock
        for probe in scenario.probes:
            probes.append(await _probe(composition, clock, scenario, probe, corpus, session_events))
        extractor_name: str = composition.memory.extractor_name
        live: list[MemoryRecord] = await composition.memory.list_memories()
        every: list[MemoryRecord] = await composition.memory.list_memories(include_inactive=True)
    return DeterministicScenarioResult(
        scenario_id=scenario.id,
        extractor_name=extractor_name,
        formation=score_formation(scenario, live, every),
        consolidations=consolidations,
        probes=probes,
        beliefs=[
            EvaluationBelief(
                belief_type=record.belief_type.value,
                subject=record.subject,
                statement=record.statement,
            )
            for record in live
        ],
    )


async def _converse(
    composition: Any,
    clock: Any,
    session: BenchmarkSession,
    session_events: dict[str, SessionEvents] | None = None,
) -> ConsolidationCounts:
    """Replay one session's turns and consolidate what they stated.

    A turn the scenario marks as the assistant's is appended as an assistant
    message rather than as a principal one, so the conversation the dataset
    recorded is replayed whole while the extractor, which reads principal user
    messages only, can never form from it.  When `session_events` is given the
    replay records the event each turn appended, which is what the
    evidence-provenance metric joins a returned belief against.
    """

    _advance(clock, session.advance_seconds)
    view = await _open_session(composition, session.project_scope)
    sequences: list[int] = []
    for turn in session.turns:
        _advance(clock, turn.advance_seconds)
        async with composition.uow_factory() as uow:
            appended: EventEnvelope = await uow.events.append(_turn_event(view.id, turn))
        sequences.append(appended.sequence)
    if session_events is not None:
        session_events[session.id] = SessionEvents(session_id=view.id, turn_sequences=sequences)
    scope = session.project_scope or DEFAULT_SCOPE
    result = await composition.memory.run(trigger="session_close", scope=scope, session_id=view.id)
    return ConsolidationCounts(
        session_id=session.id,
        scope=scope,
        candidates_proposed=result.run.candidates_proposed,
        committed=result.run.committed,
        reinforced=result.run.reinforced,
        superseded=result.run.superseded,
        rejected=result.run.rejected,
    )


def _turn_event(session_id: UUID, turn: BenchmarkTurn) -> NewEvent:
    """Shape one replayed turn as the event its role would have appended."""

    if turn.role == "user":
        return NewEvent(
            session_id=session_id,
            run_id=None,
            event_type="user.message.created",
            actor_type="principal",
            actor_id=EVALUATION_PRINCIPAL.principal_id,
            payload={"content": turn.text},
        )
    message = AssistantMessage(content=[TextPart(text=turn.text)])
    return NewEvent(
        session_id=session_id,
        run_id=None,
        event_type="assistant.message.completed",
        actor_type="runtime",
        payload={"message": message.model_dump(mode="json")},
    )


async def _probe(
    composition: Any,
    clock: Any,
    scenario: BenchmarkScenario,
    probe: BenchmarkProbe,
    corpus: MemoryBenchmarkCorpus,
    session_events: Mapping[str, SessionEvents] | None = None,
) -> ProbeRetrievalResult:
    """Ask one probe in a fresh session and score what it recalled."""

    _advance(clock, probe.advance_seconds)
    store_live: list[MemoryRecord] = await composition.memory.list_memories()
    view = await _open_session(composition, probe.project_scope)
    run_id = await composition.runs.submit(probe_prompt(probe, corpus, live=False), view.id)
    run = await composition.runs.get(run_id)
    if run.status not in TERMINAL_RUN_STATUSES:
        run = await composition.runs.wait_terminal(run_id)
    async with composition.uow_factory() as uow:
        events: list[EventEnvelope] = await uow.events.list_after(view.id, 0, EVALUATION_PRINCIPAL)
    snapshot, in_turn = await _recall_traces(composition, events)
    distinct_prefixes, policy_failures, run_completed = probe_run_facts(events, run.status)
    named = set() if session_events is None else evidence_event_refs(probe, session_events)
    recalled = bool(named) and evidence_provenance_recalled(
        probe,
        [*([] if snapshot is None else [snapshot]), *in_turn],
        session_events or {},
        store=store_live,
    )
    return score_probe(
        probe,
        scenario,
        store_live=store_live,
        snapshot=snapshot,
        in_turn=in_turn,
        distinct_prefixes=distinct_prefixes,
        policy_failures=policy_failures,
        run_completed=run_completed,
        evidence_total=int(bool(named)),
        evidence_recalled=int(recalled),
        snapshot_trace_id=None if snapshot is None else snapshot.id,
        in_turn_trace_ids=[trace.id for trace in in_turn],
    )


async def _recall_traces(
    composition: Any, events: list[EventEnvelope]
) -> tuple[RecallTrace | None, list[RecallTrace]]:
    """Read the probe run's recall traces back, split by moment.

    A probe run plans once and takes one turn, so it records one snapshot
    trace; the first snapshot in event order is the one the frozen prefix
    carried. The turn itself records the base recall and, over a frozen
    snapshot, the delta bounded by its watermark, and every in-turn trace
    counts: a probe is scored on what the turn was told, from whichever read
    told it.
    """

    snapshot: RecallTrace | None = None
    in_turn: list[RecallTrace] = []
    for event in events:
        if event.event_type != "memory.recalled":
            continue
        identifier = event.payload.get("trace_id")
        if not isinstance(identifier, str):
            raise ValueError("a memory.recalled event carried no trace identifier")
        trace: RecallTrace = await composition.memory.get_recall_trace(UUID(identifier))
        if trace.moment is RecallMoment.SNAPSHOT:
            snapshot = trace if snapshot is None else snapshot
        elif trace.moment is RecallMoment.IN_TURN:
            in_turn.append(trace)
    return snapshot, in_turn


async def _open_session(composition: Any, project_scope: str | None) -> Any:
    """Open a session through the public service so metadata carries the scope."""

    metadata: dict[str, object] = {} if project_scope is None else {"project_scope": project_scope}
    return await composition.services.sessions.create(EVALUATION_PRINCIPAL, DEFAULT_SCOPE, metadata)


def _advance(clock: Any, seconds: int) -> None:
    """Move the scenario's fixed clock forward, or refuse a clock that cannot."""

    if not seconds:
        return
    advance = getattr(clock, "advance", None)
    if not callable(advance):
        raise ValueError("the deterministic memory benchmark requires a fixed clock")
    advance(timedelta(seconds=seconds))
