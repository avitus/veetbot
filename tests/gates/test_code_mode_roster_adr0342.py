"""Code mode in Chat: the roster, the instructions and the evaluation case (ADR-0342).

docs/plan/tool-system.md, "Code mode", Chat. With the setting on, the script
tool takes the command tool's place in Chat's tools and nothing else moves:
no definition, index entry or cap changes. Chat's instructions gain one
paragraph. Off,
the roster and the instructions are exactly what they were. The two-list case
runs a real script through the pipeline and proves one model step does the
work of five.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from agent_core.bootstrap import (
    CODE_MODE_AGENT_INSTRUCTIONS,
    DEFAULT_AGENT_INSTRUCTIONS,
    build,
)
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.context import ContextPlan
from agent_core.domain.execution import SANDBOX_RUN_CODE_TOOL_NAME
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from agent_core.evals.cases import EvalCase, load_cases
from agent_core.evals.fixtures import resolve_model_fixture
from agent_core.evals.runner import run_case
from tests.gates.test_deferred_tools_adr0123 import (
    PLAN_CONFIG,
    WEBSITE_AND_LATER_FLAGS,
    _plan_for_one_turn,
    _production,
)
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_reply_attachments import _settings

ROOT = Path(__file__).resolve().parents[2]
CODE_MODE = {"AGENT_CODE_MODE_ENABLED": "1"}
COMMAND = "sandbox.run_command"
CASE = "code_mode_two_list_intersection"


async def _bound_plan(tmp_path: Path, extra: dict[str, str]) -> ContextPlan:
    """The fullest production chat: every flag on and a website profile bound."""
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment={**WEBSITE_AND_LATER_FLAGS, **extra},
    )
    async with context as app:
        await seed_browser_authority(app)
        created = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("What can you do for me?", created.id)
        terminal = await app.runs.wait_terminal(run_id)
        plan = await app.executor._context_planner.current(created.id)
    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    assert plan is not None
    return plan


def _swapped(names: tuple[str, ...]) -> set[str]:
    return (set(names) - {COMMAND}) | {SANDBOX_RUN_CODE_TOOL_NAME}


# --- the roster ---------------------------------------------------------------


async def test_the_script_tool_takes_the_command_tools_place_and_nothing_else_moves(
    tmp_path: Path,
) -> None:
    off = await _plan_for_one_turn(tmp_path / "off")
    on = await _plan_for_one_turn(tmp_path / "on", extra_environment=CODE_MODE)

    assert COMMAND in off.tool_names
    assert SANDBOX_RUN_CODE_TOOL_NAME not in {*off.tool_names, *off.deferred_tool_names}
    assert set(on.tool_names) == _swapped(off.tool_names)
    assert on.deferred_tool_names == off.deferred_tool_names
    assert on.skipped_tool_names == ()


async def test_the_fullest_production_chat_keeps_its_definitions_and_its_index(
    tmp_path: Path,
) -> None:
    off = await _bound_plan(tmp_path / "off", {})
    on = await _bound_plan(tmp_path / "on", CODE_MODE)

    assert set(on.tool_names) == _swapped(off.tool_names)
    # The index is untouched, so ADR-0267's spare entries are too.
    assert on.deferred_tool_names == off.deferred_tool_names
    assert on.skipped_tool_names == ()
    definitions = yaml.safe_load(PLAN_CONFIG.read_text(encoding="utf-8"))["classes"][
        "tool_definitions"
    ]
    estimator = ConservativeTokenEstimator()
    assert estimator.estimate_tools(list(on.tool_specs), on.model_id) <= int(
        definitions["max_tokens"]
    )


# --- the instructions -----------------------------------------------------------


async def _instructions(tmp_path: Path, *, code_mode: bool, **overrides: Any) -> str:
    """The instructions of the agent a new chat runs under."""
    settings = replace(_settings(tmp_path), code_mode_enabled=code_mode)
    script = FakeModelScript(turns=[ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)])
    async with build(settings=settings, script=script, **overrides) as app:
        run = await app.runs.get(await app.runs.submit("Hello."))
        async with app.uow_factory() as uow:
            agent = await uow.agents.get_version(run.agent_id, run.agent_version)
    assert run.status is RunStatus.COMPLETED, run.failure
    return agent.instructions


async def test_chat_is_told_when_to_write_a_script_only_while_it_has_the_tool(
    tmp_path: Path,
) -> None:
    on = await _instructions(tmp_path / "on", code_mode=True)
    without_the_tool = await _instructions(
        tmp_path / "narrow", code_mode=True, enabled_tools=["math.calculate"]
    )
    off = await _instructions(tmp_path / "off", code_mode=False)

    assert on == DEFAULT_AGENT_INSTRUCTIONS + CODE_MODE_AGENT_INSTRUCTIONS
    assert without_the_tool == DEFAULT_AGENT_INSTRUCTIONS
    assert off == DEFAULT_AGENT_INSTRUCTIONS


def test_the_paragraph_names_the_tool_its_uses_and_what_stays_a_direct_call() -> None:
    text = CODE_MODE_AGENT_INSTRUCTIONS
    assert text.startswith(" ")
    for phrase in (
        SANDBOX_RUN_CODE_TOOL_NAME,
        "loop",
        "paging",
        "two lists",
        "tools.<name>",
        # The command tool leaves Chat's tools, and the base instructions name it.
        COMMAND,
    ):
        assert phrase in text
    # A change inside a script holds the sandbox for its approval, so Chat is
    # told to make changes as direct calls.
    assert "direct" in text
    assert ConservativeTokenEstimator().estimate_text(text, "m") <= 240


# --- the evaluation case --------------------------------------------------------


def _case() -> EvalCase:
    matches = [case for case in load_cases(ROOT / "tests/eval_cases") if case.name == CASE]
    assert matches, f"the {CASE} evaluation case exists"
    return matches[0]


async def test_one_script_pages_two_lists_and_only_its_answer_reaches_the_model() -> None:
    case = _case()
    result = await run_case(case, ROOT / "evals/fixtures/models")

    assert result.run.status is RunStatus.COMPLETED, result.run.failure
    started = [
        event.payload["name"] for event in result.events if event.event_type == "tool.call.started"
    ]
    # One script, then two pages of each list over the bridge.
    assert started == [
        SANDBOX_RUN_CODE_TOOL_NAME,
        "mcp.lists.renewals",
        "mcp.lists.renewals",
        "mcp.lists.overdue",
        "mcp.lists.overdue",
    ]
    assert result.run.tool_call_count == 5
    # Two model requests ran. The script's step was given back.
    assert result.run.usage.model_calls == 2
    assert (result.run.step_count, result.run.model_call_count) == (1, 1)
    # Nothing asked the owner: the script was written for the owner's request
    # before the turn read outside content, and every bridged call is a read.
    assert not [event for event in result.events if event.event_type == "approval.requested"]
    # The pages are recorded as bridged results and never enter the
    # conversation. The one result the model receives is what the script printed.
    results = {
        event.payload["call_id"]: event.payload["result_item"]["content"][0]["text"]
        for event in result.events
        if event.event_type == "tool.call.completed"
    }
    assert results.pop("call_script") == "Acme, Globex\n"
    assert len(results) == 4
    assert all(call_id.startswith("bridge:") for call_id in results)
    assert len(resolve_model_fixture(ROOT / "evals/fixtures/models", case.model_fixture).turns) == 2
