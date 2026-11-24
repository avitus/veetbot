"""Replay owner recall probes through the composed production run loop.

`app` is the composed application (`agent_core.bootstrap.Composition`); the
driver takes it duck-typed so the evaluators never import the composition root.

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
import html
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from uuid import UUID

from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import (
    MemoryBrowseQuery,
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
from agent_core.evals.owner_recall_questions import question_fingerprint

_SHORT_ID = re.compile(r"\[m:([0-9a-f]{8})\]")
# The store's largest browse page; the locator walks every page.
_BROWSE_PAGE = 200
SCRIPTED_ANSWER = "Answered from the delivered evidence."
# Orders a memory the ranking never listed behind every one it did.
_UNRANKED = 1_000_000_000
Finish = Callable[[Any, UUID], Awaitable[Run]]


async def _wait(app: Any, run_id: UUID) -> Run:
    return cast(Run, await app.runs.wait_terminal(run_id))


def _rewind(
    app: Any, script: FakeModelScript | None, turns: list[ScriptedTurn], stand_in: Any = None
) -> None:
    """Give the scripted provider this run's turns from the top."""

    if script is None:
        return
    script.turns[:] = turns
    for provider in (getattr(app.executor, "model_provider", None), stand_in):
        rewind = getattr(provider, "rewind", None)
        if callable(rewind):
            rewind()


@asynccontextmanager
async def scripted_chat(
    script: FakeModelScript, *, model_policy: str, **build_arguments: Any
) -> AsyncIterator[tuple[Any, Any]]:
    """Compose scripted runs that carry the identity of the chat model a real chat uses.

    A run takes the budget it packs memories into, and the reader its semantic
    recall is admitted for, from the model it is on. A scripted composition is
    otherwise on the fake model, whose limits and reader no real chat has, so
    it measured recall a chat never gets (ADR-0343). Here the run is routed to
    `model_policy` like a chat, the model's own adapter is never constructed,
    and the scripted provider answers in its place: no model is called.

    Yields the composition and that provider; hand the provider to
    `observe_probes` as `stand_in` so each run starts its script over.
    """

    from agent_core import bootstrap
    from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
    from agent_core.config import PACKAGE_ROOT, load_settings
    from agent_core.model.registry import ProviderRegistry, StaticModelRouter

    settings = build_arguments.get("settings") or load_settings()
    clock = build_arguments.get("clock") or bootstrap.system_clock()
    registry = ProviderRegistry.load(
        PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS, overlay_root=settings.config_dir
    )
    resolved = await StaticModelRouter(registry, clock).resolve(
        model_policy, tenant_id="owner-recall-probe"
    )
    stand_in = bootstrap.scripted_provider(script, clock)
    async with bootstrap.build(
        script=script,
        model_policy=model_policy,
        model_provider_overrides={resolved.provider: stand_in},
        model_metadata_only=True,
        **build_arguments,
    ) as app:
        yield app, stand_in


async def observe_probes(
    app: Any,
    script: FakeModelScript | None,
    probes: Sequence[Probe],
    *,
    finish: Finish = _wait,
    paths: Sequence[PathKind] | None = None,
    probe_digest: str | None = None,
    now: datetime | None = None,
    stand_in: Any = None,
) -> ProbeReport:
    """Run every probe path and write probe, return the content-free report.

    With a scripted model the answer is fixed, so Used and answer correctness
    are not judged; with the live model (`script is None`) they are. `stand_in`
    is the scripted provider of a `scripted_chat` composition, which answers
    in a routed model's place and is started over with each run.
    """

    captured: list[RecallResult] = []
    retriever = app.memory_retriever
    original = retriever.recall

    async def recording(*args: Any, **kwargs: Any) -> RecallResult:
        result = cast(RecallResult, await original(*args, **kwargs))
        captured.append(result)
        return result

    retriever.recall = recording
    observations: list[ProbeObservation] = []
    try:
        for probe in probes:
            path_observations = []
            for path in probe.paths:
                if paths is not None and path not in paths:
                    continue
                path_observations.append(
                    await _observe_path(app, script, probe, path, captured, finish, stand_in)
                )
            save = None
            if probe.write is not None:
                save = await _observe_save(app, script, probe, finish, stand_in)
            observations.append(
                ProbeObservation(
                    probe_id=probe.id,
                    incident=probe.incident,
                    family=probe.family,
                    content_sha256=question_fingerprint(probe),
                    paths=tuple(path_observations),
                    save=save,
                )
            )
    finally:
        retriever.recall = original
    live = await app.memory.list_memories(limit=200)
    return ProbeReport(
        generated_at=now or app.clock.now(),
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
    app: Any,
    script: FakeModelScript | None,
    probe: Probe,
    path: PathKind,
    captured: list[RecallResult],
    finish: Finish,
    stand_in: Any = None,
) -> PathObservation:
    _rewind(app, script, _script_turns(probe, path), stand_in)
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
    rendered = html.unescape(
        "\n".join(trace.rendered for trace in traces if trace.id in delivered_trace_ids)
    ).casefold()
    delivered_found = tuple(
        re.search(rf"(?<!\w){re.escape(text.casefold())}(?!\w)", rendered) is not None
        for text in probe.delivered_must_contain
    )
    answer = run.final_message if script is None else None
    arms = tuple(sorted({arm for trace in traces for arm in trace.arms_ran}))
    recall_latency = max((trace.recall_latency_ms or 0 for trace in traces), default=0)
    within = None
    if probe.latency_ceiling_ms is not None:
        within = recall_latency <= probe.latency_ceiling_ms

    evidence: list[EvidenceObservation] = []
    store = _StoreSnapshot(app)
    for index, expected in enumerate(probe.expected):
        # A text names a fact, and every live memory that contains it states
        # that fact: the fact got as far as the memory that got furthest. The
        # newest one alone can be a later mention that no question asks for
        # (ADR-0346).
        holders: list[MemoryRecord | None] = [
            *await _holders(app, expected.belief_id, expected.statement_contains, store)
        ]
        furthest: tuple[tuple[int, int], EvidenceObservation] | None = None
        for holder in holders or [None]:
            located, stages = _locate(holder, traces, results)
            stage = evidence_stage(
                holder,
                stages,
                located,
                delivered=delivered,
                answer=answer,
                ceiling=located.sensitivity_ceiling
                if located is not None
                else Sensitivity.RESTRICTED,
            )
            rank = None
            if holder is not None and stages is not None and holder.id in stages.ranked_ids:
                rank = stages.ranked_ids.index(holder.id) + 1
            # Holders come newest first, so the newest keeps an exact tie.
            reached = (stage_rank(stage), -(rank if rank is not None else _UNRANKED))
            if furthest is None or reached > furthest[0]:
                furthest = (
                    reached,
                    EvidenceObservation(
                        expected_index=index,
                        required=expected.required,
                        belief_id=None if holder is None else holder.id,
                        stage=stage,
                        rank=rank,
                    ),
                )
        assert furthest is not None
        evidence.append(furthest[1])
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
        delivered_found=delivered_found,
        required_delivered=required_delivered,
        forbidden_delivered=forbidden_delivered,
        answer_correct=answer_correct,
    )


async def _belief_for_short_id(app: Any, match: re.Match[str] | None) -> UUID | None:
    """The remember tool answers with the renderer's short form, never a full identifier."""

    if match is None:
        return None
    short = match.group(1)
    for record in await app.memory.list_memories(include_inactive=True, limit=200):
        if str(record.id).startswith(short):
            return cast(UUID, record.id)
    return None


class _StoreSnapshot:
    """The owner's whole store, read once and only when an expected text asks for it.

    One observation may name many expected texts. Reading every belief again
    for each of them multiplies the store by the number of texts.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._records: list[MemoryRecord] | None = None

    async def records(self) -> list[MemoryRecord]:
        if self._records is None:
            self._records = await _every_belief(self._app)
        return self._records


async def _holders(
    app: Any,
    belief_id: UUID | None,
    statement_contains: str | None,
    store: _StoreSnapshot | None = None,
) -> list[MemoryRecord]:
    """The memories that can stand for one expected fact, newest first.

    A pinned memory stands alone. An expected text is stated by every live
    memory that contains it. When none is live, the newest retired or
    superseded one stands for it, so the report can say why the fact is gone.
    """

    if belief_id is not None:
        async with app.uow_factory() as uow:
            try:
                return [cast(MemoryRecord, await uow.memories.get(belief_id, app.principal))]
            except NotFoundError:
                return []
    assert statement_contains is not None
    needle = statement_contains.casefold()
    records = await (store or _StoreSnapshot(app)).records()
    matches = sorted(
        (record for record in records if needle in record.statement.casefold()),
        key=lambda record: record.store_position,
        reverse=True,
    )
    live = [
        record
        for record in matches
        if record.status in {MemoryStatus.ACTIVE, MemoryStatus.PROVISIONAL}
    ]
    return live or matches[:1]


async def _resolve(
    app: Any,
    belief_id: UUID | None,
    statement_contains: str | None,
    store: _StoreSnapshot | None = None,
) -> MemoryRecord | None:
    """The newest memory that can stand for one expected fact, if any."""

    holders = await _holders(app, belief_id, statement_contains, store)
    return holders[0] if holders else None


async def _every_belief(app: Any) -> list[MemoryRecord]:
    """Every belief the owner's store holds, in any status, read page by page.

    One page is not the store: an expected text that matched a belief outside
    the first page was reported as never stored, a formation finding the run
    did not make.
    """

    records: list[MemoryRecord] = []
    cursor: tuple[int, UUID] | None = None
    async with app.uow_factory() as uow:
        while True:
            page = await uow.memories.browse(
                MemoryBrowseQuery(
                    tenant_id=app.principal.tenant_id,
                    principal_id=app.principal.principal_id,
                    ceiling=Sensitivity.RESTRICTED,
                    statuses=tuple(MemoryStatus),
                    limit=_BROWSE_PAGE,
                    cursor=cursor,
                )
            )
            records.extend(page[:_BROWSE_PAGE])
            if len(page) <= _BROWSE_PAGE:
                return records
            last = page[_BROWSE_PAGE - 1]
            cursor = (last.store_position, last.id)


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
    app: Any, script: FakeModelScript | None, probe: Probe, finish: Finish, stand_in: Any = None
) -> SaveObservation:
    assert probe.write is not None
    write = probe.write
    remember_call = f"probe-{probe.id}-remember"
    if script is not None:
        search = ScriptedToolCall(
            name="web.search",
            arguments={"query": probe.question[:200], "max_results": 3},
            call_id=f"probe-{probe.id}-web",
        )
        remember = ScriptedToolCall(
            name="memory.remember",
            arguments={
                "statement": write.statement,
                "subject": write.subject,
                "scope": write.scope,
            },
            call_id=remember_call,
        )
        turns: list[ScriptedTurn] = []
        if write.web_results and write.same_step:
            turns.append(
                ScriptedTurn(tool_calls=[search, remember], stop_reason=StopReason.TOOL_USE)
            )
        else:
            if write.web_results:
                turns.append(ScriptedTurn(tool_calls=[search], stop_reason=StopReason.TOOL_USE))
            turns.append(ScriptedTurn(tool_calls=[remember], stop_reason=StopReason.TOOL_USE))
        turns.append(ScriptedTurn(text=SCRIPTED_ANSWER))
        _rewind(app, script, turns, stand_in)
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
