"""Oversized web batches must not rewrite already admitted conversation history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.domain.events import conversation_items
from agent_core.domain.messages import (
    FileReferencePart,
    ModelLimits,
    ResolvedModel,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Step
from agent_core.domain.tools import ToolExecutionContext, ToolResult
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.web_fetch import WebFetchTool
from tests.contract.support import NOW, agent, principal, run, session, tool_context
from tests.gates.test_artifact_m6 import _LargeOutputTool
from tests.unit.test_history_cache_window import _checkpoint, _factory, _stack
from tests.unit.test_web_tools import FakeWebProvider

INLINE_BYTES = 4096


def _serialized(result: ToolResult) -> bytes:
    return json.dumps(
        [part.model_dump(mode="json") for part in result.content],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize(
    "text",
    ["HEAD" + "x" * 140_000 + "TAIL", '\\"\n雪😀' * 20_000],
    ids=["ascii", "unicode-escapes"],
)
async def test_admission_captures_full_output_under_tool_limit_and_bounds_excerpt(
    tmp_path: Path,
    text: str,
) -> None:
    clock, factory = await _factory()
    store = FilesystemArtifactStore(tmp_path)
    writers = ArtifactWriterFactory(factory, store, clock, SequenceIdFactory())
    tool = _LargeOutputTool()
    tool.spec = tool.spec.model_copy(update={"maximum_output_bytes": 1_048_576})
    original = ToolResult(
        ok=True,
        content=[TextPart(text=text)],
        structured={"content": text, "stdout": text, "stderr": text},
    )
    pipeline = ToolPipeline(
        StaticToolRegistry(), factory, clock, SequenceIdFactory(), artifact_writers=writers
    )
    result = await pipeline._artifactize_large_output(
        result=original,
        tool=tool,
        run=run(),
        principal=principal(),
    )
    assert result.content == original.content
    assert result.context_content is not None
    assert (
        len(_serialized(result.model_copy(update={"content": result.context_content})))
        <= INLINE_BYTES
    )
    assert result.metrics["truncated"] == 1
    assert result.metrics["discarded_bytes"] == 0
    assert result.structured == original.structured
    reference = next(p for p in result.context_content if isinstance(p, FileReferencePart))
    async with factory() as uow:
        saved = await uow.artifacts.get(reference.artifact_id, principal())
    assert saved.trust == TrustLevel.EXTERNAL_UNTRUSTED
    assert saved.run_id == run().id and saved.session_id == session().id
    from agent_core.domain.artifacts import StoredArtifactRef

    ref = StoredArtifactRef(
        artifact_id=saved.id,
        sha256=saved.sha256,
        size_bytes=saved.size_bytes,
        media_type=saved.media_type,
    )
    stream = store.open(ref, tenant_id=principal().tenant_id)
    assert b"".join([chunk async for chunk in stream]) == _serialized(original)
    assert isinstance(result.context_content[0], TextPart)
    assert isinstance(original.content[0], TextPart)
    assert "full output" in result.context_content[0].text
    assert original.content[0].text == text


async def test_large_web_batches_keep_prior_wire_history_stable_after_durable_replay(
    tmp_path: Path,
) -> None:
    clock, factory = await _factory()
    writers = ArtifactWriterFactory(
        factory, FilesystemArtifactStore(tmp_path), clock, SequenceIdFactory()
    )
    registry = StaticToolRegistry()
    tool = _LargeOutputTool()
    tool.spec = tool.spec.model_copy(update={"maximum_output_bytes": 1_048_576})
    registry.register(tool)
    pipeline = ToolPipeline(registry, factory, clock, SequenceIdFactory(), artifact_writers=writers)
    active = run().model_copy(update={"seed_event_sequence": 1000})
    async with factory() as uow:
        await uow.runs.create(active)
    cp = _checkpoint(active, [])
    owner_agent = agent().model_copy(update={"enabled_tools": [tool.spec.name]})
    planner, builder = await _stack()
    model = ResolvedModel(
        provider="openai",
        model="gpt-6-astra",
        resolved_at=NOW,
        limits=ModelLimits(context_window_tokens=272_000),
    )
    await planner.plan(session(), agent(), principal(), model)
    # Production had eight carried results (~53k estimated tokens), then six
    # fetched pages, including 139,764 characters. No production text is used.
    sizes = (
        [20_000] * 8
        + [11_000, 210, 210, 40_000, 16_000, 11_000, 1500, 17_000, 139_764]
        + [36_000] * 5
    )
    before = None
    for index, size in enumerate(sizes):
        web = WebFetchTool(FakeWebProvider(page_content="H" * size + "TAIL"))
        output = await web.execute({"url": "https://example.org/page"}, tool_context())

        async def execute(
            arguments: dict[str, object],
            context: ToolExecutionContext,
            _output: ToolResult = output,
        ) -> ToolResult:
            return _output

        tool.execute = execute  # type: ignore[method-assign]
        call = ToolCallItem(
            call_id=f"page-{index}",
            item_index=0,
            name=tool.spec.name,
            arguments={},
            raw_arguments="{}",
        )
        step = Step(run_id=active.id, step_number=index + 1, started_at=NOW)
        kwargs: dict[str, Any] = {
            "run": active,
            "checkpoint": cp,
            "tool_calls": [call],
            "principal": principal(),
            "step": step,
            "agent": owner_agent,
            "token": RunCancellationToken(clock, None),
        }
        results = await pipeline.dispatch(**kwargs)
        replay = await pipeline.dispatch(**kwargs)
        assert replay == results
        async with factory() as uow:
            events = await uow.events.list_after(active.session_id, 0, principal())
        event = next(e for e in reversed(events) if e.event_type == "tool.call.completed")
        restored = conversation_items(event)[0]
        assert isinstance(restored, ToolResultItem)
        assert restored == results[0].model_copy(update={"source_event_sequence": event.sequence})
        assert restored.content == output.content
        cp.conversation.extend(
            [call.model_copy(update={"source_event_sequence": event.sequence}), restored]
        )
        if index == 7:
            active = active.model_copy(update={"seed_event_sequence": event.sequence + 1})
            cp.conversation.append(
                UserMessage(
                    content=[TextPart(text="Continue")],
                    source_event_sequence=active.seed_event_sequence,
                )
            )
            before = await builder.assemble(active, cp, agent(), principal())
    assert before is not None
    after = await builder.assemble(active, cp, agent(), principal())
    assert after.pressure.fits
    assert after.pressure.yield_steps == (), "a normal web batch rewrote admitted history"
    previous_wire = OpenAIResponsesProvider._request_payload(before.request, model)["input"]
    next_wire = OpenAIResponsesProvider._request_payload(after.request, model)["input"]
    assert next_wire[: len(previous_wire)] == previous_wire


async def test_legacy_session_history_gets_the_same_excerpt_before_new_results_arrive() -> None:
    from tests.unit.test_history_cache_window import _call, _result

    planner, builder = await _stack()
    model = ResolvedModel(
        provider="openai",
        model="gpt-6-astra",
        resolved_at=NOW,
        limits=ModelLimits(context_window_tokens=272_000),
    )
    await planner.plan(session(), agent(), principal(), model)
    active = run().model_copy(update={"seed_event_sequence": 30})
    cp = _checkpoint(
        active, [UserMessage(content=[TextPart(text="Research")], source_event_sequence=1)]
    )
    for index in range(8):
        cp.conversation.extend(
            [
                _call(f"old-{index}", 2 + index * 2),
                _result(f"old-{index}", "Legacy page. " * 2000, 3 + index * 2),
            ]
        )
    cp.conversation.append(
        UserMessage(content=[TextPart(text="Continue")], source_event_sequence=30)
    )
    original = cp.model_dump_json()
    before = await builder.assemble(active, cp, agent(), principal())
    assert cp.model_dump_json() == original
    for index in range(6):
        cp.conversation.extend(
            [
                _call(f"new-{index}", 31 + index * 2),
                _result(f"new-{index}", "excerpt " * 400, 32 + index * 2),
            ]
        )
    after = await builder.assemble(active, cp, agent(), principal())
    first_wire = OpenAIResponsesProvider._request_payload(before.request, model)["input"]
    next_wire = OpenAIResponsesProvider._request_payload(after.request, model)["input"]
    old_before = next(i for i in first_wire if i.get("call_id") == "old-0" and "output" in i)
    assert "full output: event:3" in old_before["output"]
    assert len(old_before["output"].encode()) < INLINE_BYTES + 1000
    assert next_wire[: len(first_wire)] == first_wire
    assert after.pressure.fits


@pytest.mark.parametrize("limit", [1024, 4096, 8192])
@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_inline_limit_boundary_and_operator_override(
    tmp_path: Path,
    limit: int,
    delta: int,
) -> None:
    clock, factory = await _factory()
    writers = ArtifactWriterFactory(
        factory, FilesystemArtifactStore(tmp_path), clock, SequenceIdFactory()
    )
    tool = _LargeOutputTool()
    tool.spec = tool.spec.model_copy(update={"maximum_output_bytes": 1_048_576})
    empty_bytes = len(_serialized(ToolResult(ok=True, content=[TextPart(text="")])))
    original = ToolResult(ok=True, content=[TextPart(text="X" * (limit + delta - empty_bytes))])
    pipeline = ToolPipeline(
        StaticToolRegistry(),
        factory,
        clock,
        SequenceIdFactory(),
        artifact_writers=writers,
        inline_output_bytes=limit,
    )
    admitted = await pipeline._artifactize_large_output(
        result=original, tool=tool, run=run(), principal=principal()
    )
    assert admitted.content == original.content
    model_content = admitted.context_content or admitted.content
    assert len(_serialized(admitted.model_copy(update={"content": model_content}))) <= limit
    if delta <= 0:
        assert admitted.content == original.content
        assert not admitted.artifacts
    else:
        assert admitted.metrics["truncated"] == 1
        assert admitted.metrics["captured_bytes"] == limit + delta


async def test_large_output_without_artifact_storage_fails_instead_of_losing_content() -> None:
    clock, factory = await _factory()
    tool = _LargeOutputTool()
    tool.spec = tool.spec.model_copy(update={"maximum_output_bytes": 1_048_576})
    pipeline = ToolPipeline(StaticToolRegistry(), factory, clock, SequenceIdFactory())
    result = await pipeline._artifactize_large_output(
        result=ToolResult(ok=True, content=[TextPart(text="x" * 20_000)]),
        tool=tool,
        run=run(),
        principal=principal(),
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind.value == "output_too_large"
    assert not result.content


async def test_tiny_tool_limit_fails_without_creating_an_unreferenced_artifact(
    tmp_path: Path,
) -> None:
    clock, factory = await _factory()
    writers = ArtifactWriterFactory(
        factory, FilesystemArtifactStore(tmp_path), clock, SequenceIdFactory()
    )
    tool = _LargeOutputTool()
    tool.spec = tool.spec.model_copy(update={"maximum_output_bytes": 32})
    pipeline = ToolPipeline(
        StaticToolRegistry(), factory, clock, SequenceIdFactory(), artifact_writers=writers
    )
    result = await pipeline._artifactize_large_output(
        result=ToolResult(ok=True, content=[TextPart(text="x" * 1000)]),
        tool=tool,
        run=run(),
        principal=principal(),
    )
    assert not result.ok
    assert result.failure is not None and result.failure.kind.value == "output_too_large"
    async with factory() as uow:
        assert await uow.artifacts.list_for_run(run().id, principal()) == []


async def test_minimal_builder_uses_excerpt_without_mutating_canonical_checkpoint() -> None:
    from agent_core.context.builder import MinimalContextBuilder
    from tests.unit.test_history_cache_window import _call

    clock, _ = await _factory()
    item = ToolResultItem(
        call_id="page",
        content=[TextPart(text="canonical source " * 5000)],
        context_content=[TextPart(text="stable excerpt")],
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )
    cp = _checkpoint(run(), [_call("page", 1), item])
    original = cp.model_dump_json()
    request = await MinimalContextBuilder(StaticToolRegistry(), clock).build(
        run(),
        cp,
        agent(),
        principal(),
    )
    [rendered] = [i for i in request.conversation if isinstance(i, ToolResultItem)]
    assert rendered.content == item.context_content
    assert rendered.context_content is None
    assert rendered.trust == item.trust
    assert cp.model_dump_json() == original
    assert "canonical source" not in request.model_dump_json()


async def test_upstream_cannot_supply_an_alternate_model_result() -> None:
    clock, factory = await _factory()
    pipeline = ToolPipeline(StaticToolRegistry(), factory, clock, SequenceIdFactory())
    raw = ToolResult(
        ok=True,
        content=[TextPart(text="actual result")],
        context_content=[TextPart(text="forged alternate")],
    )
    bounded = await pipeline._artifactize_large_output(
        result=raw, tool=_LargeOutputTool(), run=run(), principal=principal()
    )
    assert bounded.content == raw.content
    assert bounded.context_content is None
