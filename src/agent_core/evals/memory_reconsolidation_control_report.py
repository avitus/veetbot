"""Reproducible non-activating recordings of the original-only retrieval control."""

import asyncio
import hashlib
import importlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from agent_core.domain.memory import LIVE_MEMORY_STATUSES
from agent_core.evals.memory_reconsolidation import (
    CORPUS_PATH,
    MANIFEST_PATH,
    SCORER_VERSION,
    Observation,
    Score,
    StrictValue,
    load_corpus,
    score_split,
)
from agent_core.evals.memory_reconsolidation_control import (
    CONTROL_AT,
    CONTROL_VERSION,
    PRINCIPAL,
    ControlBudget,
    ControlCaseResult,
    collect_case,
    runtime_case,
)
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION, DeterministicQueryFormer

BASELINE_PATH = Path("evals/capability/memory-reconsolidation-control.v1.json")
RECORDING_MANIFEST_PATH = Path("evals/capability/memory-reconsolidation-control.manifest.json")


class SplitScore(StrictValue):
    repeat: int
    split: Literal["development", "holdout"]
    counts: Score


class ControlRecording(StrictValue):
    schema_version: Literal[1] = 1
    control_version: Literal["reconsolidation-control@1"] = CONTROL_VERSION
    arm: Literal["original_only"] = "original_only"
    purpose: Literal["offline_retrieval_control"] = "offline_retrieval_control"
    answer_evaluation: Literal["not_run"] = "not_run"
    activation_evidence: Literal[False] = False
    provider_calls: Literal[0] = 0
    cost_usd: Literal["0"] = "0"
    recorded_at: datetime
    code_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    working_tree_dirty: bool
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_version: str = SCORER_VERSION
    retrieval_policy_version: str = RETRIEVAL_POLICY_VERSION
    clock_at: datetime = CONTROL_AT
    budget: ControlBudget = ControlBudget()
    rows: tuple[ControlCaseResult, ...]
    scores: tuple[SplitScore, ...]


def implementation_digest(root: Path) -> str:
    """Bind actual source/config/lock bytes, including unpublished local changes."""
    files = sorted(
        path
        for path in (root / "src" / "agent_core").rglob("*")
        if path.is_file() and path.suffix in {".py", ".yaml", ".json"}
    )
    files += [root / "pyproject.toml", root / "uv.lock"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


async def run_control(root: Path) -> tuple[ControlCaseResult, ...]:
    """Run all cases three times; a failed attempt still occupies its census row."""
    corpus = load_corpus(root)
    implementation = implementation_digest(root)
    rows: list[ControlCaseResult] = []
    for repeat in (1, 2, 3):
        for case in corpus.cases:
            try:
                async with asyncio.timeout(30):
                    row = await collect_case(runtime_case(case), repeat=repeat)
            except Exception:
                row = ControlCaseResult(
                    case_id=case.id,
                    repeat=repeat,
                    observation=Observation(case_id=case.id),
                    failure="runtime_failure",
                )
            rows.append(row.model_copy(update={"implementation_sha256": implementation}))
    if implementation_digest(root) != implementation:
        raise ValueError("implementation changed during control execution")
    return tuple(rows)


def _scores(root: Path, rows: tuple[ControlCaseResult, ...]) -> tuple[SplitScore, ...]:
    corpus = load_corpus(root)
    expected = {(repeat, case.id) for repeat in (1, 2, 3) for case in corpus.cases}
    if len(rows) != len(expected) or {(row.repeat, row.case_id) for row in rows} != expected:
        raise ValueError("control requires three complete unique repeats")
    cases = {case.id: case for case in corpus.cases}
    for row in rows:
        if row.failure is not None:
            raise ValueError("control includes failed cases")
        case = cases[row.case_id]
        if row.observation.case_id != row.case_id:
            raise ValueError("observation case mismatch")
        if row.observation.merges or row.observation.hypotheses or row.observation.answers:
            raise ValueError("original-only retrieval control cannot report generated outputs")
        known = {seed.id for seed in case.seeds}
        captured = {entry.seed_id: entry.record for entry in row.originals}
        if len(captured) != len(row.originals) or not set(captured) <= known:
            raise ValueError("unknown or repeated original snapshot")
        seeds = {seed.id: seed for seed in case.seeds}
        for key, record in captured.items():
            seed = seeds[key]
            if (record.id, record.statement, record.subject, record.last_evidence_at) != (
                uuid5(NAMESPACE_URL, f"veetbot:recon-control:{case.id}:{key}"),
                seed.statement,
                seed.subject,
                seed.evidence_at,
            ):
                raise ValueError("captured original disagrees with frozen seed")
        surviving = tuple(
            sorted(key for key, record in captured.items() if record.status in LIVE_MEMORY_STATUSES)
        )
        if row.observation.surviving_ids != surviving:
            raise ValueError("survivor observation disagrees with captured store")
        if len(row.traces) != len(case.probes) or {trace.probe_id for trace in row.traces} != {
            p.id for p in case.probes
        }:
            raise ValueError("control requires complete unique probe traces")
        budget = ControlBudget()
        queries = {
            probe.id: DeterministicQueryFormer(
                PRINCIPAL,
                current_scope=budget.scope,
                budget_tokens=budget.tokens,
                max_items=budget.items,
                min_score=budget.min_score,
            )
            .from_text(probe.question)[0]
            .model_copy(update={"as_of": CONTROL_AT})
            for probe in case.probes
        }
        for trace in row.traces:
            if (
                trace.query.budget_tokens,
                trace.query.max_items,
                trace.query.min_score,
                trace.query.current_scope,
            ) != (
                budget.tokens,
                budget.items,
                budget.min_score,
                budget.scope,
            ) or trace.query.as_of != CONTROL_AT:
                raise ValueError("control budget or clock mismatch")
            if trace.query != queries[trace.probe_id]:
                raise ValueError("control question or query mismatch")
            if trace.rendered_sha256 != hashlib.sha256(trace.rendered.encode()).hexdigest():
                raise ValueError("trace digest mismatch")
            if len(trace.returned_ids) != len(trace.items) or len(set(trace.returned_ids)) != len(
                trace.returned_ids
            ):
                raise ValueError("trace identity mismatch")
            if not {*trace.returned_ids, *trace.dropped_ids, *trace.blocked_ids} <= set(captured):
                raise ValueError("trace references unavailable originals")
            for key, item in zip(trace.returned_ids, trace.items, strict=True):
                original = captured[key]
                if (item.belief_id, item.statement, item.subject) != (
                    original.id,
                    original.statement,
                    original.subject,
                ):
                    raise ValueError("trace original mismatch")
                if key not in surviving or item.merge_id is not None:
                    raise ValueError("original control rendered invalid or merged content")
    splits: tuple[Literal["development", "holdout"], ...] = ("development", "holdout")
    return tuple(
        SplitScore(
            repeat=repeat,
            split=split,
            counts=score_split(
                corpus,
                [
                    row.observation
                    for row in rows
                    if row.repeat == repeat and cases[row.case_id].split == split
                ],
                split,
            ),
        )
        for repeat in (1, 2, 3)
        for split in splits
    )


def write_control(
    root: Path, rows: tuple[ControlCaseResult, ...], output: Path
) -> ControlRecording:
    """Record complete control observations without overwriting previous evidence."""
    scores = _scores(root, rows)
    implementation = implementation_digest(root)
    if any(row.implementation_sha256 != implementation for row in rows):
        raise ValueError("implementation changed before control recording")
    root = root.resolve()
    if not Path(__file__).resolve().is_relative_to(root / "src"):
        raise ValueError("recording must name the executing source tree")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    bootstrap = importlib.import_module("agent_core.bootstrap")
    recording = ControlRecording(
        recorded_at=bootstrap.system_clock().now(),
        code_commit=head,
        working_tree_dirty=dirty,
        implementation_sha256=implementation,
        frozen_manifest_sha256=hashlib.sha256((root / MANIFEST_PATH).read_bytes()).hexdigest(),
        corpus_sha256=hashlib.sha256((root / CORPUS_PATH).read_bytes()).hexdigest(),
        rows=rows,
        scores=scores,
    )
    with output.open("x", encoding="utf-8") as handle:
        handle.write(recording.model_dump_json(indent=2) + "\n")
    return recording


def load_recorded_control(root: Path) -> ControlRecording:
    """Check the frozen recording bytes and recompute their complete observation census."""
    manifest = json.loads((root / RECORDING_MANIFEST_PATH).read_text())
    raw = (root / BASELINE_PATH).read_bytes()
    if manifest != {
        "schema_version": 1,
        "path": str(BASELINE_PATH),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }:
        raise ValueError("recorded control drift")
    result = ControlRecording.model_validate_json(raw)
    if (
        result.scores != _scores(root, result.rows)
        or result.corpus_sha256 != hashlib.sha256((root / CORPUS_PATH).read_bytes()).hexdigest()
        or result.frozen_manifest_sha256
        != hashlib.sha256((root / MANIFEST_PATH).read_bytes()).hexdigest()
        or result.budget != ControlBudget()
        or result.clock_at != CONTROL_AT
        or result.scorer_version != SCORER_VERSION
        or any(row.implementation_sha256 != result.implementation_sha256 for row in result.rows)
    ):
        raise ValueError("recorded control observation mismatch")
    return result
