"""Replay owner recall probes through the composed production run loop.

Each probe path is one real run: a session, an owner message submitted
through `RunService.submit`, context assembly, recall, the model request and
the completion hooks, observed exactly as production performs them. The
driver reads what the run left behind (the retriever's transient stage
identifiers, the trace, the runtime's delivery record, the final message)
and names where each expected belief stopped. Nothing here seeds a store or
tunes a policy; the frozen evaluation controls are untouched.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

from agent_core.bootstrap import Composition
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import (
    MemoryRecord,
    MemoryStatus,
    RecallResult,
    RecallTrace,
    Sensitivity,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import Run, RunStatus
from agent_core.evals.general_memory_recall_driver import implementation_hashes
from agent_core.evals.owner_recall_probes import (
    EvidenceObservation,
    PathKind,
    PathObservation,
    Probe,
    ProbeObservation,
    ProbeReport,
    SaveObservation,
    evidence_stage,
    stage_rank,
    summarize,
)

_SHORT_ID = re.compile(r"\[m:([0-9a-f]{8})\]")
SCRIPTED_ANSWER = "Answered from the delivered evidence."
Finish = Callable[[Composition, UUID], Awaitable[Run]]


async def _wait(app: Composition, run_id: UUID) -> Run:
    return await app.runs.wait_terminal(run_id)


def _rewind(app: Composition, script: FakeModelScript | None, turns: list[ScriptedTurn]) -> None:
    """Give the scripted provider this run's turns from the top."""

    if script is None:
        return
    script.turns[:] = turns
    provider = getattr(app.executor, "model_provider", None)
    rewind = getattr(provider, "rewind", None)
    if callable(rewind):
        rewind()


async def observe_probes(
    app: Composition,
    script: FakeModelScript | None,
    probes: Sequence[Probe],
    *,
    finish: Finish = _wait,
    paths: Sequence[PathKind] | None = None,
    probe_digest: str | None = None,
    now: datetime | None = None,
) -> ProbeReport:
    """Run every probe path and write probe, return the content-free report.

    With a scripted model the answer is fixed, so Used and answer correctness
    are not judged; with the live model (`script is None`) they are.
    """

    captured: list[RecallResult] = []
    retriever = app.memory_retriever
    original = retriever.recall

    async def recording(*args: Any, **kwargs: Any) -> RecallResult:
        result = await original(*args, **kwargs)
        captured.append(result)
        return result

    retriever.recall = recording  # type: ignore[method-assign]
    observations: list[ProbeObservation] = []
    try:
        for probe in probes:
            path_observations = []
            for path in probe.paths:
                if paths is not None and path not in paths:
                    continue
                path_observations.append(
                    await _observe_path(app, script, probe, path, captured, finish)
                )
            save = None
            if probe.write is not None:
                save = await _observe_save(app, script, probe, finish)
            observations.append(
                ProbeObservation(
                    probe_id=probe.id,
                    incident=probe.incident,
                    family=probe.family,
                    paths=tuple(path_observations),
                    save=save,
                )
            )
    finally:
        retriever.recall = original  # type: ignore[method-assign]
    live = await app.memory.list_memories(limit=200)
    return ProbeReport(
        generated_at=now or datetime.now(UTC),
        probe_digest=probe_digest,
        live_model=script is None,
        implementation=_implementation(),
        store={"live_sampled": len(live)},
        probes=tuple(observations),
        summary=summarize(tuple(observations)),
    )


def _implementation() -> dict[str, str]:
    hashes = implementation_hashes()
    here = Path(__file__).resolve().parent
    for name in ("owner_recall_probes.py", "owner_recall_probe_driver.py"):
        hashes[f"evals/{name}"] = hashlib.sha256((here / name).read_bytes()).hexdigest()
    return hashes


def _script_turns(probe: Probe, path: PathKind) -> list[ScriptedTurn]:
    if path == "deliberate":
        return [
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        arguments={"text": probe.search_text or probe.question},
                        call_id=f"probe-{probe.id}-search",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text=SCRIPTED_ANSWER),
        ]
    return [ScriptedTurn(text=SCRIPTED_ANSWER)]


async def _observe_path(
    app: Composition,
    script: FakeModelScript | None,
    probe: Probe,
    path: PathKind,
    captured: list[RecallResult],
    finish: Finish,
) -> PathObservation:
    _rewind(app, script, _script_turns(probe, path))
    session_id = await app.sessions.create()
    started = perf_counter()
    run_id = await app.runs.submit(probe.question, session_id=session_id)
    run = await finish(app, run_id)
    elapsed_ms = int((perf_counter() - started) * 1000)

    async with app.uow_factory() as uow:
        traces = [
            trace
            for trace in await uow.traces.for_turn(run_id)
            if (trace.tenant_id, trace.principal_id)
            == (app.principal.tenant_id, app.principal.principal_id)
        ]
        events = await uow.events.list_after(session_id, 0, app.principal, run_id=run_id)
    trace_ids = {trace.id for trace in traces}
    results = [result for result in captured if result.trace_id in trace_ids]
    delivered_trace_ids = {
        UUID(value)
        for event in events
        if event.event_type == "memory.delivered"
        for value in event.payload.get("trace_ids", [])
    }
    delivered: set[UUID] = set()
    for trace in traces:
        if trace.id in delivered_trace_ids:
            delivered.update(trace.returned)
    answer = run.final_message if script is None else None
    arms = tuple(sorted({arm for trace in traces for arm in trace.arm_latencies_ms}))
    recall_latency = max((sum(trace.arm_latencies_ms.values()) for trace in traces), default=0)
    within = None
    if probe.latency_ceiling_ms is not None:
        within = recall_latency <= probe.latency_ceiling_ms

    evidence: list[EvidenceObservation] = []
    for index, expected in enumerate(probe.expected):
        record = await _resolve(app, expected.belief_id, expected.statement_contains)
        located, stages = _locate(record, traces, results)
        stage = evidence_stage(
            record,
            stages,
            located,
            delivered=delivered,
            answer=answer,
            ceiling=located.sensitivity_ceiling if located is not None else Sensitivity.RESTRICTED,
        )
        rank = None
        if record is not None and stages is not None and record.id in stages.ranked_ids:
            rank = stages.ranked_ids.index(record.id) + 1
        evidence.append(
            EvidenceObservation(
                expected_index=index,
                required=expected.required,
                belief_id=None if record is None else record.id,
                stage=stage,
                rank=rank,
            )
        )
    required_delivered = sum(
        1
        for item in evidence
        if item.required and stage_rank(item.stage) >= stage_rank("delivered")
    )
    forbidden_delivered = 0
    if probe.forbidden and delivered:
        async with app.uow_factory() as uow:
            for belief_id in delivered:
                try:
                    record = await uow.memories.get(belief_id, app.principal)
                except NotFoundError:
                    continue
                text = record.statement.casefold()
                if any(item.statement_contains.casefold() in text for item in probe.forbidden):
                    forbidden_delivered += 1
    answer_correct = None
    if answer is not None and (probe.answer_must_contain or probe.answer_must_not_contain):
        lowered = answer.casefold()
        answer_correct = all(
            needle.casefold() in lowered for needle in probe.answer_must_contain
        ) and not any(needle.casefold() in lowered for needle in probe.answer_must_not_contain)
    return PathObservation(
        path=path,
        run_id=run_id,
        run_status=run.status.value,
        trace_ids=tuple(sorted(trace_ids, key=str)),
        delivered_trace_ids=tuple(sorted(delivered_trace_ids & trace_ids, key=str)),
        arms=arms,
        elapsed_ms=elapsed_ms,
        recall_latency_ms=recall_latency,
        within_latency_ceiling=within,
        evidence=tuple(evidence),
        required_delivered=required_delivered,
        forbidden_delivered=forbidden_delivered,
        answer_correct=answer_correct,
    )


async def _belief_for_short_id(app: Composition, match: re.Match[str] | None) -> UUID | None:
    """The remember tool answers with the renderer's short form, never a full identifier."""

    if match is None:
        return None
    short = match.group(1)
    for record in await app.memory.list_memories(include_inactive=True, limit=200):
        if str(record.id).startswith(short):
            return record.id
    return None


async def _resolve(
    app: Composition, belief_id: UUID | None, statement_contains: str | None
) -> MemoryRecord | None:
    if belief_id is not None:
        async with app.uow_factory() as uow:
            try:
                return await uow.memories.get(belief_id, app.principal)
            except NotFoundError:
                return None
    assert statement_contains is not None
    needle = statement_contains.casefold()
    matches = [
        record
        for record in await app.memory.list_memories(include_inactive=True, limit=200)
        if needle in record.statement.casefold()
    ]
    live = [
        record
        for record in matches
        if record.status in {MemoryStatus.ACTIVE, MemoryStatus.PROVISIONAL}
    ]
    chosen = live or matches
    return max(chosen, key=lambda record: record.store_position) if chosen else None


def _locate(
    record: MemoryRecord | None, traces: list[RecallTrace], results: list[RecallResult]
) -> tuple[RecallTrace | None, Any]:
    """Pick the trace and stages that carry the belief furthest; else the first in-turn ones."""

    best: tuple[int, RecallTrace | None, Any] = (-1, None, None)
    for result in results:
        trace = next((item for item in traces if item.id == result.trace_id), None)
        stages = result.stages
        score = 0
        if record is not None and stages is not None:
            score += int(record.id in stages.candidate_ids) + int(record.id in stages.ranked_ids)
        if record is not None and trace is not None and record.id in trace.returned:
            score += 1
        if score > best[0]:
            best = (score, trace, stages)
    if best[1] is None and traces:
        return traces[0], None
    return best[1], best[2]


async def _observe_save(
    app: Composition, script: FakeModelScript | None, probe: Probe, finish: Finish
) -> SaveObservation:
    assert probe.write is not None
    write = probe.write
    remember_call = f"probe-{probe.id}-remember"
    if script is not None:
        turns: list[ScriptedTurn] = []
        if write.web_results:
            turns.append(
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="web.search",
                            arguments={"query": probe.question[:200], "max_results": 3},
                            call_id=f"probe-{probe.id}-web",
                        )
                    ],
                    stop_reason=StopReason.TOOL_USE,
                )
            )
        turns.append(
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.remember",
                        arguments={
                            "statement": write.statement,
                            "subject": write.subject,
                            "scope": write.scope,
                        },
                        call_id=remember_call,
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        )
        turns.append(ScriptedTurn(text=SCRIPTED_ANSWER))
        _rewind(app, script, turns)
    session_id = await app.sessions.create()
    run_id = await app.runs.submit(probe.question, session_id=session_id)
    run = await finish(app, run_id)
    async with app.uow_factory() as uow:
        events = await uow.events.list_after(session_id, 0, app.principal, run_id=run_id)
    proposed = {
        event.payload.get("call_id"): event.payload.get("name")
        for event in events
        if event.event_type == "tool.call.proposed"
    }
    remember_ids = {call_id for call_id, name in proposed.items() if name == "memory.remember"}
    if script is None:
        remember_call = str(next(iter(remember_ids), remember_call))
    status = "not_attempted"
    belief_id = None
    for event in events:
        if event.payload.get("call_id") != remember_call:
            continue
        if event.event_type == "tool.call.failed":
            status = f"save_refused:{event.payload.get('reason_code', 'unknown')}"
        elif event.event_type == "tool.call.completed":
            item = event.payload.get("result_item") or {}
            if item.get("is_error"):
                status = f"save_refused:{event.payload.get('reason_code', 'unknown')}"
                continue
            text = "".join(
                part.get("text", "") for part in item.get("content", []) if isinstance(part, dict)
            )
            status = "committed"
            belief_id = await _belief_for_short_id(app, _SHORT_ID.search(text))
        elif event.event_type == "tool.call.denied":
            status = f"save_refused:{event.payload.get('reason_code', 'denied')}"
    if run.status is not RunStatus.COMPLETED and status == "not_attempted":
        status = f"run_{run.status.value.lower()}"
    return SaveObservation(run_id=run_id, status=status, belief_id=belief_id)
