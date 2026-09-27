"""Repeated research must leave an answer opportunity without relaxing the breaker."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import (
    FakeModelScript,
    ModelTransientError,
    ScriptedToolCall,
    ScriptedTurn,
    TextPart,
    ToolResultItem,
)
from agent_core.domain.runs import FailureReason, RunLimits, RunStatus
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import (
    _calc,
    _control_texts,
    _requests,
    _settings,
    _tool_turn,
)
from tests.unit.test_web_tools import FakeWebProvider

CONTROL = "repeated-tool synthesis"
LIMITS = RunLimits(max_steps=20, max_model_calls=20, max_tool_calls=40)


@pytest.mark.parametrize("threshold", [2, 5])
async def test_repetition_gets_a_final_answer_before_the_breaker(
    tmp_path: Path, threshold: int
) -> None:
    overlay = tmp_path / "tools" / "limits.yaml"
    overlay.parent.mkdir()
    overlay.write_text(f"circuit_breaker:\n  identical_call_threshold: {threshold}\n")
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://localhost/runtime",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "dev",
            "SANDBOX_MECHANISM": "fake",
            "AGENT_CONFIG_DIR": str(tmp_path),
            "OPENAI_MODEL": "",
        }
    )
    script = FakeModelScript(
        turns=[
            *(_tool_turn(_calc("1 + 1", f"read-{n}")) for n in range(threshold - 1)),
            ScriptedTurn(
                text="Two. Further verification remains incomplete.", context_contains=CONTROL
            ),
            _tool_turn(_calc("1 + 1", "would-repeat")),
        ]
    )
    async with build(settings=settings, script=script, limits=LIMITS) as composition:
        run_id = await composition.runs.submit("research and report the evidence")
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)
        async with composition.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.final_message == "Two. Further verification remains incomplete."
    assert run.tool_call_count == threshold - 1
    assert run.model_call_count == threshold
    assert all(not _control_texts(request) for request in requests[:-1])
    assert any(CONTROL in text for text in _control_texts(requests[-1]))
    assert requests[-1].model_dump()["tool_choice"] == "none"
    assert requests[-1].tools == requests[0].tools
    assert requests[-1].metadata["prefix_sha256"] == requests[0].metadata["prefix_sha256"]
    assert checkpoint is not None
    assert CONTROL not in checkpoint.model_dump_json()


@pytest.mark.parametrize("expression", ["1 + 1", "2 + 2"])
async def test_tools_returned_during_loop_synthesis_never_dispatch(expression: str) -> None:
    script = FakeModelScript(
        turns=[
            *(_tool_turn(_calc("1 + 1", f"read-{n}")) for n in range(4)),
            _tool_turn(_calc(expression, "ignored-control")),
            ScriptedTurn(text="should not reach this"),
        ]
    )
    async with build(settings=_settings(), script=script, limits=LIMITS) as composition:
        run_id = await composition.runs.submit("research")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)

    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.reason is FailureReason.TOOL_LOOP_DETECTED
    assert run.tool_call_count == 4
    assert [event.event_type for event in events].count("tool.call.proposed") == 4


async def test_repeats_in_batches_also_get_synthesis() -> None:
    script = FakeModelScript(
        turns=[
            _tool_turn(_calc("1 + 1", "a"), _calc("1 + 1", "b")),
            _tool_turn(_calc("1 + 1", "c"), _calc("1 + 1", "d")),
            ScriptedTurn(text="Two.", context_contains=CONTROL),
            _tool_turn(_calc("1 + 1", "e")),
        ]
    )
    async with build(settings=_settings(), script=script, limits=LIMITS) as composition:
        run_id = await composition.runs.submit("research")
        run = await composition.runs.wait_terminal(run_id)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.tool_call_count == 4


async def test_distinct_calls_keep_their_tools() -> None:
    script = FakeModelScript(
        turns=[
            *(_tool_turn(_calc(f"{n} + 1", f"read-{n}")) for n in range(6)),
            ScriptedTurn(text="Done."),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        limits=LIMITS,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("research")
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.tool_call_count == 6
    assert all(not _control_texts(request) for request in requests)
    assert all(request.model_dump().get("tool_choice") is None for request in requests)


async def test_web_research_with_elided_results_still_concludes() -> None:
    """Beads incident shape: interleaved large reads and four reads of one URL."""

    provider = FakeWebProvider(page_content="Repository evidence. " * 1_000)
    turns = []
    for index in range(4):
        turns.append(
            _tool_turn(
                ScriptedToolCall(
                    name="web.fetch", arguments={"url": f"https://example.org/page-{index}"}
                ),
                ScriptedToolCall(
                    name="web.fetch", arguments={"url": "https://example.org/AGENTS.md"}
                ),
            )
        )
    turns.extend(
        [
            ScriptedTurn(
                text="Here are the findings; the audit is incomplete.", context_contains=CONTROL
            ),
            _tool_turn(
                ScriptedToolCall(
                    name="web.fetch", arguments={"url": "https://example.org/AGENTS.md"}
                )
            ),
        ]
    )
    async with build(
        settings=_settings(),
        script=FakeModelScript(turns=turns),
        limits=LIMITS,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Audit this repository and report limitations.")
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert provider.fetches.count("https://example.org/AGENTS.md") == 4
    assert len(provider.fetches) == 8
    assert any(
        "tool result truncated" in part.text
        for item in requests[-1].conversation
        if isinstance(item, ToolResultItem)
        for part in item.content
        if isinstance(part, TextPart)
    ), "the fixture must actually exercise context pressure"
    assert requests[-1].tool_choice == "none"


async def test_synthesis_survives_approval_resume() -> None:
    script = FakeModelScript(
        turns=[
            *(_tool_turn(_calc("1 + 1", f"read-{n}")) for n in range(3)),
            _tool_turn(
                _calc("1 + 1", "fourth-read"),
                ScriptedToolCall(
                    name="demo.external_write",
                    arguments={"destination": "demo", "content": "hello"},
                ),
            ),
            ScriptedTurn(text="Two; the approved action finished.", context_contains=CONTROL),
            _tool_turn(_calc("1 + 1", "would-repeat")),
        ]
    )
    async with build(settings=_settings(), script=script, limits=LIMITS) as composition:
        run_id = await composition.runs.submit("research then perform the action")
        assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert requests[-1].tool_choice == "none"
    assert len(invocations) == 5


async def test_model_retry_cannot_reenable_tools_during_synthesis() -> None:
    script = FakeModelScript(
        turns=[
            *(_tool_turn(_calc("1 + 1", f"read-{n}")) for n in range(4)),
            ScriptedTurn(
                fail_with=ModelTransientError(
                    provider="fake",
                    model="scripted",
                    attempt_id=UUID(int=1),
                    message="temporary provider outage",
                )
            ),
            ScriptedTurn(text="Two.", context_contains=CONTROL),
            _tool_turn(_calc("1 + 1", "would-repeat")),
        ]
    )
    async with build(settings=_settings(), script=script, limits=LIMITS) as composition:
        run_id = await composition.runs.submit("research")
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.tool_call_count == 4
    assert len(requests) == 6
    assert all(request.tool_choice == "none" for request in requests[-2:])
    assert all(len(_control_texts(request)) == 1 for request in requests[-2:])


async def test_synthesis_does_not_bypass_the_model_call_budget() -> None:
    script = FakeModelScript(
        turns=[
            *(_tool_turn(_calc("1 + 1", f"read-{n}")) for n in range(4)),
            ScriptedTurn(text="must not make an unbudgeted call"),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        limits=RunLimits(max_steps=10, max_model_calls=4, max_tool_calls=20),
    ) as composition:
        run_id = await composition.runs.submit("research")
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)

    assert run.status is RunStatus.FAILED
    assert run.failure is not None and run.failure.reason is FailureReason.BUDGET_EXCEEDED
    assert len(requests) == 4
