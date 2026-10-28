"""ADR-0261: admission marks captures and points the marker at the reader."""

from __future__ import annotations

from pathlib import Path

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.domain.captures import CAPTURE_METADATA_KEY, CaptureSections, capture_text
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tool_output import content_bytes
from agent_core.domain.tools import ToolResult
from agent_core.tools.artifact_read_capture import ArtifactReadCaptureTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import principal, run
from tests.gates.test_artifact_m6 import _LargeOutputTool
from tests.unit.test_history_cache_window import _factory

INLINE_BYTES = 4096
FILING = "".join(f"Item {n}. Results of operations {n}\n" for n in range(14_000))


async def _pipeline(tmp_path: Path) -> tuple[ToolPipeline, object, FilesystemArtifactStore]:
    clock, factory = await _factory()
    store = FilesystemArtifactStore(tmp_path)
    writers = ArtifactWriterFactory(factory, store, clock, SequenceIdFactory())
    pipeline = ToolPipeline(
        StaticToolRegistry(), factory, clock, SequenceIdFactory(), artifact_writers=writers
    )
    return pipeline, factory, store


def _large_tool(trust: TrustLevel = TrustLevel.EXTERNAL_UNTRUSTED) -> _LargeOutputTool:
    tool = _LargeOutputTool()
    tool.spec = tool.spec.model_copy(
        update={
            "maximum_output_bytes": 1_048_576,
            "output_trust": trust,
            "required_scopes": {"web.read", "a.scope"},
        }
    )
    return tool


async def test_the_marker_names_the_reader_and_the_capture_is_marked(tmp_path: Path) -> None:
    pipeline, factory, _ = await _pipeline(tmp_path)
    tool = _large_tool()
    result = await pipeline._artifactize_large_output(
        result=ToolResult(ok=True, content=[TextPart(text=FILING)]),
        tool=tool,
        run=run(),
        principal=principal(),
        capture_reader=True,
    )
    assert result.context_content is not None
    excerpt = result.context_content[0]
    assert isinstance(excerpt, TextPart)
    reference = next(p for p in result.context_content if isinstance(p, FileReferencePart))
    artifact = reference.artifact_id
    sections = CaptureSections(FILING, artifact_id=artifact, tool=tool.spec.name)
    assert sections.count > 1
    call = f'{{"name":"artifact.read_capture","arguments":{{"artifact":"{artifact}","section":1}}}}'
    assert (
        f"full output: artifact:{artifact}, {sections.count} sections; "
        f'read one with tool.call {call}, or pass "find" to search it ...'
    ) in excerpt.text
    assert len(content_bytes(result.context_content)) <= INLINE_BYTES
    async with factory() as uow:  # type: ignore[operator]
        row = await uow.artifacts.get(artifact, principal())
    assert row.metadata == {
        CAPTURE_METADATA_KEY: {"tool": tool.spec.name, "required_scopes": ["a.scope", "web.read"]}
    }


async def test_a_trusted_capture_keeps_the_marker_that_says_nothing_opens_it(
    tmp_path: Path,
) -> None:
    pipeline, factory, _ = await _pipeline(tmp_path)
    result = await pipeline._artifactize_large_output(
        result=ToolResult(ok=True, content=[TextPart(text=FILING)]),
        tool=_large_tool(TrustLevel.INTERNAL_TOOL),
        run=run(),
        principal=principal(),
        capture_reader=True,
    )
    assert result.context_content is not None
    excerpt = result.context_content[0]
    assert isinstance(excerpt, TextPart)
    assert "kept for the owner; no tool, sandbox or workspace can open it ..." in excerpt.text
    assert "artifact.read_capture" not in excerpt.text
    reference = next(p for p in result.context_content if isinstance(p, FileReferencePart))
    async with factory() as uow:  # type: ignore[operator]
        row = await uow.artifacts.get(reference.artifact_id, principal())
    assert CAPTURE_METADATA_KEY in row.metadata


async def test_without_the_reader_the_marker_is_adr_0260s(tmp_path: Path) -> None:
    pipeline, _, _ = await _pipeline(tmp_path)
    result = await pipeline._artifactize_large_output(
        result=ToolResult(ok=True, content=[TextPart(text=FILING)]),
        tool=_large_tool(),
        run=run(),
        principal=principal(),
    )
    assert result.context_content is not None
    excerpt = result.context_content[0]
    assert isinstance(excerpt, TextPart)
    assert "no tool, sandbox or workspace can open it" in excerpt.text


async def test_a_section_is_admitted_whole_and_never_captured_again(tmp_path: Path) -> None:
    pipeline, factory, _ = await _pipeline(tmp_path)
    reader = ArtifactReadCaptureTool.spec
    sections = CaptureSections('"雪\\' * 20_000, artifact_id=run().id, tool="web.fetch")
    section = ToolResult(
        ok=True,
        content=[TextPart(text=sections.section_text(1))],
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )
    assert INLINE_BYTES < len(content_bytes(section.content)) <= reader.maximum_output_bytes

    class _Reader:
        spec = reader

    admitted = await pipeline._artifactize_large_output(
        result=section,
        tool=_Reader(),  # type: ignore[arg-type]
        run=run(),
        principal=principal(),
        capture_reader=True,
    )
    assert admitted.content == section.content
    assert admitted.context_content is None
    assert admitted.artifacts == []
    async with factory() as uow:  # type: ignore[operator]
        assert await uow.artifacts.list_for_run(run().id, principal()) == []


async def test_the_marker_count_is_the_readers_count(tmp_path: Path) -> None:
    pipeline, _, _ = await _pipeline(tmp_path)
    tool = _large_tool()
    rendered = content_bytes([TextPart(text=FILING)])
    result = await pipeline._artifactize_large_output(
        result=ToolResult(ok=True, content=[TextPart(text=FILING)]),
        tool=tool,
        run=run(),
        principal=principal(),
        capture_reader=True,
    )
    assert result.context_content is not None
    reference = next(p for p in result.context_content if isinstance(p, FileReferencePart))
    assert capture_text(rendered) == FILING
    count = CaptureSections(
        capture_text(rendered), artifact_id=reference.artifact_id, tool=tool.spec.name
    ).count
    excerpt = result.context_content[0]
    assert isinstance(excerpt, TextPart)
    assert f", {count} sections;" in excerpt.text


def test_two_sections_fit_one_truncation_block_on_the_production_window() -> None:
    """ADR-0261, Consequences: a step of reads moves the truncated prefix at most once."""

    from agent_core.context.budget import ContextBudgetAllocator
    from agent_core.context.builder import TOOL_RESULT_TRUNCATION_BLOCKS
    from agent_core.context.estimator import ConservativeTokenEstimator
    from agent_core.domain.messages import ModelLimits, ResolvedModel, ToolResultItem
    from tests.contract.support import NOW
    from tests.unit.test_history_cache_window import CONFIG

    astra = ResolvedModel(
        provider="openai",
        model="gpt-6-astra",
        resolved_at=NOW,
        limits=ModelLimits(context_window_tokens=272_000, max_output_tokens=128_000),
    )
    budget = ContextBudgetAllocator(CONFIG).allocate(astra, prefix_tokens=24_000)
    block = budget.tool_result_tokens // TOOL_RESULT_TRUNCATION_BLOCKS
    sections = CaptureSections(FILING, artifact_id=run().id, tool="web.fetch")
    results = [
        ToolResultItem(
            call_id=f"read-{number}",
            content=[TextPart(text=sections.section_text(number))],
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
        for number in (1, 2)
    ]
    estimated = ConservativeTokenEstimator().estimate(results, "openai:gpt-6-astra")
    assert estimated <= block, (estimated, block)
