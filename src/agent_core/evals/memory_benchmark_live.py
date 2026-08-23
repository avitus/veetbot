"""Live arm of the multi-session memory benchmark and the evidence it publishes.

The deterministic arm measures what memory forms and recalls; this module
measures what that recall is worth.  Every probe is asked twice against a real
model — once against the composition the scenario's own conversations built,
and once against a composition that never saw them — and the difference between
the two is the only number that justifies the subsystem.

Nothing here runs without `RUN_LIVE_MODEL_TESTS=1`.  The arm is bounded by a
per-invocation cost ceiling enforced before each run is admitted, and it
publishes an artifact only when every pass condition held: the evidence model
re-derives its own thresholds from the counts inside it, so an artifact that
exists is an artifact that passed.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import tempfile
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.config import Settings, load_settings
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import MemoryRecord, minimum_supported_case_count
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, RunFailure, RunStatus
from agent_core.evals.memory_benchmark import (
    BenchmarkProbe,
    BenchmarkScenario,
    DeterministicMetrics,
    MemoryBenchmarkCorpus,
    ProbeAnswer,
    ProbeCategory,
    ProbeRetrievalResult,
    load_corpus,
    probe_run_facts,
    score_probe,
)
from agent_core.evals.memory_benchmark_driver import (
    EVALUATION_PRINCIPAL,
    PROBE_RUN_COST_CEILING_USD,
    MemoryBenchmarkResult,
    _advance,
    _open_session,
    _recall_traces,
    probe_prompt,
    run_deterministic_benchmark,
    scenario_composition,
)
from agent_core.evals.memory_formation import (
    _evaluation_settings,
    _normalized,
    _write_evidence,
)
from agent_core.ports.determinism import Clock

LIVE_COST_CEILING_USD = Decimal("4.00")
PROBE_WALL_SECONDS = 120

LiveArm = Literal["with_memory", "without_memory"]
_ARMS: tuple[LiveArm, ...] = ("with_memory", "without_memory")


class AnswerScore(BaseModel):
    """How one answer scored against its gold answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correct: bool
    abstained: bool
    leaked_protected: bool


def score_answer(
    answer: str | None,
    expected: ProbeAnswer,
    *,
    abstain_phrase: str,
    protected: Sequence[str],
) -> AnswerScore:
    """Score one live answer mechanically: no judge, and no partial credit.

    An answer abstains when its normalized text is exactly the normalized
    abstain phrase.  An `exact` or `alternatives` answer is correct when it is
    a single line, did not abstain, and names some gold value token-bounded, so
    that "Portlandia" never answers "Portland"; `all_of` needs every value.
    A protected fragment appearing anywhere in the answer is a leak whatever
    the answer scored.
    """

    text = answer or ""
    stripped = text.strip()
    normalized = _normalized(stripped)
    abstained = bool(normalized) and normalized == _normalized(abstain_phrase)
    single_line = len(stripped.splitlines()) == 1
    matches = (_names_value(normalized, value) for value in expected.values)
    match expected.kind:
        case "abstain":
            correct = abstained
        case "all_of":
            correct = single_line and not abstained and all(matches)
        case _:
            correct = single_line and not abstained and any(matches)
    return AnswerScore(
        correct=correct,
        abstained=abstained,
        leaked_protected=any(
            fragment in normalized for fragment in map(_normalized, protected) if fragment
        ),
    )


def _names_value(normalized_answer: str, value: str) -> bool:
    """Report whether a normalized answer names one gold value token-bounded."""

    target = _normalized(value)
    if not target:
        return False
    return re.search(rf"\b{re.escape(target)}\b", normalized_answer) is not None


def minimum_live_lift(answerable: int) -> int:
    """The twenty-per-cent lift floor over the answerable probes."""

    return (answerable + 4) // 5


def _terminal_failure_class(failure: RunFailure | None) -> str | None:
    """Name why a run ended, as the class name the runtime already records.

    The run record carries a whole failure — a reason, a class, a message, and
    details — and only the class name may travel here: it says whether the arm
    hit a transport error, a budget, or the run loop, and it cannot carry a
    belief, an answer, or anything else the run saw.
    """

    if failure is None:
        return None
    return failure.error_class or None


class LiveProbeArmResult(BaseModel):
    """One probe asked once, in one arm, against a real model.

    `retried` marks the attempt that replaced a first attempt which terminated
    without an answer, and `failure_class` names why a terminated run did:
    the run record's terminal error class, or the exception type when the arm
    failed outside the run.  Both are content-free — a class name, never a
    message, an answer, or a belief.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: LiveArm
    answer: str | None
    score: AnswerScore
    run_status: RunStatus
    model_calls: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    latency_ms: int = Field(ge=0)
    policy_failures: int = Field(ge=0)
    provider: str | None = None
    model: str | None = None
    policy_version: str | None = None
    retrieval: ProbeRetrievalResult | None = None
    failure_class: str | None = Field(default=None, min_length=1, max_length=256)
    retried: bool = False


class LiveProbeResult(BaseModel):
    """One probe's paired arms and the lift between them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str = Field(min_length=1)
    probe_id: str = Field(min_length=1)
    category: ProbeCategory
    with_memory: LiveProbeArmResult
    without_memory: LiveProbeArmResult
    lift: int


class LiveCategoryMetrics(BaseModel):
    """The per-category slice of the live aggregate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probes: int = Field(ge=0)
    with_memory_correct: int = Field(ge=0)
    without_memory_correct: int = Field(ge=0)
    lift: int


class MemoryBenchmarkLiveMetrics(BaseModel):
    """The whole live arm as counts, beside the rows they were summed from.

    Nothing here is a pass condition: a run that stopped for its cost ceiling
    or missed a floor still reports these numbers as its diagnostics.  The
    thresholds live in :class:`MemoryBenchmarkEvidence`, which is the document
    a passing run publishes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_count: int = Field(ge=0)
    answerable_probe_count: int = Field(ge=0)
    abstain_expected: int = Field(ge=0)
    with_memory_correct: int = Field(ge=0)
    without_memory_correct: int = Field(ge=0)
    lift: int
    recoverable_probe_count: int = Field(ge=0)
    recoverable_correct: int = Field(ge=0)
    abstain_with_memory_correct: int = Field(ge=0)
    protected_leaks_in_answers: int = Field(ge=0)
    with_memory_policy_failures: int = Field(ge=0)
    without_memory_policy_failures: int = Field(ge=0)
    incomplete_runs: int = Field(ge=0)
    retried_runs: int = Field(default=0, ge=0)
    ceiling_hits: int = Field(ge=0)
    total_cost_usd: Decimal = Field(ge=0)
    p50_latency_ms: int = Field(ge=0)
    p95_latency_ms: int = Field(ge=0)
    stopped_by: str | None = None
    failure_classes: dict[str, int] = Field(default_factory=dict)
    per_category: dict[str, LiveCategoryMetrics] = Field(default_factory=dict)
    probes: list[LiveProbeResult] = Field(default_factory=list)


class MemoryBenchmarkEvidence(BaseModel):
    """Evidence that one live benchmark run met every published pass condition.

    Every threshold is re-derived here from the counts carried in the artifact,
    so it cannot be tuned to a run, and every condition is re-checked at parse
    time: an artifact that exists is an artifact that passed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    benchmark_version: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_ref: str = Field(min_length=1)
    model_policy: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    policy_profile: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    formation_policy_version: str = Field(min_length=1)
    retrieval_policy_version: str = Field(min_length=1)
    cost_ceiling_usd: Decimal = Field(gt=0)
    minimum_lift: int = Field(ge=0)
    minimum_recoverable_correct: int = Field(ge=0)
    minimum_abstain_correct: int = Field(ge=0)
    deterministic: DeterministicMetrics
    live: MemoryBenchmarkLiveMetrics
    evaluated_at: datetime

    @model_validator(mode="after")
    def passed_every_condition(self) -> MemoryBenchmarkEvidence:
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("memory benchmark evaluation time must be timezone-aware")
        live = self.live
        self._counts_are_consistent(live)
        if self.cost_ceiling_usd != LIVE_COST_CEILING_USD:
            raise ValueError(
                f"memory benchmark cost ceiling must be {LIVE_COST_CEILING_USD} US dollars"
            )
        if live.total_cost_usd > self.cost_ceiling_usd:
            raise ValueError("memory benchmark live arm spent more than its cost ceiling")
        if live.ceiling_hits:
            raise ValueError("memory benchmark live arm stopped for its cost ceiling")
        if live.stopped_by is not None:
            raise ValueError(f"memory benchmark live arm stopped early: {live.stopped_by}")
        if live.incomplete_runs:
            raise ValueError("memory benchmark live arm left runs incomplete")
        if self.minimum_lift != minimum_live_lift(live.answerable_probe_count):
            raise ValueError("minimum lift must equal twenty per cent of the answerable probes")
        if live.lift < self.minimum_lift:
            raise ValueError(
                f"memory benchmark lift {live.lift} is below the minimum {self.minimum_lift}"
            )
        if self.minimum_recoverable_correct != minimum_supported_case_count(
            live.recoverable_probe_count
        ):
            raise ValueError("minimum recoverable correct must equal eighty per cent coverage")
        if live.recoverable_correct < self.minimum_recoverable_correct:
            raise ValueError(
                f"memory benchmark answered {live.recoverable_correct} of "
                f"{live.recoverable_probe_count} recoverable probes, "
                f"below the minimum {self.minimum_recoverable_correct}"
            )
        if self.minimum_abstain_correct != minimum_supported_case_count(live.abstain_expected):
            raise ValueError("minimum abstain correct must equal eighty per cent coverage")
        if live.abstain_with_memory_correct < self.minimum_abstain_correct:
            raise ValueError(
                f"memory benchmark abstained on {live.abstain_with_memory_correct} of "
                f"{live.abstain_expected} probes, below the minimum "
                f"{self.minimum_abstain_correct}"
            )
        if live.protected_leaks_in_answers:
            raise ValueError("memory benchmark leaked protected content into an answer")
        if live.with_memory_policy_failures > live.without_memory_policy_failures:
            raise ValueError("memory benchmark observed a policy regression with memory")
        self._identity_covers_every_run(live)
        return self

    def _identity_covers_every_run(self, live: MemoryBenchmarkLiveMetrics) -> None:
        """Hold every recorded run to the provider, model, and policy named here.

        A lift measured across two providers is not a lift, so the tuple the
        artifact names has to be the tuple every arm ran under, and an arm that
        resolved no identity is a gap in that claim rather than an exemption
        from it.
        """

        named = (self.provider, self.model, self.policy_version)
        for row in live.probes:
            for arm in (row.with_memory, row.without_memory):
                observed = (arm.provider, arm.model, arm.policy_version)
                where = f"probe row {row.scenario_id}/{row.probe_id} {arm.arm}"
                if any(value is None for value in observed):
                    raise ValueError(
                        f"{where} did not resolve a provider, model, and policy version"
                    )
                if observed != named:
                    raise ValueError(
                        f"{where} ran under a different provider, model, or policy version "
                        "than the artifact names"
                    )

    @staticmethod
    def _counts_are_consistent(live: MemoryBenchmarkLiveMetrics) -> None:
        if live.probe_count < 1:
            raise ValueError("memory benchmark evidence needs at least one probe")
        if live.retried_runs > 2 * live.probe_count:
            raise ValueError("retried runs exceed the runs the two arms could have asked")
        if any(count < 1 for count in live.failure_classes.values()):
            raise ValueError("a failure class counted fewer than one run")
        if live.failure_classes and live.incomplete_runs != sum(live.failure_classes.values()):
            raise ValueError("the failure class histogram must account for every incomplete run")
        if live.abstain_expected > live.probe_count:
            raise ValueError("abstain-expected probes exceed the probe count")
        if live.answerable_probe_count != live.probe_count - live.abstain_expected:
            raise ValueError("answerable probes must be the probes that are not abstain-expected")
        if live.with_memory_correct > live.probe_count:
            raise ValueError("with-memory correct answers exceed the probe count")
        if live.without_memory_correct > live.probe_count:
            raise ValueError("without-memory correct answers exceed the probe count")
        if live.lift != live.with_memory_correct - live.without_memory_correct:
            raise ValueError("lift must be the difference between the two arms")
        if live.recoverable_probe_count > live.answerable_probe_count:
            raise ValueError("recoverable probes exceed the answerable probes")
        if live.recoverable_correct > live.recoverable_probe_count:
            raise ValueError("recoverable correct answers exceed the recoverable probes")
        if live.abstain_with_memory_correct > live.abstain_expected:
            raise ValueError("abstained answers exceed the abstain-expected probes")
        if live.probes and len(live.probes) != live.probe_count:
            raise ValueError("the probe rows must be the probes the counts were summed from")


@dataclass(frozen=True, slots=True)
class LiveRunLedger:
    """What one live invocation asked, what it spent, and what it resolved."""

    rows: list[LiveProbeResult]
    spent: Decimal
    stopped_by: str | None
    identities: set[tuple[str, str, str]]


class LiveScenarioContext:
    """The with-memory composition one scenario's probes share.

    Replaying a scenario's sessions for every probe would multiply the setup
    without changing what any probe reads, so one composition per scenario
    serves all of its probes and each probe still opens its own session.  The
    composition is built on first use and closed when the scenario ends, which
    keeps an injected probe evaluator from composing anything at all.
    """

    def __init__(
        self,
        settings: Settings,
        scenario: BenchmarkScenario,
        *,
        model_policy: str,
        policy_profile: str,
    ) -> None:
        self._settings = settings
        self._scenario = scenario
        self._model_policy = model_policy
        self._policy_profile = policy_profile
        self._stack: AsyncExitStack | None = None
        self._composition: Any = None

    async def composition(self) -> Any:
        """Open the scenario's composition once and hand it to every probe."""

        if self._composition is None:
            self._stack = AsyncExitStack()
            self._composition, _ = await self._stack.enter_async_context(
                scenario_composition(
                    self._settings,
                    self._scenario,
                    policy_profile=self._policy_profile,
                    model_policy=self._model_policy,
                )
            )
        return self._composition

    async def aclose(self) -> None:
        """Close the composition if any probe ever opened it."""

        stack, self._stack, self._composition = self._stack, None, None
        if stack is not None:
            await stack.aclose()


async def _evaluate_probe_live(
    settings: Settings,
    corpus: MemoryBenchmarkCorpus,
    scenario: BenchmarkScenario,
    probe: BenchmarkProbe,
    *,
    arm: LiveArm,
    model_policy: str,
    policy_profile: str,
    scenario_context: LiveScenarioContext,
) -> LiveProbeArmResult:
    """Ask one probe once against a real model and read the run back.

    The with-memory arm asks the scenario's own composition; the without-memory
    arm composes a fresh graph that never heard the conversations, carrying the
    same session metadata so that only memory differs between the two.
    """

    if arm == "with_memory":
        composition = await scenario_context.composition()
        return await _ask(composition, scenario, probe, corpus, arm=arm)
    async with scenario_composition(
        settings,
        scenario,
        policy_profile=policy_profile,
        model_policy=model_policy,
        replay_sessions=False,
    ) as (composition, _):
        return await _ask(composition, scenario, probe, corpus, arm=arm)


async def _attempt_probe_arm(
    settings: Settings,
    corpus: MemoryBenchmarkCorpus,
    scenario: BenchmarkScenario,
    probe: BenchmarkProbe,
    *,
    arm: LiveArm,
    model_policy: str,
    policy_profile: str,
    scenario_context: LiveScenarioContext,
) -> LiveProbeArmResult:
    """Ask one probe arm once and answer with a result even when it threw.

    A failure that lands outside the run — the harness's own wall-clock wait,
    a composition that would not build, a transport error raised past the run
    loop — is as much an incomplete run as one the run loop recorded, and an
    exception that ends the whole invocation makes the arm undiagnosable and
    unretryable.  It becomes a failed arm carrying the exception's type name,
    which is content-free, and the caller retries it like any other.
    """

    try:
        return await _evaluate_probe_live(
            settings,
            corpus,
            scenario,
            probe,
            arm=arm,
            model_policy=model_policy,
            policy_profile=policy_profile,
            scenario_context=scenario_context,
        )
    except Exception as exc:
        return LiveProbeArmResult(
            arm=arm,
            answer=None,
            score=AnswerScore(correct=False, abstained=False, leaked_protected=False),
            run_status=RunStatus.FAILED,
            model_calls=0,
            cost_usd=Decimal("0"),
            latency_ms=0,
            policy_failures=0,
            failure_class=type(exc).__name__,
        )


async def _ask(
    composition: Any,
    scenario: BenchmarkScenario,
    probe: BenchmarkProbe,
    corpus: MemoryBenchmarkCorpus,
    *,
    arm: LiveArm,
) -> LiveProbeArmResult:
    """Submit one probe, wait for it, and score the answer it produced.

    The scenario's own clock is fixed, so it cannot time anything; latency is
    read from the composition root's wall clock, which is the only clock an
    evaluation is allowed to ask for.
    """

    wall = _wall_clock()
    _advance(composition.clock, probe.advance_seconds)
    store_live: list[MemoryRecord] = await composition.memory.list_memories()
    view = await _open_session(composition, probe.project_scope)
    prompt = probe_prompt(probe, corpus, live=True)
    started = wall.now()
    run_id = await asyncio.wait_for(composition.runs.submit(prompt, view.id), PROBE_WALL_SECONDS)
    run = await composition.runs.get(run_id)
    if run.status not in TERMINAL_RUN_STATUSES:
        run = await asyncio.wait_for(composition.runs.wait_terminal(run_id), PROBE_WALL_SECONDS)
    latency_ms = max(int((wall.now() - started).total_seconds() * 1000), 0)
    async with composition.uow_factory() as uow:
        events: list[EventEnvelope] = await uow.events.list_after(view.id, 0, EVALUATION_PRINCIPAL)
    snapshot, in_turn = await _recall_traces(composition, events)
    distinct_prefixes, policy_failures, run_completed = probe_run_facts(events, run.status)
    answer: str | None = run.final_message
    pin = run.provider_pin
    return LiveProbeArmResult(
        arm=arm,
        answer=answer,
        score=score_answer(
            answer,
            probe.answer,
            abstain_phrase=corpus.abstain_phrase,
            protected=scenario.protected_statements,
        ),
        run_status=run.status,
        model_calls=run.usage.model_calls,
        cost_usd=run.usage.cost,
        latency_ms=latency_ms,
        policy_failures=policy_failures,
        provider=None if pin is None else pin.provider,
        model=None if pin is None else pin.model,
        policy_version=composition.ruleset.policy_version,
        failure_class=_terminal_failure_class(run.failure),
        retrieval=(
            None
            if arm != "with_memory"
            else score_probe(
                probe,
                scenario,
                store_live=store_live,
                snapshot=snapshot,
                in_turn=in_turn,
                distinct_prefixes=distinct_prefixes,
                policy_failures=policy_failures,
                run_completed=run_completed,
                snapshot_trace_id=None if snapshot is None else snapshot.id,
                in_turn_trace_ids=[trace.id for trace in in_turn],
            )
        ),
    )


async def run_live_benchmark(
    repository_root: Path,
    *,
    model_policy: str,
    policy_profile: str,
    build_ref: str,
    output: Path,
) -> MemoryBenchmarkResult | None:
    """Run both live arms over the corpus and publish evidence only on a pass.

    The deterministic arm runs first, because its metrics travel inside the
    evidence as the retrieval the answers were built on.  Every live run is
    admitted only when the invocation ceiling can still absorb a whole run at
    the per-run ceiling, so the arm stops before it crosses USD 4.00 rather
    than after; a model priced at zero would make that arithmetic vacuous, so
    the first free run aborts the arm instead.  A failing run returns its
    diagnostics and writes nothing.
    """

    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        return None
    if not model_policy.strip() or not policy_profile.strip() or not build_ref.strip():
        raise ValueError("model policy, policy profile, and build ref must be non-empty")
    resolved = output.resolve()
    if resolved.exists():
        raise ValueError(f"refusing to overwrite existing evaluation evidence: {resolved}")

    corpus, corpus_sha256 = load_corpus(repository_root)
    deterministic = await run_deterministic_benchmark(
        repository_root, policy_profile=policy_profile
    )
    if corpus_sha256 != deterministic.corpus_sha256:
        raise ValueError("the deterministic and live arms read different benchmark corpora")
    base_settings = load_settings()
    with tempfile.TemporaryDirectory(prefix="agent-memory-benchmark-live-") as temporary_root:
        settings = _evaluation_settings(base_settings, Path(temporary_root) / "artifacts")
        ledger = await _ask_every_probe(
            settings,
            corpus,
            model_policy=model_policy,
            policy_profile=policy_profile,
        )
    metrics = _aggregate_live(corpus, ledger)
    failures = _pass_condition_failures(metrics)
    if failures:
        return MemoryBenchmarkResult(
            passed=False,
            failure_summary="; ".join(failures),
            deterministic=deterministic,
            live=metrics,
        )
    identities = set(ledger.identities)
    if len(identities) != 1:
        raise ValueError(
            "the live memory benchmark resolved "
            f"{len(identities)} provider, model, and policy version tuples, not one"
        )
    provider, model, policy_version = identities.pop()
    evidence = MemoryBenchmarkEvidence(
        benchmark_version=deterministic.benchmark_version,
        corpus_sha256=deterministic.corpus_sha256,
        build_ref=build_ref,
        model_policy=model_policy,
        provider=provider,
        model=model,
        policy_profile=policy_profile,
        policy_version=policy_version,
        formation_policy_version=deterministic.formation_policy_version,
        retrieval_policy_version=deterministic.retrieval_policy_version,
        cost_ceiling_usd=LIVE_COST_CEILING_USD,
        minimum_lift=minimum_live_lift(metrics.answerable_probe_count),
        minimum_recoverable_correct=minimum_supported_case_count(metrics.recoverable_probe_count),
        minimum_abstain_correct=minimum_supported_case_count(metrics.abstain_expected),
        deterministic=deterministic.metrics,
        live=metrics,
        evaluated_at=_wall_clock().now(),
    )
    _write_evidence(output, evidence)
    return MemoryBenchmarkResult(
        passed=True,
        failure_summary=None,
        deterministic=deterministic,
        live=metrics,
        evidence=evidence,
    )


async def _ask_every_probe(
    settings: Settings,
    corpus: MemoryBenchmarkCorpus,
    *,
    model_policy: str,
    policy_profile: str,
) -> LiveRunLedger:
    """Ask both arms of every probe, stopping at the first refused admission.

    Only whole probes are recorded: an arm admitted after its partner was
    refused would report a lift the run never measured.  The spend is tracked
    separately from the rows for the same reason in reverse — money a
    discarded arm cost was still spent, and the ceiling answers to what was
    spent rather than to what was scored, so a first attempt that failed after
    a billed call still counts against the ceiling.

    An arm whose run terminates without an answer is asked once more, against a
    composition built the same way but freshly, because the observed failures
    are transport-shaped rather than answers.  The retry is admitted through
    the same pre-admission check as any other run, the second failure is kept,
    and an arm that completed is never asked again.
    """

    rows: list[LiveProbeResult] = []
    identities: set[tuple[str, str, str]] = set()
    spent = Decimal("0")
    stopped_by: str | None = None
    for scenario in corpus.scenarios:
        context = LiveScenarioContext(
            settings,
            scenario,
            model_policy=model_policy,
            policy_profile=policy_profile,
        )
        try:
            for probe in scenario.probes:
                if stopped_by is not None:
                    break
                arms: dict[str, LiveProbeArmResult] = {}
                for arm in _ARMS:
                    if spent + PROBE_RUN_COST_CEILING_USD > LIVE_COST_CEILING_USD:
                        stopped_by = "cost_ceiling"
                        break
                    result = await _attempt_probe_arm(
                        settings,
                        corpus,
                        scenario,
                        probe,
                        arm=arm,
                        model_policy=model_policy,
                        policy_profile=policy_profile,
                        scenario_context=context,
                    )
                    spent += result.cost_usd
                    if result.run_status is not RunStatus.COMPLETED:
                        if spent + PROBE_RUN_COST_CEILING_USD > LIVE_COST_CEILING_USD:
                            stopped_by = "cost_ceiling"
                            break
                        retry_context = LiveScenarioContext(
                            settings,
                            scenario,
                            model_policy=model_policy,
                            policy_profile=policy_profile,
                        )
                        try:
                            result = await _attempt_probe_arm(
                                settings,
                                corpus,
                                scenario,
                                probe,
                                arm=arm,
                                model_policy=model_policy,
                                policy_profile=policy_profile,
                                scenario_context=retry_context,
                            )
                        finally:
                            await retry_context.aclose()
                        spent += result.cost_usd
                        result = result.model_copy(update={"retried": True})
                    if (
                        result.provider is not None
                        and result.model is not None
                        and result.policy_version is not None
                    ):
                        identities.add((result.provider, result.model, result.policy_version))
                    if result.run_status is RunStatus.COMPLETED and result.cost_usd == 0:
                        stopped_by = "zero_cost_model"
                        break
                    arms[arm] = result
                if len(arms) == len(_ARMS):
                    with_memory = arms["with_memory"]
                    without_memory = arms["without_memory"]
                    rows.append(
                        LiveProbeResult(
                            scenario_id=scenario.id,
                            probe_id=probe.id,
                            category=probe.category,
                            with_memory=with_memory,
                            without_memory=without_memory,
                            lift=int(with_memory.score.correct) - int(without_memory.score.correct),
                        )
                    )
        finally:
            await context.aclose()
        if stopped_by is not None:
            break
    return LiveRunLedger(rows=rows, spent=spent, stopped_by=stopped_by, identities=identities)


def _aggregate_live(
    corpus: MemoryBenchmarkCorpus,
    ledger: LiveRunLedger,
) -> MemoryBenchmarkLiveMetrics:
    """Sum the probe rows into the live arm's counts.

    The probe and abstain counts come from the corpus, not from the rows, so a
    run that stopped early is measured against what it was asked to answer,
    and whether a probe was answerable is read from the corpus for the same
    reason.  A correct answer that abstained is a correct answer to a probe
    that had to abstain, because no other kind of gold answer scores correct
    while abstaining; that is the abstention count.
    """

    rows = ledger.rows
    answerable = {
        (scenario.id, probe.id)
        for scenario in corpus.scenarios
        for probe in scenario.probes
        if probe.answer.kind != "abstain"
    }
    probe_count = sum(len(scenario.probes) for scenario in corpus.scenarios)
    arms = [arm for row in rows for arm in (row.with_memory, row.without_memory)]
    latencies = sorted(arm.latency_ms for arm in arms)
    recoverable = [
        row
        for row in rows
        if (row.scenario_id, row.probe_id) in answerable and _recalled_everything_needed(row)
    ]
    return MemoryBenchmarkLiveMetrics(
        probe_count=probe_count,
        answerable_probe_count=len(answerable),
        abstain_expected=probe_count - len(answerable),
        with_memory_correct=sum(row.with_memory.score.correct for row in rows),
        without_memory_correct=sum(row.without_memory.score.correct for row in rows),
        lift=sum(row.lift for row in rows),
        recoverable_probe_count=len(recoverable),
        recoverable_correct=sum(row.with_memory.score.correct for row in recoverable),
        abstain_with_memory_correct=sum(
            row.with_memory.score.correct for row in rows if row.with_memory.score.abstained
        ),
        protected_leaks_in_answers=sum(arm.score.leaked_protected for arm in arms),
        with_memory_policy_failures=sum(row.with_memory.policy_failures for row in rows),
        without_memory_policy_failures=sum(row.without_memory.policy_failures for row in rows),
        incomplete_runs=sum(arm.run_status is not RunStatus.COMPLETED for arm in arms),
        retried_runs=sum(arm.retried for arm in arms),
        ceiling_hits=int(ledger.stopped_by == "cost_ceiling"),
        total_cost_usd=ledger.spent,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        stopped_by=ledger.stopped_by,
        failure_classes=_failure_classes(arms),
        per_category=_per_category(rows),
        probes=list(rows),
    )


def _failure_classes(arms: Sequence[LiveProbeArmResult]) -> dict[str, int]:
    """Count the incomplete runs by the class that ended them.

    An arm that terminated without recording a class is counted under its
    terminal status, so the histogram accounts for every incomplete run rather
    than for the diagnosable subset of them.
    """

    histogram: dict[str, int] = {}
    for arm in arms:
        if arm.run_status is RunStatus.COMPLETED:
            continue
        name = arm.failure_class or arm.run_status.value
        histogram[name] = histogram.get(name, 0) + 1
    return dict(sorted(histogram.items()))


def incomplete_run_diagnostics(metrics: MemoryBenchmarkLiveMetrics) -> list[str]:
    """Describe every incomplete run, content-free, one line each.

    A run that ends without an answer used to be a bare count, which said that
    the arm failed but never why.  These lines name the probe, the arm, the
    terminal status and class, whether any model call was billed, what it cost,
    and whether it was already the retry — everything needed to tell a
    transport blip from a budget from a bug, and nothing the run answered.
    """

    lines: list[str] = []
    for row in metrics.probes:
        for arm in (row.with_memory, row.without_memory):
            if arm.run_status is RunStatus.COMPLETED:
                continue
            lines.append(
                f"incomplete run {row.scenario_id}/{row.probe_id} {arm.arm}: "
                f"status={arm.run_status.value} "
                f"failure_class={arm.failure_class or 'unrecorded'} "
                f"model_calls={arm.model_calls} cost_usd={arm.cost_usd} "
                f"latency_ms={arm.latency_ms} retried={arm.retried}"
            )
    return lines


def _recalled_everything_needed(row: LiveProbeResult) -> bool:
    """Whether the with-memory arm recalled every label the probe needed.

    A probe nothing was needed for is not recoverable: there was nothing to
    recall, so an answer to it says nothing about retrieval.
    """

    retrieval = row.with_memory.retrieval
    if retrieval is None or retrieval.needed_total == 0:
        return False
    return retrieval.needed_recalled == retrieval.needed_total


def _percentile(sorted_latencies: Sequence[int], percentile: int) -> int:
    """The nearest-rank percentile of the latencies a run observed."""

    if not sorted_latencies:
        return 0
    rank = max(1, -(-percentile * len(sorted_latencies) // 100))
    return sorted_latencies[rank - 1]


def _per_category(rows: Sequence[LiveProbeResult]) -> dict[str, LiveCategoryMetrics]:
    """Slice the answered probes by category, in the order they were asked."""

    categories: dict[str, list[LiveProbeResult]] = {}
    for row in rows:
        categories.setdefault(row.category, []).append(row)
    return {
        category: LiveCategoryMetrics(
            probes=len(members),
            with_memory_correct=sum(row.with_memory.score.correct for row in members),
            without_memory_correct=sum(row.without_memory.score.correct for row in members),
            lift=sum(row.lift for row in members),
        )
        for category, members in categories.items()
    }


def _pass_condition_failures(metrics: MemoryBenchmarkLiveMetrics) -> list[str]:
    """Name every published pass condition the run missed, with its numbers."""

    failures: list[str] = []
    if metrics.stopped_by == "zero_cost_model":
        failures.append(
            "model pricing unavailable; ceiling unenforceable "
            "(the first completed live run reported a cost of zero)"
        )
    if metrics.stopped_by == "cost_ceiling":
        failures.append(
            f"stopped_by=cost_ceiling after {len(metrics.probes)} of {metrics.probe_count} "
            f"probes (spent={metrics.total_cost_usd}, per_run_ceiling="
            f"{PROBE_RUN_COST_CEILING_USD}, ceiling={LIVE_COST_CEILING_USD})"
        )
    if metrics.total_cost_usd > LIVE_COST_CEILING_USD:
        failures.append(
            f"cost {metrics.total_cost_usd} exceeds the ceiling {LIVE_COST_CEILING_USD}"
        )
    if metrics.incomplete_runs:
        failures.append(f"incomplete runs {metrics.incomplete_runs}")
    minimum_lift = minimum_live_lift(metrics.answerable_probe_count)
    if metrics.lift < minimum_lift:
        failures.append(
            f"lift {metrics.lift} below the minimum {minimum_lift} "
            f"(with_memory={metrics.with_memory_correct}, "
            f"without_memory={metrics.without_memory_correct}, "
            f"answerable={metrics.answerable_probe_count})"
        )
    minimum_recoverable = minimum_supported_case_count(metrics.recoverable_probe_count)
    if metrics.recoverable_correct < minimum_recoverable:
        failures.append(
            f"recoverable coverage {metrics.recoverable_correct}/"
            f"{metrics.recoverable_probe_count} below the minimum {minimum_recoverable}"
        )
    minimum_abstain = minimum_supported_case_count(metrics.abstain_expected)
    if metrics.abstain_with_memory_correct < minimum_abstain:
        failures.append(
            f"abstention coverage {metrics.abstain_with_memory_correct}/"
            f"{metrics.abstain_expected} below the minimum {minimum_abstain}"
        )
    if metrics.protected_leaks_in_answers:
        failures.append(f"protected leaks in answers {metrics.protected_leaks_in_answers}")
    if metrics.with_memory_policy_failures > metrics.without_memory_policy_failures:
        failures.append(
            "policy regression "
            f"(with_memory={metrics.with_memory_policy_failures}, "
            f"without_memory={metrics.without_memory_policy_failures})"
        )
    return failures


def _wall_clock() -> Clock:
    """Ask the composition root for the wall clock, never the ambient one."""

    # Defer the composition-root import to avoid an evaluation/bootstrap cycle.
    bootstrap = importlib.import_module("agent_core.bootstrap")
    clock: Clock = bootstrap.system_clock()
    return clock
