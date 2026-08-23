"""Adapters that map three public long-horizon datasets into the benchmark.

LongMemEval, LoCoMo, and HaluMem are the outside check on the authored corpus.
All three are read from a local path the operator supplies: **no dataset file
and no derivative of one is ever committed to this repository**, and what leaves
a run is a metrics document naming the dataset, its license, the sample size and
seed, and the digest of the local file that was read — never a passage of it,
never a belief statement, and never the path.  Their non-commercial and
no-derivatives terms are satisfied that way rather than by a promise.

Each loader maps one dataset's own shape into `BenchmarkScenario`s, so the same
deterministic driver that runs the authored corpus runs these too.  A probe an
adapter derives names its dataset instead of corpus labels: it carries the
dataset's evidence turns, its own category verbatim, and the category rules the
authored corpus enforces do not apply to it.  The adapters add one metric the
corpus cannot express — evidence-provenance recall, the fraction of probes for
which recall returned a belief formed from the dataset's own evidence turn.

The results document is informational.  It is not activation evidence, no gate
reads it, and the numbers are not comparable with the published leaderboards:
there is no model judge here, the deterministic extractor is a regex baseline,
subsets are sampled, and LongMemEval's single-session-assistant questions score
zero by construction because this platform never forms beliefs from assistant
turns.  Those caveats travel inside the document.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.config import Settings, load_settings
from agent_core.domain.runs import RunStatus
from agent_core.evals.memory_benchmark import (
    BENCHMARK_VERSION,
    F1_CORRECT_THRESHOLD,
    MAXIMUM_PROBES_PER_SCENARIO,
    MAXIMUM_SESSIONS_PER_SCENARIO,
    BenchmarkProbe,
    BenchmarkScenario,
    BenchmarkSession,
    BenchmarkTurn,
    DeterministicMetrics,
    DeterministicScenarioResult,
    EvidenceRef,
    FormationMetrics,
    LabeledBelief,
    MemoryBenchmarkCorpus,
    ProbeAnswer,
    ProbeCategory,
    SessionEvents,
    aggregate_deterministic,
    load_corpus,
    score_answer_f1,
)
from agent_core.evals.memory_benchmark_driver import (
    PROBE_RUN_COST_CEILING_USD,
    run_deterministic_scenario,
)
from agent_core.evals.memory_benchmark_live import (
    LIVE_COST_CEILING_USD,
    LiveScenarioContext,
)
from agent_core.evals.memory_benchmark_live import (
    _evaluate_probe_live as evaluate_probe_live,
)
from agent_core.evals.memory_formation import _evaluation_settings, _write_evidence
from agent_core.memory.formation import FORMATION_POLICY_VERSION
from agent_core.memory.provider_extraction import PROVIDER_FORMATION_POLICY_VERSION
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION

ExternalDataset = Literal["longmemeval", "locomo", "halumem"]
PrincipalSpeaker = Literal["a", "b"]

EXTERNAL_RESULT_VERSION = "memory-benchmark-external@1"

# Typical file names of the three datasets, mirrored by the .gitignore block.
NEVER_COMMIT_GLOBS: tuple[str, ...] = (
    "longmemeval_*.json",
    "longmemeval_*.jsonl",
    "locomo*.json",
    "HaluMem*",
    "halumem_*",
)


class DatasetLicense(BaseModel):
    """What a dataset is licensed under and where the operator downloads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    license: str = Field(min_length=1)
    source_url: str = Field(min_length=1)


DATASET_LICENSES: Mapping[ExternalDataset, DatasetLicense] = {
    "longmemeval": DatasetLicense(
        license="MIT",
        source_url="https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned",
    ),
    "locomo": DatasetLicense(
        license="CC BY-NC 4.0",
        source_url="https://github.com/snap-research/locomo",
    ),
    "halumem": DatasetLicense(
        license="CC BY-NC-ND 4.0",
        source_url="https://huggingface.co/datasets/IAAR-Shanghai/HaluMem",
    ),
}

LABEL_FREE_CAVEAT = (
    "this dataset names no corpus labels, so needed_total, needed_recalled, and "
    "noise_total in the deterministic block are undefined here and must not be read "
    "as recall or as noise; the recall figure for this dataset is evidence-provenance "
    "recall, evidence_recalled over evidence_total"
)

_SHARED_CAVEATS: tuple[str, ...] = (
    "there is no model judge here; published figures for these datasets use one",
    "the baseline candidate extractor is deterministic and regex-based",
    "answers are scored by normalized token F1, counted correct at 0.5",
    "sessions are replayed and probed by the deterministic driver, so a probe "
    "measures this platform's formation and recall rather than a model's reading",
)

DATASET_CAVEATS: Mapping[ExternalDataset, tuple[str, ...]] = {
    "longmemeval": (
        *_SHARED_CAVEATS,
        LABEL_FREE_CAVEAT,
        "single-session-assistant questions are excluded by design and reported "
        "separately: their answers lie in assistant turns, which never form beliefs",
        "an unrecognized question type is mapped to single_hop",
    ),
    "locomo": (
        *_SHARED_CAVEATS,
        LABEL_FREE_CAVEAT,
        "only one speaker is the formation source; a question is kept only when "
        "every dia_id it cites belongs to that speaker's turns",
        "the category numbers are mapped by the dataset's documented meaning "
        "(1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial) and "
        "the raw number is kept on every probe",
        "questions carry no timestamps, so every probe is asked at the last session's clock",
    ),
    "halumem": (
        *_SHARED_CAVEATS,
        LABEL_FREE_CAVEAT,
        "memory points carry no subject, so every labeled belief is subjected to "
        "the user and matching is looser than the authored corpus's",
        "questions name no evidence turn, so evidence-provenance recall is not "
        "reported for this dataset",
        "a memory point updating another stated in the same session keeps no "
        "supersession, because a supersession is ordered by session",
    ),
}

_LONGMEMEVAL_CATEGORIES: Mapping[str, ProbeCategory] = {
    "single-session-user": "single_hop",
    "single-session-assistant": "single_hop",
    "single-session-preference": "preference",
    "multi-session": "multi_hop",
    "temporal-reasoning": "temporal",
    "knowledge-update": "update",
}
_LOCOMO_CATEGORIES: Mapping[str, ProbeCategory] = {
    "1": "multi_hop",
    "2": "temporal",
    "3": "single_hop",
    "4": "single_hop",
    "5": "abstention",
}
_HALUMEM_BELIEF_TYPES: Mapping[str, str] = {
    "persona": "user_model_attr",
    "relationship": "relationship",
    "event": "fact",
}

_SLUG = re.compile(r"[^a-z0-9]+")
_SESSION_KEY = re.compile(r"^session_(\d+)$")
_SLASH_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})\s*(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})")
_LOCOMO_DATE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*(am|pm)\s+on\s+(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})$",
    re.IGNORECASE,
)


class ExternalLiveMetrics(BaseModel):
    """What the optional live arm answered, scored by token F1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_count: int = Field(ge=0)
    asked: int = Field(ge=0)
    correct: int = Field(ge=0)
    abstain_expected: int = Field(ge=0)
    abstain_correct: int = Field(ge=0)
    mean_f1: Decimal = Field(ge=0, le=1)
    f1_threshold: Decimal = Field(gt=0, le=1)
    incomplete_runs: int = Field(ge=0)
    total_cost_usd: Decimal = Field(ge=0)
    stopped_by: str | None = None


class MemoryBenchmarkExternalResult(BaseModel):
    """One external-dataset run, as metrics that carry no dataset content.

    This document is informational and is not an activation artifact: no gate
    reads it, and nothing in it may be compared with a published leaderboard
    without the caveats it carries.  It names the dataset, its license, the
    digest of the local file that was read, and the sample and seed that drew
    the subset — never the path, never a passage, never a belief.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    result_version: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    dataset: ExternalDataset
    license: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample: int | None = Field(default=None, ge=1)
    seed: int
    principal_speaker: PrincipalSpeaker | None = None
    build_ref: str = Field(min_length=1)
    policy_profile: str = Field(min_length=1)
    formation_policy_version: str = Field(min_length=1)
    provider_formation_policy_version: str = Field(min_length=1)
    retrieval_policy_version: str = Field(min_length=1)
    extractor_name: str = Field(min_length=1)
    deterministic: DeterministicMetrics
    evidence_total: int = Field(ge=0)
    evidence_recalled: int = Field(ge=0)
    excluded_by_design: DeterministicMetrics | None = None
    live: ExternalLiveMetrics | None = None
    caveats: list[str] = Field(min_length=1)
    evaluated_at: datetime


def load_dataset(
    dataset: ExternalDataset,
    path: Path,
    *,
    sample: int | None = None,
    seed: int = 0,
    principal_speaker: PrincipalSpeaker = "a",
) -> list[BenchmarkScenario]:
    """Load one dataset through the adapter that knows its shape."""

    if dataset == "longmemeval":
        return load_longmemeval(path, sample=sample, seed=seed)
    if dataset == "locomo":
        return load_locomo(path, principal_speaker=principal_speaker, sample=sample, seed=seed)
    return load_halumem(path, sample=sample, seed=seed)


def load_longmemeval(
    path: Path, *, sample: int | None = None, seed: int = 0
) -> list[BenchmarkScenario]:
    """Map LongMemEval instances into scenarios, one instance to one scenario.

    A haystack session becomes a benchmark session dated by its haystack date,
    the user turns become principal turns and the assistant turns become
    non-source assistant messages, and the question becomes the single probe,
    dated by `question_date` and carrying references to the sessions the
    instance names as its answer and to the turns it marked `has_answer`.  The
    question type becomes the probe category; an `_abs` suffix is an abstention.
    """

    instances = _read_records(path)
    chosen = _stratified_positions(
        [_text(instance, "question_type", default="") for instance in instances],
        sample=sample,
        seed=seed,
    )
    scenarios = [_longmemeval_scenario(instances[position], position) for position in chosen]
    return [scenario for scenario in scenarios if scenario is not None]


def load_locomo(
    path: Path,
    *,
    principal_speaker: PrincipalSpeaker = "a",
    sample: int | None = None,
    seed: int = 0,
) -> list[BenchmarkScenario]:
    """Map LoCoMo conversations into scenarios, one conversation to one scenario.

    One speaker is the principal and the only formation source; the other
    speaker's turns are replayed as non-source assistant messages.  A question
    is kept only when every `dia_id` it cites resolves to a principal turn, so
    what is scored is what this platform could have learned; the adversarial
    category becomes an abstention probe.
    """

    samples = _read_records(path)
    chosen = _stratified_positions([""] * len(samples), sample=sample, seed=seed)
    scenarios = [
        _locomo_scenario(samples[position], position, principal_speaker, seed)
        for position in chosen
    ]
    return [scenario for scenario in scenarios if scenario is not None]


def load_halumem(
    path: Path, *, sample: int | None = None, seed: int = 0
) -> list[BenchmarkScenario]:
    """Map HaluMem users into scenarios, one user to one scenario.

    Each user's sessions become the conversations, its memory points become the
    labeled beliefs the formation metrics score against — an updating point
    supersedes the point it names, when that point was stated in an earlier
    session — and its questions become the probes.
    """

    users = _read_records(path)
    chosen = _stratified_positions([""] * len(users), sample=sample, seed=seed)
    scenarios = [_halumem_scenario(users[position], position, seed) for position in chosen]
    return [scenario for scenario in scenarios if scenario is not None]


async def run_external_benchmark(
    repository_root: Path,
    *,
    dataset: ExternalDataset,
    path: Path,
    sample: int | None,
    seed: int,
    principal_speaker: PrincipalSpeaker,
    deterministic_only: bool,
    model_policy: str,
    policy_profile: str,
    build_ref: str,
    output: Path,
) -> MemoryBenchmarkExternalResult | None:
    """Run one external dataset and publish its informational metrics.

    The deterministic arm runs every scenario the adapter produced and the
    optional live arm, which asks each probe once against a real model, runs
    only under `RUN_LIVE_MODEL_TESTS=1` and stops before it crosses the same
    invocation ceiling the authored corpus's live arm answers to.  The output
    path must not already exist: a results file is never overwritten.
    """

    if not deterministic_only and os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        return None
    resolved = output.resolve()
    if resolved.exists():
        raise ValueError(f"refusing to overwrite an existing results file: {resolved}")

    scenarios = load_dataset(
        dataset, path, sample=sample, seed=seed, principal_speaker=principal_speaker
    )
    if not scenarios:
        raise ValueError(f"the {dataset} file produced no scenario the benchmark can run")
    digest = _file_sha256(path)
    # The authored corpus supplies the probe instruction and the abstain phrase,
    # which belong to this harness rather than to any dataset.
    corpus, _ = load_corpus(repository_root)

    with tempfile.TemporaryDirectory(prefix="agent-memory-benchmark-external-") as scratch:
        settings = _evaluation_settings(load_settings(), Path(scratch) / "artifacts")
        results: list[DeterministicScenarioResult] = []
        for scenario in scenarios:
            session_events: dict[str, SessionEvents] = {}
            results.append(
                await run_deterministic_scenario(
                    settings,
                    scenario,
                    corpus=corpus,
                    policy_profile=policy_profile,
                    session_events=session_events,
                )
            )
        live = (
            None
            if deterministic_only
            else await _run_live_arm(
                settings,
                corpus,
                scenarios,
                model_policy=model_policy,
                policy_profile=policy_profile,
            )
        )

    included, excluded = _split_by_design(scenarios, results)
    result = MemoryBenchmarkExternalResult(
        result_version=EXTERNAL_RESULT_VERSION,
        benchmark_version=BENCHMARK_VERSION,
        dataset=dataset,
        license=DATASET_LICENSES[dataset].license,
        source_url=DATASET_LICENSES[dataset].source_url,
        source_sha256=digest,
        sample=sample,
        seed=seed,
        principal_speaker=principal_speaker if dataset == "locomo" else None,
        build_ref=build_ref,
        policy_profile=policy_profile,
        formation_policy_version=FORMATION_POLICY_VERSION,
        provider_formation_policy_version=PROVIDER_FORMATION_POLICY_VERSION,
        retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
        extractor_name=results[0].extractor_name,
        deterministic=aggregate_deterministic(included),
        evidence_total=sum(
            probe.evidence_total for scenario in included for probe in scenario.probes
        ),
        evidence_recalled=sum(
            probe.evidence_recalled for scenario in included for probe in scenario.probes
        ),
        excluded_by_design=aggregate_deterministic(excluded) if excluded else None,
        live=live,
        caveats=list(DATASET_CAVEATS[dataset]),
        evaluated_at=_now(),
    )
    _write_evidence(output, result)
    return result


def _split_by_design(
    scenarios: Sequence[BenchmarkScenario], results: Sequence[DeterministicScenarioResult]
) -> tuple[list[DeterministicScenarioResult], list[DeterministicScenarioResult]]:
    """Separate the probes a dataset excludes by design from the rest.

    A LongMemEval single-session-assistant question expects its answer to sit
    in an assistant turn, and this platform never forms a belief from one, so
    folding those probes into the headline counts would report a design
    decision as a retrieval failure.

    The headline keeps every scenario's formation, which is a property of the
    conversation rather than of a probe; the excluded aggregate carries the
    excluded probe rows with the formation zeroed, so the two documents sum to
    the run rather than counting the same beliefs twice.
    """

    excluded_ids = {
        (scenario.id, probe.id)
        for scenario in scenarios
        for probe in scenario.probes
        if probe.excluded_by_design
    }
    included: list[DeterministicScenarioResult] = []
    excluded: list[DeterministicScenarioResult] = []
    empty = FormationMetrics(
        expected=0, supported=0, formed=0, fabricated=0, stale_live=0, policy_failures=0
    )
    for result in results:
        keep = [
            probe
            for probe in result.probes
            if (result.scenario_id, probe.probe_id) not in excluded_ids
        ]
        drop = [
            probe for probe in result.probes if (result.scenario_id, probe.probe_id) in excluded_ids
        ]
        included.append(result.model_copy(update={"probes": keep}))
        if drop:
            excluded.append(
                result.model_copy(update={"probes": drop, "formation": empty, "beliefs": []})
            )
    return included, excluded


async def _run_live_arm(
    settings: Settings,
    corpus: MemoryBenchmarkCorpus,
    scenarios: Sequence[BenchmarkScenario],
    *,
    model_policy: str,
    policy_profile: str,
) -> ExternalLiveMetrics:
    """Ask every probe once against a real model and score it by token F1.

    Only the with-memory arm runs: the datasets measure what memory answers,
    and the lift the authored corpus measures against a memoryless arm is not
    a number these publish.  Admission is checked before each run against the
    same invocation ceiling, so the arm stops rather than crossing it.
    """

    probes = [(scenario, probe) for scenario in scenarios for probe in scenario.probes]
    abstain_expected = sum(probe.answer.kind == "abstain" for _scenario, probe in probes)
    spent = Decimal("0")
    stopped_by: str | None = None
    scores: list[Decimal] = []
    correct = 0
    abstain_correct = 0
    incomplete = 0
    for scenario, probe in probes:
        if spent + PROBE_RUN_COST_CEILING_USD > LIVE_COST_CEILING_USD:
            stopped_by = "cost_ceiling"
            break
        context = LiveScenarioContext(
            settings, scenario, model_policy=model_policy, policy_profile=policy_profile
        )
        try:
            arm = await evaluate_probe_live(
                settings,
                corpus,
                scenario,
                probe,
                arm="with_memory",
                model_policy=model_policy,
                policy_profile=policy_profile,
                scenario_context=context,
            )
        finally:
            await context.aclose()
        spent += arm.cost_usd
        incomplete += int(arm.run_status is not RunStatus.COMPLETED)
        if arm.run_status is RunStatus.COMPLETED and arm.cost_usd == 0:
            stopped_by = "zero_cost_model"
            break
        if probe.answer.kind == "abstain":
            abstain_correct += int(arm.score.abstained)
            correct += int(arm.score.abstained)
            scores.append(Decimal(1) if arm.score.abstained else Decimal(0))
            continue
        f1 = score_answer_f1(arm.answer, probe.answer)
        scores.append(f1)
        correct += int(f1 >= F1_CORRECT_THRESHOLD)
    return ExternalLiveMetrics(
        probe_count=len(probes),
        asked=len(scores),
        correct=correct,
        abstain_expected=abstain_expected,
        abstain_correct=abstain_correct,
        mean_f1=(sum(scores, Decimal(0)) / len(scores)).quantize(Decimal("0.0001"))
        if scores
        else Decimal("0.0000"),
        f1_threshold=F1_CORRECT_THRESHOLD,
        incomplete_runs=incomplete,
        total_cost_usd=spent,
        stopped_by=stopped_by,
    )


def _now() -> datetime:
    """Ask the composition root for the wall clock, never the ambient one."""

    # Defer the composition-root import to avoid an evaluation/bootstrap cycle.
    bootstrap = importlib.import_module("agent_core.bootstrap")
    stamped: datetime = bootstrap.system_clock().now()
    return stamped


# --------------------------------------------------------------------------- #
# LongMemEval
# --------------------------------------------------------------------------- #


def _longmemeval_scenario(instance: Mapping[str, Any], position: int) -> BenchmarkScenario | None:
    question_id = _text(instance, "question_id", default=f"instance-{position + 1}")
    question_type = _text(instance, "question_type", default="")
    where = f"longmemeval instance {question_id}"
    raw_sessions = _sequence(instance, "haystack_sessions", where)
    raw_dates = _sequence(instance, "haystack_dates", where)
    if len(raw_dates) != len(raw_sessions):
        raise ValueError(f"{where} carries {len(raw_dates)} dates for {len(raw_sessions)} sessions")
    dataset_ids = [str(value) for value in instance.get("haystack_session_ids") or []]

    sessions: list[BenchmarkSession] = []
    dates: list[datetime] = []
    identifiers: dict[str, str] = {}
    turn_flags: dict[str, list[bool]] = {}
    for index, raw in enumerate(raw_sessions):
        turns, flags = _longmemeval_turns(raw, where)
        if not turns:
            continue
        session_id = _session_id(len(sessions) + 1, where)
        when = _parse_slash_date(str(raw_dates[index]), where)
        advance = 0 if not dates else _elapsed(dates[-1], when)
        sessions.append(BenchmarkSession(id=session_id, advance_seconds=advance, turns=turns))
        dates.append(when)
        turn_flags[session_id] = flags
        if index < len(dataset_ids):
            identifiers[dataset_ids[index]] = session_id

    if not dates:
        raise ValueError(f"{where} has no non-empty session")

    answer_sessions = [str(value) for value in instance.get("answer_session_ids") or []]
    evidence = _longmemeval_evidence(answer_sessions, identifiers, turn_flags)
    category, abstains = _longmemeval_category(question_type)
    asked = _read_slash_date(_text(instance, "question_date", default=""))
    answer = _alternatives(instance.get("answer"), abstains=abstains)
    if answer is None:
        return None
    probe = BenchmarkProbe(
        id="p01",
        category=category,
        question=_required_text(instance, "question", where),
        advance_seconds=0 if asked is None or not dates else _elapsed(dates[-1], asked),
        answer=answer,
        evidence=[] if abstains else evidence,
        source_dataset="longmemeval",
        source_category=question_type or None,
        excluded_by_design=question_type.startswith("single-session-assistant"),
    )
    return BenchmarkScenario(
        id=_scenario_id("longmemeval", question_id, position),
        title=f"LongMemEval {question_type or 'instance'} {question_id}",
        start_at=dates[0],
        sessions=sessions,
        beliefs=[],
        probes=[probe],
    )


def _longmemeval_turns(raw: Any, where: str) -> tuple[list[BenchmarkTurn], list[bool]]:
    """Read one haystack session's turns and which of them hold the answer."""

    if not isinstance(raw, list):
        raise ValueError(f"{where} carries a haystack session that is not a list of turns")
    turns: list[BenchmarkTurn] = []
    flags: list[bool] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{where} carries a haystack turn that is not an object")
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        turns.append(BenchmarkTurn(text=text, role=_role(item.get("role"))))
        flags.append(bool(item.get("has_answer")))
    return turns, flags


def _longmemeval_evidence(
    answer_sessions: Sequence[str],
    identifiers: Mapping[str, str],
    turn_flags: Mapping[str, list[bool]],
) -> list[EvidenceRef]:
    """Point at the turns the instance marked, or at the sessions it named."""

    named = [identifiers[value] for value in answer_sessions if value in identifiers]
    if not named:
        named = [session for session, flags in turn_flags.items() if any(flags)]
    evidence: list[EvidenceRef] = []
    for session_id in named:
        flags = turn_flags.get(session_id, [])
        marked = [index for index, flag in enumerate(flags) if flag]
        if marked:
            evidence.extend(
                EvidenceRef(session_id=session_id, turn_index=index) for index in marked
            )
        else:
            evidence.append(EvidenceRef(session_id=session_id, turn_index=None))
    return evidence


def _longmemeval_category(question_type: str) -> tuple[ProbeCategory, bool]:
    """Map one question type to a probe category; `_abs` is an abstention."""

    if question_type.endswith("_abs"):
        return "abstention", True
    return _LONGMEMEVAL_CATEGORIES.get(question_type, "single_hop"), False


# --------------------------------------------------------------------------- #
# LoCoMo
# --------------------------------------------------------------------------- #


def _locomo_scenario(
    sample: Mapping[str, Any],
    position: int,
    principal_speaker: PrincipalSpeaker,
    seed: int,
) -> BenchmarkScenario | None:
    sample_id = _text(sample, "sample_id", default=f"conversation-{position + 1}")
    where = f"locomo conversation {sample_id}"
    conversation = sample.get("conversation")
    if not isinstance(conversation, dict):
        raise ValueError(f"{where} carries no conversation object")
    principal = str(conversation.get(f"speaker_{principal_speaker}") or "").strip()
    if not principal:
        raise ValueError(f"{where} names no speaker_{principal_speaker}")

    sessions: list[BenchmarkSession] = []
    dates: list[datetime] = []
    located: dict[str, tuple[str, int, bool]] = {}
    for number in sorted(
        int(match.group(1))
        for key in conversation
        if (match := _SESSION_KEY.match(str(key))) is not None
    ):
        raw = conversation.get(f"session_{number}")
        if not isinstance(raw, list):
            raise ValueError(f"{where} carries a session_{number} that is not a list of turns")
        turns: list[BenchmarkTurn] = []
        identifiers: list[tuple[str, bool]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError(f"{where} session_{number} carries a turn that is not an object")
            text = str(item.get("text") or item.get("blip_caption") or "").strip()
            if not text:
                continue
            speaker = str(item.get("speaker") or "").strip()
            is_principal = speaker == principal
            turns.append(BenchmarkTurn(text=text, role="user" if is_principal else "assistant"))
            identifiers.append((str(item.get("dia_id") or ""), is_principal))
        if not turns:
            continue
        when = _parse_locomo_date(
            str(conversation.get(f"session_{number}_date_time") or ""), where, number
        )
        session_id = _session_id(len(sessions) + 1, where)
        sessions.append(
            BenchmarkSession(
                id=session_id,
                advance_seconds=0 if not dates else _elapsed(dates[-1], when),
                turns=turns,
            )
        )
        dates.append(when)
        for index, (dia_id, is_principal) in enumerate(identifiers):
            if dia_id:
                located[dia_id] = (session_id, index, is_principal)

    if not dates:
        raise ValueError(f"{where} has no non-empty session")

    eligible = [
        entry
        for entry in _sequence(sample, "qa", where)
        if isinstance(entry, dict) and _locomo_is_scorable(entry, located)
    ]
    valid_probes = [
        probe for entry in eligible if (probe := _locomo_probe(entry, located, 1)) is not None
    ]
    probes = [
        probe.model_copy(update={"id": f"p{index:02d}"})
        for index, probe in enumerate(
            _capped(valid_probes, seed=seed, salt=f"locomo:{sample_id}"), start=1
        )
    ]
    if not probes:
        if eligible:
            return None
        raise ValueError(f"{where} has no question whose evidence is the principal speaker's")
    return BenchmarkScenario(
        id=_scenario_id("locomo", sample_id, position),
        title=f"LoCoMo conversation {sample_id} as speaker {principal_speaker}",
        start_at=dates[0],
        sessions=sessions,
        beliefs=[],
        probes=probes,
    )


def _locomo_is_scorable(
    entry: Mapping[str, Any], located: Mapping[str, tuple[str, int, bool]]
) -> bool:
    """Keep a question only when the principal speaker's turns hold its evidence."""

    category = str(entry.get("category", ""))
    references = [str(value) for value in entry.get("evidence") or [] if isinstance(value, str)]
    if _LOCOMO_CATEGORIES.get(category) == "abstention":
        return True
    if not references or len(references) != len(entry.get("evidence") or []):
        return False
    return all(reference in located and located[reference][2] for reference in references)


def _locomo_probe(
    entry: Mapping[str, Any], located: Mapping[str, tuple[str, int, bool]], index: int
) -> BenchmarkProbe | None:
    category = str(entry.get("category", ""))
    mapped = _LOCOMO_CATEGORIES.get(category, "single_hop")
    abstains = mapped == "abstention"
    evidence = [
        EvidenceRef(session_id=located[str(value)][0], turn_index=located[str(value)][1])
        for value in entry.get("evidence") or []
        if isinstance(value, str) and str(value) in located
    ]
    answer = _alternatives(entry.get("answer"), abstains=abstains)
    if answer is None:
        return None
    return BenchmarkProbe(
        id=f"p{index:02d}",
        category=mapped,
        question=str(entry.get("question") or "").strip() or "(question missing)",
        answer=answer,
        evidence=[] if abstains else evidence,
        source_dataset="locomo",
        source_category=category or None,
    )


# --------------------------------------------------------------------------- #
# HaluMem
# --------------------------------------------------------------------------- #


def _halumem_scenario(
    user: Mapping[str, Any], position: int, seed: int
) -> BenchmarkScenario | None:
    user_id = _text(user, "uuid", default=_text(user, "user_id", default=f"user-{position + 1}"))
    where = f"halumem user {user_id}"
    sessions: list[BenchmarkSession] = []
    dates: list[datetime] = []
    order: dict[str, int] = {}
    points: list[tuple[str, Mapping[str, Any]]] = []
    for raw in _sequence(user, "sessions", where):
        if not isinstance(raw, dict):
            raise ValueError(f"{where} carries a session that is not an object")
        turns = _halumem_turns(raw, where)
        if not turns:
            continue
        when = _parse_iso_date(str(raw.get("timestamp") or raw.get("date") or ""), where)
        when = when if when is not None else _spaced(dates)
        session_id = _session_id(len(sessions) + 1, where)
        sessions.append(
            BenchmarkSession(
                id=session_id,
                advance_seconds=0 if not dates else _elapsed(dates[-1], when),
                turns=turns,
            )
        )
        dates.append(when)
        order[str(raw.get("session_id") or raw.get("id") or session_id)] = len(sessions) - 1
        order.setdefault(session_id, len(sessions) - 1)
        points.extend(
            (session_id, point)
            for point in raw.get("memory_points") or []
            if isinstance(point, dict)
        )

    if not dates:
        raise ValueError(f"{where} has no non-empty session")

    for point in user.get("memory_points") or []:
        if not isinstance(point, dict):
            continue
        named = str(point.get("session_id") or "")
        if named not in order:
            raise ValueError(f"{where} carries a memory point naming unknown session {named!r}")
        points.append((sessions[order[named]].id, point))

    beliefs = _halumem_beliefs(points, sessions)
    questions = [entry for entry in user.get("questions") or [] if isinstance(entry, dict)]
    valid_probes = [probe for entry in questions if (probe := _halumem_probe(entry, 1)) is not None]
    probes = [
        probe.model_copy(update={"id": f"p{index:02d}"})
        for index, probe in enumerate(
            _capped(valid_probes, seed=seed, salt=f"halumem:{user_id}"), start=1
        )
    ]
    if not probes:
        if questions:
            return None
        raise ValueError(f"{where} carries no question the benchmark can ask")
    return BenchmarkScenario(
        id=_scenario_id("halumem", user_id, position),
        title=f"HaluMem user {user_id}",
        start_at=dates[0],
        sessions=sessions,
        beliefs=beliefs,
        probes=probes,
    )


def _halumem_turns(raw: Mapping[str, Any], where: str) -> list[BenchmarkTurn]:
    """Read one HaluMem session's dialogue into principal and assistant turns."""

    dialogue = raw.get("dialogue")
    if dialogue is None:
        dialogue = raw.get("messages") or raw.get("turns") or []
    if not isinstance(dialogue, list):
        raise ValueError(f"{where} carries a dialogue that is not a list of utterances")
    turns: list[BenchmarkTurn] = []
    for item in dialogue:
        if not isinstance(item, dict):
            raise ValueError(f"{where} carries an utterance that is not an object")
        text = str(item.get("content") or item.get("text") or item.get("message") or "").strip()
        if not text:
            continue
        turns.append(BenchmarkTurn(text=text, role=_role(item.get("role") or item.get("speaker"))))
    return turns


def _halumem_beliefs(
    points: Sequence[tuple[str, Mapping[str, Any]]],
    sessions: Sequence[BenchmarkSession],
) -> list[LabeledBelief]:
    """Turn memory points into labeled beliefs, and updates into supersessions.

    A supersession is ordered by session, so a point updating one stated in the
    same session keeps no supersession link: the scenario would be rejected,
    and inventing an ordering the data does not have would be worse than
    reporting two beliefs.
    """

    positions = {session.id: index for index, session in enumerate(sessions)}
    labels: dict[str, str] = {}
    stated_in: dict[str, int] = {}
    beliefs: list[LabeledBelief] = []
    for index, (session_id, point) in enumerate(points, start=1):
        statement = str(point.get("memory_content") or "").strip()
        if not statement:
            continue
        label = f"m{index:03d}"
        supersedes: str | None = None
        if point.get("is_update"):
            supersedes = next(
                (
                    candidate
                    for original in point.get("original_memories") or []
                    if (candidate := labels.get(_memory_id_of(original))) is not None
                    and stated_in[candidate] < positions[session_id]
                ),
                None,
            )
        beliefs.append(
            LabeledBelief(
                label=label,
                session=session_id,
                supersedes=supersedes,
                belief_type=_HALUMEM_BELIEF_TYPES.get(
                    str(point.get("memory_type") or "").strip().casefold(), "fact"
                ),
                subjects=["user"],
                statements=[statement],
            )
        )
        identifier = _memory_id(point)
        if identifier:
            labels[identifier] = label
        stated_in[label] = positions[session_id]
    return beliefs


def _halumem_probe(entry: Mapping[str, Any], index: int) -> BenchmarkProbe | None:
    question_type = str(
        entry.get("question_type") or entry.get("type") or entry.get("category") or ""
    ).strip()
    category, abstains = (
        _longmemeval_category(question_type)
        if question_type
        else (
            "single_hop",
            False,
        )
    )
    answer = _alternatives(entry.get("answer"), abstains=abstains)
    if answer is None:
        return None
    return BenchmarkProbe(
        id=f"p{index:02d}",
        category=category,
        question=str(entry.get("question") or "").strip() or "(question missing)",
        answer=answer,
        source_dataset="halumem",
        source_category=question_type or None,
    )


def _memory_id(point: Mapping[str, Any]) -> str:
    return str(point.get("memory_id") or point.get("id") or "").strip()


def _memory_id_of(original: Any) -> str:
    if isinstance(original, dict):
        return _memory_id(original)
    return str(original).strip()


# --------------------------------------------------------------------------- #
# Shared reading, sampling, and shaping
# --------------------------------------------------------------------------- #


def _read_records(path: Path) -> list[dict[str, Any]]:
    """Read a dataset file as records, from JSON or from JSON lines."""

    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        loaded: Any = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        loaded = json.loads(text)
    records = loaded if isinstance(loaded, list) else [loaded]
    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"{path.name} is not a list of dataset records")
    return list(records)


def _file_sha256(path: Path) -> str:
    """Digest the local file a run read, which is what travels in the result."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stratified_positions(strata: Sequence[str], *, sample: int | None, seed: int) -> list[int]:
    """Draw `sample` positions, spread across the strata, for one seed.

    The strata are drawn round-robin so that a small sample still covers the
    question types rather than whatever the file happens to list first, and the
    result is sorted back into file order so that a scenario keeps the identity
    its position gives it whether or not the run sampled.
    """

    if sample is None or sample >= len(strata):
        return list(range(len(strata)))
    if sample < 1:
        raise ValueError("a sample draws at least one instance")
    groups: dict[str, list[int]] = {}
    for index, stratum in enumerate(strata):
        groups.setdefault(stratum, []).append(index)
    order = _seeded_order(sorted(groups), seed=seed, salt="strata")
    remaining = {key: _seeded_order(groups[key], seed=seed, salt=f"stratum:{key}") for key in order}
    chosen: list[int] = []
    while len(chosen) < sample:
        for key in order:
            if len(chosen) == sample:
                break
            if remaining[key]:
                chosen.append(remaining[key].pop())
    return sorted(chosen)


def _capped(entries: Sequence[Any], *, seed: int, salt: str) -> list[Any]:
    """Keep at most the probes a scenario may carry, drawn deterministically."""

    if len(entries) <= MAXIMUM_PROBES_PER_SCENARIO:
        return list(entries)
    positions = sorted(
        _seeded_order(range(len(entries)), seed=seed, salt=salt)[:MAXIMUM_PROBES_PER_SCENARIO]
    )
    return [entries[position] for position in positions]


def _seeded_order[T](values: Iterable[T], *, seed: int, salt: str) -> list[T]:
    """Order values by a keyed digest: one seed always gives one order.

    The evaluation package may not reach for ambient nondeterminism, and a
    seeded `random.Random` would also tie a published sample to one release's
    generator; a digest of the seed, a salt, and the value is stable across
    releases and reproduces a run from the seed the results file carries.
    """

    return sorted(
        values,
        key=lambda value: hashlib.blake2b(
            f"{seed}:{salt}:{value}".encode(), digest_size=16
        ).hexdigest(),
    )


def _session_id(number: int, where: str) -> str:
    """Name one replayed session, or refuse an instance too long to name.

    A session identifier carries two digits, so the hundredth session of an
    instance cannot be named at all.  Refusing here, where the instance is
    known, is what makes the refusal legible: the alternative is the schema
    rejecting `s100` with no dataset, no instance, and no remedy.
    """

    if number > MAXIMUM_SESSIONS_PER_SCENARIO:
        raise ValueError(
            f"{where} carries more than the {MAXIMUM_SESSIONS_PER_SCENARIO} sessions "
            "a scenario can name; run a smaller variant of the dataset"
        )
    return f"s{number:02d}"


def _scenario_id(dataset: str, identifier: str, position: int) -> str:
    """Name a scenario after its dataset, the instance, and its file position."""

    if position >= 999:
        raise ValueError(
            f"{dataset} carries more than 999 instances, more than a scenario "
            "identifier can number; sample a subset"
        )
    slug = _SLUG.sub("-", identifier.casefold()).strip("-")
    stem = f"{dataset}-{slug}" if slug else dataset
    return f"mb-{stem}-{position + 1:03d}"


def _alternatives(answer: Any, *, abstains: bool) -> ProbeAnswer | None:
    """Gold answers are alternatives; an abstention carries no value."""

    if abstains:
        return ProbeAnswer(kind="abstain", values=[])
    values = answer if isinstance(answer, list) else [answer]
    stated = [str(value).strip() for value in values if str(value or "").strip()]
    if not stated:
        return None
    return ProbeAnswer(kind="alternatives", values=stated)


def _role(value: Any) -> Literal["user", "assistant"]:
    """Read a dataset's speaker label as a benchmark turn role."""

    return "user" if str(value or "user").strip().casefold() in {"user", "human"} else "assistant"


def _elapsed(earlier: datetime, later: datetime) -> int:
    """Whole seconds between two dataset timestamps, never negative."""

    return max(int((later - earlier).total_seconds()), 0)


def _spaced(dates: Sequence[datetime]) -> datetime:
    """Place an undated session one day after the last dated one."""

    if not dates:
        return datetime(2024, 1, 1, tzinfo=UTC)
    return dates[-1] + timedelta(days=1)


def _parse_slash_date(value: str, where: str) -> datetime:
    """Read LongMemEval's `2023/05/20 (Sat) 02:21` timestamps."""

    stamp = _read_slash_date(value)
    if stamp is None:
        raise ValueError(f"{where} carries an unreadable timestamp {value!r}")
    return stamp


def _read_slash_date(value: str) -> datetime | None:
    """Read a LongMemEval timestamp, or report that there is none to read."""

    match = _SLASH_DATE.match(value.strip())
    if match is None:
        return None
    year, month, day, hour, minute = (int(group) for group in match.groups())
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _parse_locomo_date(value: str, where: str, number: int) -> datetime:
    """Read LoCoMo's `1:56 pm on 8 May, 2023` timestamps."""

    match = _LOCOMO_DATE.match(value.strip())
    if match is None:
        raise ValueError(f"{where} carries an unreadable session_{number} timestamp {value!r}")
    hour, minute, meridiem, day, month, year = match.groups()
    stamp = datetime.strptime(
        f"{day} {month} {year} {hour}:{minute} {meridiem.upper()}", "%d %B %Y %I:%M %p"
    )
    return stamp.replace(tzinfo=UTC)


def _parse_iso_date(value: str, where: str) -> datetime | None:
    """Read HaluMem's ISO 8601 timestamps, which a session may omit."""

    if not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{where} carries an unreadable timestamp {value!r}") from exc
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _sequence(record: Mapping[str, Any], key: str, where: str) -> list[Any]:
    value = record.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{where} carries no {key} list")
    return value


def _text(record: Mapping[str, Any], key: str, *, default: str) -> str:
    value = record.get(key)
    return default if value is None else str(value).strip() or default


def _required_text(record: Mapping[str, Any], key: str, where: str) -> str:
    value = str(record.get(key) or "").strip()
    if not value:
        raise ValueError(f"{where} carries no {key}")
    return value
