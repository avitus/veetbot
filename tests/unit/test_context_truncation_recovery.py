"""Truncated tool results must not send the model back to repeat its calls.

context-engine.md ("Yield order under pressure", item 3) and ADR-0258. A
deferred tool's index entry shows one sentence, so the answer that carries its
definition stays whole while older results truncate; and while results are
truncated, a platform note says that no tool reads them back and names the
events a working-state fact can cite. The Cockpit research run of 2026-10-26
lost its results to truncation from step 8, was told nothing, and fetched the
same pages again in three cycles until the identical-call guard stopped it.
"""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.context.builder import BudgetedContextBuilder
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.context import ContextPlan
from agent_core.domain.messages import (
    ConversationItem,
    FileReferencePart,
    ModelRequest,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.domain.tools import (
    ToolOutcome,
    ToolOutcomeStatus,
    ToolSpec,
    deferred_definition_text,
)
from agent_core.tools.cockpit import CockpitReadTool
from agent_core.tools.context_update import UpdateWorkingStateTool
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_fetch_json import WebFetchJsonTool
from tests.contract.support import NOW, agent, principal, run, session
from tests.gates.test_context_m7 import _budget, _PlanStore, _working_state

DEFERRED = CockpitReadTool.spec
# As in the block tests: each page estimates to about 371 tokens, ten fit the
# 4,000-token budget, and an eleventh starts truncation.
TOOL_RESULT_BUDGET = 4_000
PAGE_BYTES = 900


def _builder(
    *,
    working_state: bool = True,
    deferred: tuple[ToolSpec, ...] = (DEFERRED,),
    tool_result_budget: int = TOOL_RESULT_BUDGET,
) -> BudgetedContextBuilder:
    tools = (
        (UpdateWorkingStateTool.spec, WebFetchTool.spec) if working_state else (WebFetchTool.spec,)
    )
    configured_agent = agent()
    estimator = ConservativeTokenEstimator()
    prefix = build_prefix(configured_agent, tools, deferred_tools=deferred)
    plan = ContextPlan(
        session_id=session().id,
        epoch=1,
        prefix_sha256=hashlib.sha256(prefix_bytes(prefix, tools, deferred)).hexdigest(),
        prefix_tokens=estimator.estimate(prefix, "fake:scripted"),
        model_id="fake:scripted",
        tool_names=tuple(spec.name for spec in tools),
        tool_specs=tools,
        tool_schema_sha256=hashlib.sha256(b"[]").hexdigest(),
        policy_version="adr-0256-test",
        builder_version="context-builder@14",
        budget=_budget(tool_results=tool_result_budget),
        created_at=NOW,
        deferred_tool_names=tuple(spec.name for spec in deferred),
        deferred_tool_specs=deferred,
    )
    return BudgetedContextBuilder(
        _PlanStore(plan), estimator, FixedClock(NOW), _working_state(), None, None
    )


class _Conversation:
    def __init__(self) -> None:
        self.items: list[ConversationItem] = [
            UserMessage(
                content=[TextPart(text="Which branches are ahead?")], source_event_sequence=1
            )
        ]
        self.sequence = 1

    def _exchange(self, call: ToolCallItem, result: ToolResultItem) -> str:
        self.items.append(call.model_copy(update={"source_event_sequence": self.sequence + 1}))
        self.items.append(result.model_copy(update={"source_event_sequence": self.sequence + 2}))
        self.sequence += 2
        return call.call_id

    def page(self, index: int) -> str:
        call_id = f"page-{index:02d}"
        return self._exchange(
            ToolCallItem(
                call_id=call_id,
                item_index=0,
                name="web.fetch",
                arguments={"url": f"https://example.org/{index}"},
                raw_arguments="{}",
            ),
            ToolResultItem(
                call_id=call_id,
                content=[TextPart(text=f"page {index:02d} " + "x" * PAGE_BYTES)],
                trust=TrustLevel.EXTERNAL_UNTRUSTED,
            ),
        )

    def export(self, artifact_id: UUID) -> str:
        """A result whose content an artifact holds, as an export returns it."""

        call_id = f"export-{artifact_id.hex[:8]}"
        return self._exchange(
            ToolCallItem(
                call_id=call_id,
                item_index=0,
                name="web.fetch",
                arguments={"url": "https://example.org/export"},
                raw_arguments="{}",
            ),
            ToolResultItem(
                call_id=call_id,
                content=[
                    TextPart(text="export " + "x" * PAGE_BYTES),
                    FileReferencePart(artifact_id=artifact_id, media_type="text/csv"),
                ],
                trust=TrustLevel.EXTERNAL_UNTRUSTED,
            ),
        )

    def probe(
        self,
        call_id: str,
        *,
        answer: str | None = None,
        name: str = "tool.call",
        spec: ToolSpec = DEFERRED,
    ) -> str:
        """The model's empty-argument call and the refusal that answers it."""

        outcome = ToolOutcome(
            status=ToolOutcomeStatus.FAILED,
            action=spec.name,
            reason_code="tool.arguments_invalid",
            message="Not performed. The arguments are invalid.",
            retryable=False,
            remediation="modify_arguments",
        )
        arguments = {"name": spec.name, "arguments": {}} if name == "tool.call" else {}
        return self._exchange(
            ToolCallItem(
                call_id=call_id,
                item_index=0,
                name=name,
                arguments=arguments,
                raw_arguments="{}",
            ),
            ToolResultItem(
                call_id=call_id,
                content=[
                    TextPart(text=outcome.model_dump_json()),
                    TextPart(text=deferred_definition_text(spec) if answer is None else answer),
                ],
                is_error=True,
            ),
        )


async def _request(
    conversation: _Conversation,
    *,
    working_state: bool = True,
    seed: int = 0,
    summarized_through: int = 0,
    deferred: tuple[ToolSpec, ...] = (DEFERRED,),
    tool_result_budget: int = TOOL_RESULT_BUDGET,
) -> ModelRequest:
    current = run(status=RunStatus.RUNNING).model_copy(update={"seed_event_sequence": seed})
    checkpoint = RunCheckpoint(
        run_id=current.id,
        version=1,
        status=RunStatus.RUNNING,
        conversation=conversation.items,
        compacted_summary="Summary of the earlier pages." if summarized_through else None,
        replaced_through_sequence=summarized_through,
        created_at=NOW,
    )
    builder = _builder(
        working_state=working_state, deferred=deferred, tool_result_budget=tool_result_budget
    )
    return await builder.build(current, checkpoint, agent(), principal())


def _results(request: ModelRequest) -> dict[str, str]:
    """Each result's rendered text, inside its trust envelope."""

    return {
        item.call_id: "\n".join(part.text for part in item.content if isinstance(part, TextPart))
        for item in request.conversation
        if isinstance(item, ToolResultItem)
    }


def _sequences(request: ModelRequest) -> dict[str, int | None]:
    return {
        item.call_id: item.source_event_sequence
        for item in request.conversation
        if isinstance(item, ToolResultItem)
    }


def _truncated(request: ModelRequest) -> set[str]:
    return {
        call_id for call_id, text in _results(request).items() if "[tool result truncated:" in text
    }


def _notes(request: ModelRequest) -> list[str]:
    return [
        part.text
        for item in request.conversation
        if isinstance(item, UserMessage) and item.trust is TrustLevel.PLATFORM
        for part in item.content
        if isinstance(part, TextPart) and part.text.startswith("Tool-result note")
    ]


async def test_the_newest_definition_answer_survives_truncation() -> None:
    conversation = _Conversation()
    stale = conversation.probe("probe-old")
    pages = [conversation.page(index) for index in range(6)]
    current = conversation.probe("probe-new")
    pages += [conversation.page(index) for index in range(6, 12)]

    request = await _request(conversation)

    truncated = _truncated(request)
    # The oldest results truncate as before, including the stale copy of the
    # definition; the newest copy is kept whole although it is older than most.
    assert stale in truncated
    assert current not in truncated
    assert set(pages[:2]) <= truncated
    assert deferred_definition_text(DEFERRED) in _results(request)[current]


async def test_kept_definition_answers_never_exceed_the_budget() -> None:
    # Each deferred tool keeps its newest definition answer, but together they
    # stay within the tool-result budget: past it the oldest truncate as any
    # other result would, so probing many tools cannot crowd out the rest.
    conversation = _Conversation()
    older = conversation.probe("probe-cockpit")
    newer = conversation.probe("probe-json", spec=WebFetchJsonTool.spec)
    estimator = ConservativeTokenEstimator()
    sizes = {
        item.call_id: estimator.estimate([item], "fake:scripted")
        for item in conversation.items
        if isinstance(item, ToolResultItem)
    }

    request = await _request(
        conversation,
        deferred=(DEFERRED, WebFetchJsonTool.spec),
        tool_result_budget=sizes[newer] + sizes[older] // 2,
    )

    assert _truncated(request) == {older}
    assert deferred_definition_text(WebFetchJsonTool.spec) in _results(request)[newer]


@pytest.mark.parametrize(
    ("name", "answer"),
    [
        # A tool's own output that copies the definition is not a definition answer.
        ("web.fetch", None),
        # The schema-only refusal of the earlier contract carries no description.
        ("tool.call", "Input schema: {}"),
    ],
    ids=["not-through-tool-call", "not-the-pinned-definition"],
)
async def test_only_the_pinned_definition_answer_is_kept(name: str, answer: str | None) -> None:
    conversation = _Conversation()
    probe = conversation.probe("probe", answer=answer, name=name)
    for index in range(12):
        conversation.page(index)

    assert probe in _truncated(await _request(conversation))


async def test_an_artifact_pointer_also_names_the_recording_event() -> None:
    conversation = _Conversation()
    artifact_id = UUID("6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f")
    exported = conversation.export(artifact_id)
    sequence = conversation.sequence
    for index in range(12):
        conversation.page(index)

    request = await _request(conversation)

    assert exported in _truncated(request)
    # A fact cites its result's event, so the pointer names it beside the artifact.
    pointer = f"full content at artifact:{artifact_id}; event:{sequence}]"
    assert pointer in _results(request)[exported]


async def test_no_note_before_anything_is_truncated() -> None:
    conversation = _Conversation()
    for index in range(10):
        conversation.page(index)

    request = await _request(conversation)

    assert _truncated(request) == set()
    assert _notes(request) == []


async def test_truncation_note_names_the_events_a_fact_can_cite() -> None:
    conversation = _Conversation()
    for index in range(14):
        conversation.page(index)

    request = await _request(conversation)

    [note] = _notes(request)
    shown = [
        sequence
        for call_id, sequence in _sequences(request).items()
        if call_id not in _truncated(request)
    ]
    assert _truncated(request)
    assert "no tool reads a truncated result back" in note
    assert "context.update_working_state" in note
    listed = "; ".join(f"event {sequence} (web.fetch)" for sequence in shown)
    assert note.endswith(f"Results still shown, newest last: {listed}.")


async def test_truncation_note_without_working_state_only_says_what_happened() -> None:
    conversation = _Conversation()
    for index in range(14):
        conversation.page(index)

    [note] = _notes(await _request(conversation, working_state=False))

    assert "no tool reads a truncated result back" in note
    assert "context.update_working_state" not in note
    assert "event " not in note


async def test_truncation_note_names_only_results_the_history_cut_kept() -> None:
    conversation = _Conversation()
    for index in range(12):
        conversation.page(index)
    # A compaction summary replaces these exchanges, so the history cut takes
    # them although some are among the newest results still whole.
    summarized = conversation.sequence
    for index in range(12, 14):
        conversation.page(index)
    conversation.sequence += 1
    conversation.items.append(
        UserMessage(
            content=[TextPart(text="And the rest?")], source_event_sequence=conversation.sequence
        )
    )
    seed = conversation.sequence
    for index in range(14, 16):
        conversation.page(index)

    request = await _request(conversation, seed=seed, summarized_through=summarized)

    sent = _sequences(request)
    assert len(sent) == 4, "the fixture must actually cut history"
    assert _truncated(request) == set()
    [note] = _notes(request)
    listed = [int(value) for value in re.findall(r"event (\d+) \(web\.fetch\)", note)]
    assert listed == list(sent.values())
