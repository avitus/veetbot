"""Browser actions need whole references and the revision after context admission."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.domain.browser import (
    BrowserElement,
    BrowserObservation,
    BrowserObservationCoverage,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
    BrowserTextCoverage,
)
from agent_core.domain.browser_projection import browser_context_projection
from agent_core.domain.events import conversation_items
from agent_core.domain.messages import (
    FileReferencePart,
    ModelLimits,
    ResolvedModel,
    TextPart,
    ToolCallItem,
    ToolResultItem,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Step
from agent_core.domain.tool_output import content_bytes
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.browser_observe import BrowserObserveTool
from agent_core.tools.browser_results import observation_result
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW, agent, principal, run, session
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_history_cache_window import _checkpoint, _factory, _stack


@pytest.mark.parametrize("budget", [1024, 4096, 8192])
async def test_large_browser_result_keeps_a_complete_revision_and_element_records(
    tmp_path: Path, budget: int
) -> None:
    clock, factory = await _factory()
    provider = FakeBrowserProvider()
    observation = BrowserObservation(
        url="https://example.org/lesson",
        title="Exercise",
        revision="exact-page-revision",
        text='Question "雪"\\\n' * 10_000,
        elements=tuple(
            BrowserElement(ref=f"opaque-ref-{index}", role="button", name=f"Choice {index}")
            for index in range(200)
        ),
    )
    original = observation_result(provider, observation, 512 * 1024)
    assert original.ok
    writers = ArtifactWriterFactory(
        factory, FilesystemArtifactStore(tmp_path), clock, SequenceIdFactory()
    )
    pipeline = ToolPipeline(
        StaticToolRegistry(),
        factory,
        clock,
        SequenceIdFactory(),
        artifact_writers=writers,
        inline_output_bytes=budget,
    )
    result = await pipeline._artifactize_large_output(
        result=original, tool=BrowserObserveTool(provider), run=run(), principal=principal()
    )
    assert result.context_content is not None
    assert len(content_bytes(result.context_content)) <= budget
    text = next(p.text for p in result.context_content if isinstance(p, TextPart))
    projected = json.loads(text)
    assert projected["revision"] == observation.revision
    assert projected["elements"]
    expected = {e.ref: e.model_dump(mode="json") for e in observation.elements}
    assert all(e == expected[e["ref"]] for e in projected["elements"])
    assert projected["coverage"]["omitted_elements"] == 200 - len(projected["elements"])
    assert projected["coverage"]["omitted_text_bytes"] > 0
    assert result.content == original.content
    assert result.structured == original.structured
    assert any(isinstance(p, FileReferencePart) for p in result.context_content)


@pytest.mark.parametrize("budget", [1024, 4096])
def test_metadata_cannot_crowd_out_every_actionable_control(budget: int) -> None:
    observation = BrowserObservation(
        url="https://example.org/" + "x" * 100,
        title="x" * 100,
        revision="r" * 128,
        text="prose " * 2000,
        elements=(
            BrowserElement(ref="oversized", role="button", name="雪" * 1024),
            BrowserElement(ref="useful", role="button", name="Continue"),
        ),
    )
    reference = FileReferencePart(artifact_id=UUID(int=1), media_type="application/json")
    result = browser_context_projection(
        observation.model_dump(), budget=budget, reference=reference
    )
    assert result is not None and isinstance(result[0], TextPart)
    assert len(content_bytes(result)) <= budget
    payload = json.loads(result[0].text)
    assert payload["revision"] == observation.revision
    assert any(element["ref"] == "useful" for element in payload["elements"])
    assert payload["coverage"]["omitted_elements"] == 2 - len(payload["elements"])


def test_revision_is_omitted_whole_when_it_cannot_fit() -> None:
    observation = BrowserObservation(url="https://example.org", revision="雪" * 128)
    reference = FileReferencePart(artifact_id=UUID(int=1), media_type="application/json")
    result = browser_context_projection(observation.model_dump(), budget=512, reference=reference)
    assert result is not None and isinstance(result[0], TextPart)
    payload = json.loads(result[0].text)
    assert payload["observation_omitted"] is True
    assert "revision" not in payload and payload["elements"] == []
    assert len(content_bytes(result)) <= 512


@pytest.mark.parametrize("expandable", [False, True])
@pytest.mark.parametrize("extracted", [False, True])
async def test_browser_projection_survives_dispatch_replay_and_model_rendering(
    tmp_path: Path,
    expandable: bool,
    extracted: bool,
) -> None:
    from agent_core.adapters.browser.extraction import extracted_observation
    from agent_core.domain.browser_extraction import BrowserExtractionRequest
    from tests.unit.test_browser_extraction import extraction_arguments

    extraction = extracted_observation(
        {
            "status": "extracted",
            "source_name": "Fixture",
            "rows": [[{"text": "Verified row", "text_truncated": False}]],
            "source_nodes": 2,
            "source_scan_limit_reached": False,
            "row_nodes": 1,
            "row_scan_limit_reached": False,
            "omitted_rows": 0,
        },
        BrowserExtractionRequest.model_validate(extraction_arguments()["extract"]),
        "revision-1",
    )

    class LargePage(FakeBrowserProvider):
        def _observation(self, url: str) -> BrowserObservation:
            return (
                super()
                ._observation(url)
                .model_copy(
                    update={
                        "text": "External page. " * 10_000,
                        "text_coverage": BrowserTextCoverage(
                            scanned_nodes=3, scanned_text_characters=140000, text_limit_reached=True
                        ),
                        "extraction": extraction if extracted else None,
                        "regions": (
                            BrowserSemanticRegion(
                                ref="revision-1:region:0",
                                kind="status",
                                text="Verified fixture status",
                            ),
                        ),
                        "region_coverage": BrowserRegionCoverage(
                            scanned_nodes=10, omitted_regions=0
                        ),
                        "coverage": (
                            BrowserObservationCoverage(
                                candidate_offset=0, scanned_candidates=300, next_cursor="c" * 32
                            )
                            if expandable
                            else None
                        ),
                    }
                )
            )

    clock, factory = await _factory()
    store = FilesystemArtifactStore(tmp_path)
    writers = ArtifactWriterFactory(factory, store, clock, SequenceIdFactory())
    provider = LargePage()
    tool = BrowserObserveTool(provider)
    registry = StaticToolRegistry()
    registry.register(tool)
    pipeline = ToolPipeline(registry, factory, clock, SequenceIdFactory(), artifact_writers=writers)
    active = run()
    async with factory() as uow:
        await uow.runs.create(active)
    cp = _checkpoint(active, [])
    call = ToolCallItem(
        call_id="observe", item_index=0, name=tool.spec.name, arguments={}, raw_arguments="{}"
    )
    kwargs: dict[str, Any] = {
        "run": active,
        "checkpoint": cp,
        "tool_calls": [call],
        "principal": principal(),
        "step": Step(run_id=active.id, step_number=1, started_at=NOW),
        "agent": agent().model_copy(update={"enabled_tools": [tool.spec.name]}),
        "token": RunCancellationToken(clock, None),
    }
    first = await pipeline.dispatch(**kwargs)
    replay = await pipeline.dispatch(**kwargs)
    assert replay == first and provider.observation_count == 1
    async with factory() as uow:
        events = await uow.events.list_after(active.session_id, 0, principal())
    completed = next(e for e in events if e.event_type == "tool.call.completed")
    restored = conversation_items(completed)[0]
    assert isinstance(restored, ToolResultItem)
    assert restored.context_content == first[0].context_content
    assert restored.trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert restored.context_content is not None
    reference = next(p for p in restored.context_content if isinstance(p, FileReferencePart))
    async with factory() as uow:
        saved = await uow.artifacts.get(reference.artifact_id, principal())
    from agent_core.domain.artifacts import StoredArtifactRef

    stored_ref = StoredArtifactRef(
        artifact_id=saved.id,
        sha256=saved.sha256,
        size_bytes=saved.size_bytes,
        media_type=saved.media_type,
    )
    assert b"".join(
        [chunk async for chunk in store.open(stored_ref, tenant_id=principal().tenant_id)]
    ) == content_bytes(restored.content)
    cp.conversation.extend([call, restored])
    planner, builder = await _stack()
    model = ResolvedModel(
        provider="openai",
        model="gpt-6-astra",
        resolved_at=NOW,
        limits=ModelLimits(context_window_tokens=272_000),
    )
    await planner.plan(session(), agent(), principal(), model)
    request = await builder.build(active, cp, agent(), principal())
    rendered = next(item for item in request.conversation if isinstance(item, ToolResultItem))
    rendered_text = next(p.text for p in rendered.content if isinstance(p, TextPart))
    projection_text = next(p.text for p in restored.context_content if isinstance(p, TextPart))
    assert rendered_text.startswith('<untrusted trust="external_untrusted"')
    assert projection_text in rendered_text
    projected = json.loads(projection_text)
    assert projected["revision"] == "revision-1"
    canonical = json.loads(next(p.text for p in restored.content if isinstance(p, TextPart)))
    assert BrowserTextCoverage.model_validate(projected["provider_text_coverage"]) == (
        BrowserTextCoverage.model_validate(canonical["text_coverage"])
    )
    assert projected["provider_text_coverage"]["text_limit_reached"]
    assert projected["regions"][0]["text"] == "Verified fixture status"
    assert projected["regions"][0]["ref"] == "revision-1:region:0"
    assert projected["elements"][0]["ref"] == "element-1"
    if extracted:
        assert projected["extraction"] == extraction.model_dump(mode="json")
        assert "Verified row" in rendered_text
    if expandable:
        assert projected["next_observe"] == {"cursor": "c" * 32}
    wire = OpenAIResponsesProvider._request_payload(request, model)
    assert "External page. " * 1000 not in json.dumps(wire)
    assert restored.content == first[0].content


@pytest.mark.parametrize("budget", [1024, 4096])
def test_expansion_follows_the_last_control_the_model_actually_received(budget: int) -> None:
    structured = {
        "url": "https://example.org/lesson",
        "revision": "current-revision",
        "text": "Choose an answer. " * 1000,
        "elements": [
            {"ref": f"current-ref-{index}", "role": "button", "name": f"Choice {index}"}
            for index in range(256)
        ],
        "coverage": {
            "candidate_offset": 0,
            "scanned_candidates": 300,
            "next_cursor": "a" * 32,
        },
    }
    result = browser_context_projection(
        structured,
        budget=budget,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert result is not None and isinstance(result[0], TextPart)
    payload = json.loads(result[0].text)
    assert 0 < len(payload["elements"]) < 256
    assert payload["next_observe"] == {"after": payload["elements"][-1]["ref"]}
    assert len(content_bytes(result)) <= budget


def test_projection_never_advances_past_an_oversized_missing_control() -> None:
    observation = BrowserObservation(
        url="https://example.org",
        revision="current",
        elements=(
            BrowserElement(ref="large", role="button", name="雪" * 1024),
            BrowserElement(ref="small", role="button", name="Continue"),
        ),
        coverage=BrowserObservationCoverage(
            candidate_offset=0, scanned_candidates=300, next_cursor="c" * 32
        ),
    )
    result = browser_context_projection(
        observation.model_dump(),
        budget=1024,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert result is not None and isinstance(result[0], TextPart)
    payload = json.loads(result[0].text)
    assert payload["elements"][0]["ref"] == "small"
    assert "next_observe" not in payload
    assert payload["coverage"]["expansion_blocked"] == "inline_control_too_large"


@pytest.mark.parametrize("budget", [512, 1024, 4096])
def test_small_projection_preserves_unmet_condition_or_omits_the_whole_observation(
    budget: int,
) -> None:
    from agent_core.domain.browser import BrowserConditionResult

    observation = BrowserObservation(
        url="https://example.org",
        revision="current-revision",
        text="Large page " * 10_000,
        readiness="bound_expired",
        condition=BrowserConditionResult(status="not_observed", observations=4, elapsed_ms=1000),
        elements=tuple(
            BrowserElement(ref=f"e-{i}", role="button", name=f"Choice {i}") for i in range(200)
        ),
    )
    projected = browser_context_projection(
        observation.model_dump(),
        budget=budget,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert projected is not None and isinstance(projected[0], TextPart)
    assert len(content_bytes(projected)) <= budget
    payload = json.loads(projected[0].text)
    if not payload.get("observation_omitted"):
        assert payload["condition"]["status"] == "not_observed"
        assert payload["readiness"] == "bound_expired"
        assert payload["revision"] == observation.revision
    else:
        assert not payload["elements"]
