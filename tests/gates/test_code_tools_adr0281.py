"""ADR-0281 through the real pipeline: the tools work, and a proposal stops at the owner."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_core.bootstrap import DEFAULT_AGENT_INSTRUCTIONS
from agent_core.config import ConfigurationError, load_settings
from agent_core.context.rendering import UNINDEXED_DEFERRED_TOOLS, deferred_index_items
from agent_core.domain.code import (
    CODE_TOOL_NAMES,
    CODE_WRITE_TOOL_NAMES,
    RUN_INSPECT_TOOL_NAME,
    CodeDraftState,
    content_digest,
)
from agent_core.domain.messages import ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from tests.gates.test_deferred_tools_adr0123 import (
    _plan_for_one_turn,
    _production,
    _session_events,
    _tool_call,
)
from tests.gates.test_email_m18 import _base_environment


def _environment(root: Path) -> dict[str, str]:
    (root / "src").mkdir(exist_ok=True)
    (root / "src/app.py").write_text("limit = 1\n")
    (root / ".release.env").write_text("SECRET=canary\n")
    return {
        "AGENT_COCKPIT_ENABLED": "1",
        "AGENT_CODE_TOOLS_ENABLED": "1",
        "AGENT_CODE_PROPOSALS_ENABLED": "1",
        "AGENT_CODE_SOURCE_ROOT": str(root),
        "VEETBOT_RELEASE_ID": "20261101-120000-abc1234",
    }


def _turns(*calls: Any) -> list[ScriptedTurn]:
    """One scripted turn per tool call, then the closing text."""
    return [
        *(ScriptedTurn(tool_calls=[call], stop_reason=StopReason.TOOL_USE) for call in calls),
        ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
    ]


async def _results(app: Any, session_id: Any) -> dict[str, list[dict[str, Any]]]:
    """Each tool's completed and failed payloads, by tool name."""
    results: dict[str, list[dict[str, Any]]] = {}
    for event in await _session_events(app, session_id):
        if event.event_type in {"tool.call.completed", "tool.call.failed"}:
            results.setdefault(event.payload["name"], []).append(event.payload)
    return results


async def test_the_tools_are_reached_by_name_and_take_no_index_entry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plan = await _plan_for_one_turn(tmp_path, extra_environment=_environment(source))

    assert {RUN_INSPECT_TOOL_NAME, *CODE_TOOL_NAMES} <= set(plan.deferred_tool_names)
    assert {RUN_INSPECT_TOOL_NAME, *CODE_TOOL_NAMES} <= UNINDEXED_DEFERRED_TOOLS
    index = str(deferred_index_items(plan.deferred_tool_specs))
    assert "code.read" not in index and "run.inspect" not in index
    assert "call run.inspect through tool.call" in DEFAULT_AGENT_INSTRUCTIONS
    assert "code.propose" in DEFAULT_AGENT_INSTRUCTIONS


async def test_without_the_flag_only_run_inspect_is_offered(tmp_path: Path) -> None:
    plan = await _plan_for_one_turn(tmp_path)
    assert RUN_INSPECT_TOOL_NAME in plan.deferred_tool_names
    assert not set(CODE_TOOL_NAMES) & set(plan.deferred_tool_names)


async def test_code_read_serves_source_and_refuses_what_sits_beside_it(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _factory, context = await _production(
        tmp_path,
        _turns(
            _tool_call("code.read", {"operation": "read", "path": "src/app.py"}),
            _tool_call("code.read", {"operation": "read", "path": ".release.env"}),
        ),
        extra_environment=_environment(source),
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Read the code.", session_id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        results = await _results(app, session_id)
    assert terminal.status is RunStatus.COMPLETED
    served, refused = results["code.read"]
    assert served["reason_code"] == "tool.succeeded"
    assert "limit = 1" in json.dumps(served["result_item"])
    assert "code.path_refused" in json.dumps(refused)
    assert "canary" not in json.dumps(results)


async def test_a_proposal_waits_for_the_owner_and_shows_the_diff(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _factory, context = await _production(
        tmp_path,
        _turns(
            _tool_call("code.draft", {"operation": "open", "slug": "fix-limit"}),
            _tool_call(
                "code.draft",
                {
                    "operation": "edit",
                    "slug": "fix-limit",
                    "path": "src/app.py",
                    "old": "limit = 1",
                    "new": "limit = 2",
                },
            ),
            _tool_call(
                "code.propose",
                {
                    "slug": "fix-limit",
                    "title": "Raise the limit",
                    "summary": "Why it matters.",
                    "digest": content_digest(
                        "20261101-120000-abc1234", "abc1234", {"src/app.py": "limit = 2\n"}
                    ),
                },
            ),
        ),
        extra_environment=_environment(source),
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Fix the limit and open a PR.", session_id)
        await asyncio.wait_for(_until_waiting(app, run_id), timeout=30)
        approvals = await app.approvals.list_pending()
        async with app.uow_factory() as uow:
            draft = await uow.code_drafts.get(app.principal, "fix-limit")

    assert len(approvals) == 1
    view = approvals[0]
    assert "public repository" in view.action_summary and "cannot merge" in view.action_summary
    assert "Raise the limit" not in view.action_summary
    assert "+limit = 2" in [entry for page in view.arguments["diff"].values() for entry in page]
    # Asking published nothing: the draft stays private until the owner decides.
    assert draft.state is CodeDraftState.OPEN
    assert draft.proposal is None


async def test_a_long_line_reaches_the_owner_whole_through_the_pipeline(tmp_path: Path) -> None:
    """Whatever the approval path stores is what the owner reads, so nothing may clip it."""
    source = tmp_path / "source"
    source.mkdir()
    long_line = "limit = '" + "x" * 3000 + "tail'"
    _factory, context = await _production(
        tmp_path,
        _turns(
            _tool_call("code.draft", {"operation": "open", "slug": "fix-limit"}),
            _tool_call(
                "code.draft",
                {
                    "operation": "edit",
                    "slug": "fix-limit",
                    "path": "src/app.py",
                    "old": "limit = 1",
                    "new": long_line,
                },
            ),
            _tool_call(
                "code.propose",
                {
                    "slug": "fix-limit",
                    "title": "Raise the limit",
                    "summary": "Why it matters.",
                    "digest": content_digest(
                        "20261101-120000-abc1234", "abc1234", {"src/app.py": f"{long_line}\n"}
                    ),
                },
            ),
        ),
        extra_environment=_environment(source),
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Fix the limit and open a PR.", session_id)
        await asyncio.wait_for(_until_waiting(app, run_id), timeout=30)
        approvals = await app.approvals.list_pending()

    entries = [entry for page in approvals[0].arguments["diff"].values() for entry in page]
    pieces = [entry for entry in entries if "xxxx" in entry]
    assert not any("TRUNCATED" in entry for entry in entries)
    assert pieces[0] + "".join(piece.removeprefix("> ") for piece in pieces[1:]) == f"+{long_line}"


async def _until_waiting(app: Any, run_id: Any) -> None:
    """Wait for the run to reach its approval; finishing first fails the test."""
    while True:
        run = await app.runs.get(run_id)
        if run.status is RunStatus.WAITING_FOR_APPROVAL:
            return
        assert run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}, run
        await asyncio.sleep(0.05)


def test_scheduled_and_delegated_work_cannot_change_code() -> None:
    from agent_core.application.delegations import FORBIDDEN_CHILD_TOOLS

    assert set(CODE_WRITE_TOOL_NAMES) <= FORBIDDEN_CHILD_TOOLS


async def test_a_session_outside_owner_chat_is_not_offered_code_writes(tmp_path: Path) -> None:
    from agent_core.bootstrap import _session_tool_filter
    from agent_core.domain.sessions import Session
    from agent_core.domain.tools import ToolSpec

    source = tmp_path / "source"
    source.mkdir()
    plan = await _plan_for_one_turn(tmp_path, extra_environment=_environment(source))
    specs: list[ToolSpec] = list(plan.deferred_tool_specs)
    chat = Session.model_construct(metadata={})
    scheduled = Session.model_construct(metadata={"schedule_id": "x"})
    keep = _session_tool_filter(None)
    assert set(CODE_WRITE_TOOL_NAMES) <= {tool.name for tool in keep(chat, specs)}
    assert not set(CODE_WRITE_TOOL_NAMES) & {tool.name for tool in keep(scheduled, specs)}


def test_proposals_need_the_tools_and_the_cockpit_key_holder(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ConfigurationError, match="AGENT_CODE_PROPOSALS_ENABLED"):
        load_settings({**_base_environment(), "AGENT_CODE_PROPOSALS_ENABLED": "1"})
    with pytest.raises(ConfigurationError, match="AGENT_CODE_SOURCE_ROOT"):
        load_settings({**_base_environment(), "AGENT_CODE_TOOLS_ENABLED": "1"})
