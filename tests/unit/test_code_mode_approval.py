"""Code mode approval: when a script Veetbot wrote runs without asking (ADR-0342).

A script written in a turn that has read no outside content is Veetbot's own
work on the owner's request and runs without an approval. Once the turn has
read a web page, a message or an earlier script's output, the next script asks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
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
from agent_core.tools.executor import _argument_trust
from agent_core.tools.registry import StaticToolRegistry
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
    # Production Chat always carries the owner's memory. A script is not an
    # external effect, and each call it makes is judged under its own trust.
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
