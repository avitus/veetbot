"""Rendered evidence reuse requires an explicit complete dependency revision."""

import asyncio
from typing import Any

import pytest

from agent_core.domain.messages import TextPart, UserMessage
from agent_core.domain.runs import RunCheckpoint, RunStatus
from tests.contract.support import NOW, agent, principal, run
from tests.gates.test_context_m7 import _builder, _CountingMemoryRetriever, _StaticQueryFormer


def checkpoint() -> RunCheckpoint:
    return RunCheckpoint(
        run_id=run().id,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text="How should you answer?")])],
        created_at=NOW,
    )


@pytest.mark.parametrize("failure", ["unknown", "exception"])
async def test_unavailable_revision_revokes_completed_evidence_and_recovers(failure: str) -> None:
    retriever = _CountingMemoryRetriever()
    builder = _builder(memory_retriever=retriever, query_former=_StaticQueryFormer())
    available = True

    async def revision() -> int | None:
        if available:
            return 0
        if failure == "exception":
            raise RuntimeError("revision unavailable")
        return None

    builder._recall_revision = revision
    live = run(status=RunStatus.RUNNING)
    saved = checkpoint()
    await builder.build(live, saved, agent(), principal())
    await builder.build(live, saved, agent(), principal())
    assert retriever.calls == 1
    available = False
    request = await builder.build(live, saved, agent(), principal())
    assert retriever.calls == 2
    assert "concise" in request.model_dump_json()
    assert builder._recall_tasks == {}
    available = True
    await builder.build(live, saved, agent(), principal())
    assert retriever.calls == 3, "restoring a revision must not revive revoked evidence"


@pytest.mark.parametrize("change", ["scopes", "roles", "run", "checkpoint"])
async def test_opted_in_reuse_binds_authority_and_complete_checkpoint(change: str) -> None:
    retriever = _CountingMemoryRetriever()
    builder = _builder(memory_retriever=retriever, query_former=_StaticQueryFormer())

    async def revision() -> int:
        return 0

    builder._recall_revision = revision
    live, saved, owner = run(status=RunStatus.RUNNING), checkpoint(), principal()
    await builder.build(live, saved, agent(), owner)
    if change == "scopes":
        owner = owner.model_copy(update={"scopes": {"memory.read"}})
    elif change == "roles":
        owner = owner.model_copy(update={"roles": {"admin"}})
    elif change == "run":
        live = live.model_copy(update={"lease_epoch": live.lease_epoch + 1})
    else:
        saved = saved.model_copy(update={"budget_state": {"task_epoch": 2}})
    await builder.build(live, saved, agent(), owner)
    assert retriever.calls == 2


async def test_uncached_cancellation_stops_the_read_without_detached_work() -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Blocking(_CountingMemoryRetriever):
        async def recall(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    builder = _builder(memory_retriever=Blocking(), query_former=_StaticQueryFormer())
    task = asyncio.create_task(
        builder.build(run(status=RunStatus.RUNNING), checkpoint(), agent(), principal())
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert builder._recall_tasks == {}


async def test_transient_recall_failure_is_not_retained() -> None:
    retriever = _CountingMemoryRetriever(fail=True)
    builder = _builder(memory_retriever=retriever, query_former=_StaticQueryFormer())
    live, saved = run(status=RunStatus.RUNNING), checkpoint()
    before = await builder.build(live, saved, agent(), principal())
    assert "concise" not in before.model_dump_json()
    retriever._fail = False
    after = await builder.build(live, saved, agent(), principal())
    assert "concise" in after.model_dump_json()
    assert retriever.calls == 2
