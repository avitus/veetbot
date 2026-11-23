"""Code mode approval: when a script Veetbot wrote runs without asking (ADR-0342).

A script written in a turn that has read no outside content is Veetbot's own
work on the owner's request and runs without an approval. Once the turn has
read a web page, a message or an earlier script's output, the next script asks.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser_request_grants import RequestGrantEcho
from agent_core.domain.execution import SANDBOX_RUN_CODE_TOOL_NAME
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Run, RunCheckpoint, RunStatus
from agent_core.domain.script_request_grants import SCRIPT_REQUEST_GRANT_AUTHORIZATION_KIND
from agent_core.tools.executor import _argument_trust
from agent_core.tools.registry import RegisteredTool, StaticToolRegistry
from tests.contract.support import NOW
from tests.unit.test_code_mode import _Probe, _spec
from tests.unit.test_code_mode_budget import _RUN_CODE, _Script, _turn
from tests.unit.test_reply_attachments import _settings

_SOURCE = {"code": "print(sum(len(page) for page in pages))"}
# A read whose result is outside content, as a web page or a message is.
_OUTSIDE_READ = _spec("demo.outside", output_trust=TrustLevel.EXTERNAL_UNTRUSTED)


async def _submit(
    tmp_path: Path, turns: list[ScriptedTurn], tools: list[Any]
) -> tuple[Run, int, list[bool]]:
    """Run to the end, granting what is asked.

    Returns the run, how many approvals it asked for, and whether each named
    untrusted input as its reason.
    """
    model = FakeModelScript(
        turns=[*turns, ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN)]
    )
    untrusted: list[bool] = []
    async with build(
        settings=_settings(tmp_path),
        script=model,
        enabled_tools=[tool.spec.name for tool in tools],
    ) as built:
        registry = cast(StaticToolRegistry, built.tool_pipeline._registry)
        for tool in tools:
            registry.register(tool)
        # The owner's message quotes no source, so nothing below is the
        # owner's own words.
        run_id = await built.runs.submit("Work out how long the pages are.")
        for _ in range(4):
            run = await built.runs.get(run_id)
            if run.status is not RunStatus.WAITING_FOR_APPROVAL:
                break
            [approval] = await built.approvals.list_pending(run_id=run_id)
            untrusted.append(approval.policy_decision.untrusted_escalation)
            await built.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await built.runs.get(run_id)
    assert run.status is RunStatus.COMPLETED, run.failure
    return run, len(untrusted), untrusted


# --- through the pipeline -----------------------------------------------------


async def test_a_script_written_for_the_owners_request_runs_without_asking(
    tmp_path: Path,
) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(_RUN_CODE, [["demo.ordinary"] * 2])

    _, asked, _ = await _submit(
        tmp_path, [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))], [script, ordinary]
    )

    assert asked == 0
    assert script.steps == [1]
    assert ordinary.executions == 2


async def test_a_script_written_after_outside_content_asks_first(tmp_path: Path) -> None:
    outside = _Probe(_OUTSIDE_READ)
    script = _Script(_RUN_CODE, [[]])

    _, asked, untrusted = await _submit(
        tmp_path,
        [_turn(("demo.outside", "read")), _turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))],
        [script, outside],
    )

    assert (asked, untrusted) == (1, [True])
    assert script.steps == [2]


async def test_a_script_in_the_same_batch_as_an_outside_read_asks_first(
    tmp_path: Path,
) -> None:
    outside = _Probe(_OUTSIDE_READ)
    script = _Script(_RUN_CODE, [[]])

    _, asked, untrusted = await _submit(
        tmp_path,
        [_turn(("demo.outside", "read"), (SANDBOX_RUN_CODE_TOOL_NAME, "script"))],
        [script, outside],
    )

    assert (asked, untrusted) == (1, [True])


async def test_a_second_script_asks_once_the_first_has_returned_output(
    tmp_path: Path,
) -> None:
    script = _Script(_RUN_CODE, [[], []])

    _, asked, untrusted = await _submit(
        tmp_path,
        [
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "first")),
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "second")),
        ],
        [script],
    )

    # A script's output may carry whatever its tools returned.
    assert (asked, untrusted) == (1, [True])
    assert script.steps == [1, 2]


async def test_another_sandbox_tool_still_asks_for_what_the_model_wrote(
    tmp_path: Path,
) -> None:
    command = _Script(
        _spec(
            "demo.script",
            input_schema=_RUN_CODE.input_schema,
            target_kind="sandbox",
            side_effect=_RUN_CODE.side_effect,
        ),
        [[]],
    )
    turn = _turn(("demo.script", "command"))
    turn.tool_calls[0].arguments = dict(_SOURCE)

    _, asked, untrusted = await _submit(tmp_path, [turn], [command])

    assert (asked, untrusted) == (1, [True])


# --- one approval for the scripts of a request (decision 11) -------------------


async def _three_scripts_after_an_outside_read(
    tmp_path: Path, first: ApprovalResolutionType
) -> tuple[Run, list[bool], list[str]]:
    """Run three scripts after an outside read.

    The first card is resolved as given and any later card is approved once.
    Returns the run, whether each card offered the request, and the kind of
    each standing authorization the run recorded.
    """
    outside = _Probe(_OUTSIDE_READ)
    model = FakeModelScript(
        turns=[
            _turn(("demo.outside", "read")),
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "first")),
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "second")),
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "third")),
            ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
        ]
    )
    offered: list[bool] = []
    async with build(
        settings=replace(_settings(tmp_path), code_mode_enabled=True),
        script=model,
        enabled_tools=[SANDBOX_RUN_CODE_TOOL_NAME, "demo.outside"],
    ) as built:
        registry = cast(StaticToolRegistry, built.tool_pipeline._registry)
        registry.register(outside)
        # The setting registers the real tool. A run without a worker lease
        # gives up its sandbox when it pauses for an approval, so the approval
        # path is driven with the real tool's spec over a stand-in body.
        registered = cast(RegisteredTool, registry.get(SANDBOX_RUN_CODE_TOOL_NAME))
        script = _Script(registered.spec, [[], [], []])
        registered.implementation = script
        run_id = await built.runs.submit("Work out how long the pages are.")
        for _ in range(4):
            run = await built.runs.get(run_id)
            if run.status is not RunStatus.WAITING_FOR_APPROVAL:
                break
            [card] = await built.approvals.list_pending(run_id=run_id)
            assert card.tool_name == SANDBOX_RUN_CODE_TOOL_NAME
            offered.append(card.script_request_grant_offer is not None)
            if len(offered) == 1 and first is ApprovalResolutionType.APPROVE_FOR_REQUEST:
                await built.services.approvals.resolve(
                    built.principal,
                    card.id,
                    first,
                    None,
                    request_grant=RequestGrantEcho(tool_name=SANDBOX_RUN_CODE_TOOL_NAME),
                )
            else:
                await built.approvals.resolve(card.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await built.runs.wait_terminal(run_id)
        authorized = [
            str(event.payload["authorization_kind"])
            for event in await built.runs.events(run_id)
            if event.event_type == "tool.call.authorized" and "authorization_kind" in event.payload
        ]
    assert run.status is RunStatus.COMPLETED, run.failure
    assert script.steps == [2, 3, 4]
    return run, offered, authorized


async def test_one_approval_covers_every_later_script_of_the_request(tmp_path: Path) -> None:
    run, offered, authorized = await _three_scripts_after_an_outside_read(
        tmp_path, ApprovalResolutionType.APPROVE_FOR_REQUEST
    )

    # One card, which offered the request. The second and third scripts ran
    # under it, and each use is recorded against the approval.
    assert offered == [True]
    assert authorized == [SCRIPT_REQUEST_GRANT_AUTHORIZATION_KIND] * 2
    assert run.tool_call_count == 4


async def test_approving_a_script_once_leaves_the_next_script_asking(tmp_path: Path) -> None:
    _, offered, authorized = await _three_scripts_after_an_outside_read(
        tmp_path, ApprovalResolutionType.APPROVE_ONCE
    )

    assert offered == [True, True, True]
    assert authorized == []


# --- the rule itself ----------------------------------------------------------


def _checkpoint(
    *,
    owner_trust: TrustLevel = TrustLevel.USER,
    context: TrustLevel = TrustLevel.USER,
    read: TrustLevel | None = None,
) -> RunCheckpoint:
    conversation: list[object] = [
        UserMessage(content=[TextPart(text="Work out the total.")], trust=owner_trust)
    ]
    if read is not None:
        conversation.extend(
            [
                ToolCallItem(
                    call_id="read", item_index=0, name="demo.read", arguments={}, raw_arguments="{}"
                ),
                ToolResultItem(call_id="read", content=[TextPart(text="a page")], trust=read),
            ]
        )
    return RunCheckpoint(
        run_id=UUID(int=342),
        version=1,
        status=RunStatus.RUNNING,
        conversation=conversation,  # type: ignore[arg-type]
        context_origin_trust=context,
        created_at=NOW,
    )


def _trust(checkpoint: RunCheckpoint, name: str = SANDBOX_RUN_CODE_TOOL_NAME) -> TrustLevel:
    [trust] = _argument_trust(dict(_SOURCE), checkpoint, tool_name=name).values()
    return trust


def test_source_written_in_a_turn_that_read_nothing_outside_is_veetbots_own() -> None:
    assert _trust(_checkpoint()) is TrustLevel.INTERNAL_TOOL


@pytest.mark.parametrize("read", [None, TrustLevel.MEMORY, TrustLevel.INTERNAL_TOOL])
def test_memory_in_the_context_does_not_make_a_script_ask(read: TrustLevel | None) -> None:
    # Production Chat always carries the owner's memory. A script reaches only
    # its run's sandbox workspace directly, and each call it makes is judged
    # under its own trust.
    assert _trust(_checkpoint(context=TrustLevel.MEMORY, read=read)) is TrustLevel.INTERNAL_TOOL


@pytest.mark.parametrize(
    "checkpoint",
    [
        _checkpoint(read=TrustLevel.EXTERNAL_UNTRUSTED),
        _checkpoint(read=TrustLevel.KNOWLEDGE),
        _checkpoint(context=TrustLevel.EXTERNAL_UNTRUSTED),
        _checkpoint(owner_trust=TrustLevel.EXTERNAL_UNTRUSTED),
        _checkpoint(owner_trust=TrustLevel.MEMORY),
    ],
    ids=["outside-read", "knowledge-read", "outside-context", "outside-sender", "memory-sender"],
)
def test_source_written_after_outside_content_stays_untrusted(
    checkpoint: RunCheckpoint,
) -> None:
    assert _trust(checkpoint) is TrustLevel.EXTERNAL_UNTRUSTED


def test_the_rule_names_one_tool() -> None:
    assert _trust(_checkpoint(), "sandbox.run_command") is TrustLevel.EXTERNAL_UNTRUSTED
