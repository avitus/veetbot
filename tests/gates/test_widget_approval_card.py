"""The widget approval card, through the real pipeline: all of the change, or nothing published."""

import asyncio
from pathlib import Path
from typing import Any

from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from tests.gates.test_deferred_tools_adr0123 import _production, _session_events, _tool_call

_ENVIRONMENT = {"AGENT_COCKPIT_ENABLED": "1"}


def _turns(files: dict[str, str]) -> list[ScriptedTurn]:
    """Publish and read a feed, which taints the turn so the write asks first, then write."""
    calls = (
        _tool_call(
            "cockpit.feed.publish",
            {
                "feed": "agent.probe",
                "data": {"n": 1},
                "expected_interval_seconds": 60,
                "schema": {"type": "object"},
            },
        ),
        _tool_call("cockpit.read", {"operation": "read_feed", "feed": "agent.probe"}),
        _tool_call(
            "cockpit.widget.write", {"operation": "create", "slug": "wide-card", "files": files}
        ),
    )
    return [
        *(ScriptedTurn(tool_calls=[call], stop_reason=StopReason.TOOL_USE) for call in calls),
        ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
    ]


async def _until(app: Any, run_id: Any, status: RunStatus) -> None:
    """Wait for the run to reach a status; a run that fails first fails the test."""
    while True:
        run = await app.runs.get(run_id)
        if run.status is status:
            return
        assert run.status is not RunStatus.FAILED, run
        await asyncio.sleep(0.05)


async def _card(tmp_path: Path, files: dict[str, str]) -> Any:
    """The approval the owner is shown for a tainted widget write."""
    _factory, context = await _production(tmp_path, _turns(files), extra_environment=_ENVIRONMENT)
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Make the widget.", session_id)
        await asyncio.wait_for(_until(app, run_id, RunStatus.WAITING_FOR_APPROVAL), timeout=30)
        [approval] = await app.approvals.list_pending(run_id=run_id)
    return approval


def _entries(approval: Any) -> list[str]:
    """Every diff entry on the card, across its pages."""
    diff = approval.arguments["diff"]
    return [line for page in diff.values() for line in page]


async def test_a_change_past_fifty_lines_reaches_the_owner_whole(tmp_path: Path) -> None:
    source = [f"export const line{number} = {number};" for number in range(120)]
    approval = await _card(tmp_path, {"widget.tsx": "\n".join(source)})

    entries = _entries(approval)
    assert not any("TRUNCATED" in entry for entry in entries)
    assert [entry.removeprefix("+") for entry in entries if entry.startswith("+export")] == source


async def test_a_long_line_reaches_the_owner_whole(tmp_path: Path) -> None:
    long_line = "export const x = '" + "x" * 3000 + "tail';"
    approval = await _card(tmp_path, {"widget.tsx": long_line})

    entries = _entries(approval)
    pieces = [entry for entry in entries if "xxxx" in entry]
    assert not any("TRUNCATED" in entry for entry in entries)
    assert pieces[0] + "".join(piece.removeprefix("> ") for piece in pieces[1:]) == f"+{long_line}"


async def test_approving_a_change_too_large_to_show_publishes_nothing(tmp_path: Path) -> None:
    source = "\n".join(f"export const line{number} = {number};" for number in range(500))
    _factory, context = await _production(
        tmp_path, _turns({"widget.tsx": source}), extra_environment=_ENVIRONMENT
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Make the widget.", session_id)
        await asyncio.wait_for(_until(app, run_id, RunStatus.WAITING_FOR_APPROVAL), timeout=30)
        [approval] = await app.approvals.list_pending(run_id=run_id)

        assert approval.arguments["diff_status"] == "too_large"
        assert "diff" not in approval.arguments
        assert "publishes nothing" in approval.action_summary

        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        await asyncio.wait_for(_until(app, run_id, RunStatus.COMPLETED), timeout=30)
        events = await _session_events(app, session_id)

    [failed] = [
        event.payload
        for event in events
        if event.event_type == "tool.call.failed"
        and event.payload["name"] == "cockpit.widget.write"
    ]
    assert failed["reason_code"] == "cockpit.change_too_large"
