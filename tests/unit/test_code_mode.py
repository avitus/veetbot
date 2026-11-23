"""ADR-0342: code mode runs a model-written script whose tool calls cross the bridge.

The process tests run the actual sandbox entry point against a local socket.
They do not claim kernel isolation.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.config import ConfigurationError, load_settings
from agent_core.domain.execution import ExecutionCommand, ExecutionResult
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
)
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailureKind,
    ToolKind,
    ToolResult,
    ToolSpec,
)
from agent_core.tools.bridge import ProgrammaticBridgeSession
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.sandbox_run_code import SandboxRunCodeTool
from agent_core.tools.sandbox_run_command import SandboxRunCommandTool
from tests.contract.support import tool_context
from tests.unit.test_config import base_environment
from tests.unit.test_reply_attachments import _settings

# --- the sandbox entry point and its library -------------------------------


def _text_result(text: str) -> dict[str, object]:
    return {"status": "succeeded", "result": [{"type": "text", "text": text}], "retryable": False}


async def _run_script(
    source: str, responses: list[dict[str, object]] | None
) -> tuple[int | None, str, str, list[dict[str, Any]]]:
    """Run the runner in a subprocess; `responses` None means no bridge is offered."""
    calls: list[dict[str, Any]] = []
    pending = list(responses or [])

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            calls.append(json.loads(await reader.readline()))
            writer.write(json.dumps(pending.pop(0)).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    environment = {
        key: value for key, value in os.environ.items() if key != "AGENT_TOOL_BRIDGE_SOCKET"
    }
    with tempfile.TemporaryDirectory(prefix="code-mode-", dir="/tmp") as directory:  # noqa: S108
        server = None
        if responses is not None:
            socket_path = Path(directory) / "bridge.sock"
            server = await asyncio.start_unix_server(respond, path=str(socket_path))
            environment["AGENT_TOOL_BRIDGE_SOCKET"] = str(socket_path)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agent_core.execution.code_runner",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(source.encode()), timeout=20
            )
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()
    return process.returncode, stdout.decode(), stderr.decode(), calls


async def test_a_script_pages_two_lists_and_prints_only_the_intersection() -> None:
    source = """
first, second = set(), set()
for page in (1, 2):
    first.update(tools.demo.list_items(name="a", page=page)["items"])
second.update(tools.call("demo.list_items", {"name": "b", "page": 1})["items"])
print(sorted(first & second))
"""
    exit_code, stdout, stderr, calls = await _run_script(
        source,
        [
            _text_result('{"items": ["x", "y"]}'),
            _text_result('{"items": ["z"]}'),
            _text_result('{"items": ["z", "y", "q"]}'),
        ],
    )

    assert (exit_code, stderr) == (0, "")
    assert stdout == "['y', 'z']\n"
    assert calls == [
        {"call": "demo.list_items", "arguments": {"name": "a", "page": 1}, "ordinal": 0},
        {"call": "demo.list_items", "arguments": {"name": "a", "page": 2}, "ordinal": 1},
        {"call": "demo.list_items", "arguments": {"name": "b", "page": 1}, "ordinal": 2},
    ]


async def test_a_text_result_stays_text_and_parts_join_by_newline() -> None:
    exit_code, stdout, _stderr, _calls = await _run_script(
        "print(repr(tools.system.current_time()))\nprint(repr(tools.demo.two_parts()))\n",
        [
            _text_result("2026-11-21T10:00:00Z"),
            {
                "status": "succeeded",
                "result": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
            },
        ],
    )

    assert exit_code == 0
    assert stdout == "'2026-11-21T10:00:00Z'\n'one\\ntwo'\n"


async def test_a_refused_call_raises_an_error_the_script_can_read() -> None:
    source = """
try:
    tools.email.send(to="someone")
except ToolCallError as error:
    print(error.status, error.reason_code, error.retryable)
"""
    exit_code, stdout, _stderr, _calls = await _run_script(
        source,
        [{"status": "denied", "reason_code": "policy.matrix.external_message", "retryable": False}],
    )

    assert exit_code == 0
    assert stdout == "denied policy.matrix.external_message False\n"


async def test_an_uncaught_refusal_ends_the_script_with_its_reason_on_stderr() -> None:
    exit_code, stdout, stderr, _calls = await _run_script(
        "print('before')\ntools.email.send(to='someone')\nprint('after')\n",
        [{"status": "denied", "reason_code": "bridge.call_limit", "retryable": False}],
    )

    assert exit_code == 1
    assert stdout == "before\n"
    assert "ToolCallError" in stderr
    assert "bridge.call_limit" in stderr
    assert 'File "<code>", line 2' in stderr


async def test_a_refusal_the_bridge_did_not_count_keeps_the_next_ordinal() -> None:
    source = """
for attempt in range(2):
    try:
        print(tools.demo.read())
    except ToolCallError as error:
        print(error.reason_code)
"""
    exit_code, stdout, _stderr, calls = await _run_script(
        source,
        [
            {"status": "denied", "reason_code": "bridge.call_limit", "retryable": False},
            _text_result("read"),
        ],
    )

    assert exit_code == 0
    assert stdout == "bridge.call_limit\nread\n"
    assert [call["ordinal"] for call in calls] == [0, 0]


async def test_a_script_without_tool_calls_needs_no_bridge() -> None:
    exit_code, stdout, stderr, _calls = await _run_script("print(sum(range(5)))\n", None)

    assert (exit_code, stdout, stderr) == (0, "10\n", "")


async def test_a_call_without_a_bridge_raises_instead_of_hanging() -> None:
    source = """
try:
    tools.demo.anything()
except ToolCallError as error:
    print(error.status, error.reason_code)
"""
    exit_code, stdout, _stderr, _calls = await _run_script(source, None)

    assert exit_code == 0
    assert stdout == "unavailable bridge.unavailable\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("print(", "SyntaxError"),
        ("", "empty"),
        ("tools.call('demo.x', {'value': float('nan')})", "ValueError"),
        ("tools._private()", "AttributeError"),
        ("tools.call('demo.x', ['not', 'an', 'object'])", "TypeError"),
    ],
)
async def test_a_broken_script_fails_with_feedback_and_sends_nothing(
    source: str, expected: str
) -> None:
    exit_code, stdout, stderr, calls = await _run_script(source, [])

    assert exit_code == 1
    assert stdout == ""
    assert expected in stderr
    assert calls == []


# --- the tool ---------------------------------------------------------------


class _Manager:
    """Stands in for SandboxManager: records the command and plays a script's bridge calls."""

    def __init__(self, bridge_calls: list[tuple[str, dict[str, Any]]] | None = None) -> None:
        self.bridge_calls = bridge_calls or []
        self.commands: list[ExecutionCommand] = []
        self.bridges: list[ProgrammaticBridgeSession | None] = []
        self.responses: list[dict[str, Any]] = []

    async def execute_for(
        self,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        command: ExecutionCommand,
        bridge: ProgrammaticBridgeSession | None = None,
    ) -> ExecutionResult:
        del tenant_id, run_id, lease_epoch
        self.commands.append(command)
        self.bridges.append(bridge)
        if bridge is not None:
            for ordinal, (name, arguments) in enumerate(self.bridge_calls):
                request = {
                    "token": bridge.token,
                    "call": name,
                    "arguments": arguments,
                    "ordinal": ordinal,
                }
                self.responses.append(json.loads(await bridge.handle(json.dumps(request).encode())))
        return ExecutionResult(
            exit_code=0,
            stdout=b"['y', 'z']\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            killed_by=None,
            files_changed=(),
            duration_ms=12,
        )


def _context(dispatch: object | None, *, call_id: str = "call-code") -> ToolExecutionContext:
    return replace(tool_context(), bridge_dispatch=dispatch, call_id=call_id, timeout_seconds=300)


def test_the_tool_is_classified_exactly_like_the_command_tool() -> None:
    code, command = SandboxRunCodeTool.spec, SandboxRunCommandTool.spec

    assert code.name == "sandbox.run_code"
    assert code.kind is ToolKind.CAPABILITY
    for field in (
        "side_effect",
        "risk",
        "idempotency",
        "required_scopes",
        "timeout_seconds",
        "maximum_output_bytes",
        "allow_parallel",
        "target_kind",
        "output_trust",
    ):
        assert getattr(code, field) == getattr(command, field), field


async def test_the_tool_sends_the_source_on_stdin_and_reports_bridged_calls() -> None:
    dispatched: list[tuple[str, dict[str, Any], str]] = []

    async def dispatch(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        dispatched.append((name, arguments, call_id))
        return _text_result("{}")

    manager = _Manager([("demo.list_items", {"page": 1}), ("demo.list_items", {"page": 2})])
    tool = SandboxRunCodeTool(cast(Any, manager))
    source = "print(tools.demo.list_items(page=1))"

    result = await tool.execute({"code": source, "timeout_seconds": 20}, _context(dispatch))

    [command] = manager.commands
    assert command.argv == ("python", "-m", "agent_core.execution.code_runner")
    assert command.stdin == source.encode()
    assert command.working_directory == PurePosixPath(".")
    assert command.timeout_seconds == 20
    assert [(name, arguments) for name, arguments, _ in dispatched] == manager.bridge_calls
    assert all(call_id.startswith("bridge:") for _, _, call_id in dispatched)
    assert result.ok
    assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert result.content == [TextPart(text="['y', 'z']\n")]
    assert result.structured is not None
    assert result.structured["stdout"] == "['y', 'z']\n"
    assert result.structured["exit_code"] == 0
    assert result.structured["tool_calls"] == 2
    assert result.structured["tool_call_limit_reached"] is False


async def test_bridge_call_ids_follow_the_invocation_not_the_source_alone() -> None:
    seen: dict[str, list[str]] = {"first": [], "replay": [], "later": []}

    def recorder(bucket: str) -> Any:
        async def dispatch(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
            del name, arguments
            seen[bucket].append(call_id)
            return _text_result("{}")

        return dispatch

    source = {"code": "tools.demo.read()"}
    for bucket, call_id in (("first", "call-1"), ("replay", "call-1"), ("later", "call-2")):
        tool = SandboxRunCodeTool(cast(Any, _Manager([("demo.read", {})])))
        await tool.execute(source, _context(recorder(bucket), call_id=call_id))

    assert seen["first"] == seen["replay"]
    assert seen["first"] != seen["later"]


async def test_a_script_past_the_call_cap_is_refused_and_the_result_says_so() -> None:
    async def dispatch(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        del name, arguments, call_id
        return _text_result("{}")

    manager = _Manager([("demo.read", {})] * 65)
    result = await SandboxRunCodeTool(cast(Any, manager)).execute(
        {"code": "while True: tools.demo.read()"}, _context(dispatch)
    )

    assert manager.responses[63]["status"] == "succeeded"
    assert manager.responses[64] == {
        "status": "denied",
        "reason_code": "bridge.call_limit",
        "retryable": False,
    }
    assert result.structured is not None
    assert result.structured["tool_calls"] == 64
    assert result.structured["tool_call_limit_reached"] is True


@pytest.mark.parametrize("code", ["", "   \n", "x" * (64 * 1024 + 1), "print('\x00')"])
async def test_the_tool_refuses_source_outside_its_bounds(code: str) -> None:
    manager = _Manager()

    result = await SandboxRunCodeTool(cast(Any, manager)).execute({"code": code}, _context(None))

    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert result.failure.reason_code == "tool.arguments_invalid"
    assert manager.commands == []


async def test_the_approval_card_names_the_first_line_of_the_script() -> None:
    tool = SandboxRunCodeTool(cast(Any, _Manager()))
    arguments = {"code": "\n# Compare the two lists\nitems = tools.demo.list_items()\n"}

    summary, shown = await tool.approval_view(arguments, tenant_id="tenant")

    assert summary == "Run a Python script in Veetbot's sandbox: # Compare the two lists"
    assert shown == arguments


# --- the bridge refuses control tools and sandbox tools ----------------------


def _spec(name: str, **overrides: Any) -> ToolSpec:
    fields: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "description": f"Test probe {name}.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "output_schema": {"type": "object"},
        "side_effect": SideEffectClass.NONE,
        "risk": RiskLevel.LOW,
        "idempotency": IdempotencyClass.READ_ONLY,
        "required_scopes": set(),
        "timeout_seconds": 10,
        "maximum_output_bytes": 4096,
        "allow_parallel": False,
        "target_kind": "in_process",
        "output_trust": TrustLevel.INTERNAL_TOOL,
    }
    return ToolSpec(**(fields | overrides))


class _Probe:
    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec
        self.executions = 0

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments, context
        self.executions += 1
        return ToolResult(ok=True, content=[TextPart(text="ran")], structured={})


class _Script:
    """A sandbox-targeted tool whose 'script' makes the given bridge calls in order."""

    spec = _spec(
        "demo.script",
        target_kind="sandbox",
        side_effect=SideEffectClass.CODE_EXECUTION,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.responses: list[dict[str, Any]] = []

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments
        dispatch = cast(Any, context.bridge_dispatch)
        for index, name in enumerate(self._names):
            self.responses.append(await dispatch(name, {}, f"bridge-{index}"))
        return ToolResult(
            ok=True,
            content=[TextPart(text="script finished")],
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )


async def test_the_bridge_refuses_control_and_sandbox_tools_and_still_runs_ordinary_ones(
    tmp_path: Path,
) -> None:
    nested = _Probe(
        _spec(
            "demo.nested_script",
            target_kind="sandbox",
            side_effect=SideEffectClass.CODE_EXECUTION,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
    )
    ordinary = _Probe(_spec("demo.ordinary"))
    # The control set is closed at build time, so the probe is a real control tool.
    script = _Script(["conversation.ask_user", "demo.nested_script", "demo.ordinary"])
    model = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="demo.script", arguments={}, call_id="script")],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
        ]
    )
    names = ["conversation.ask_user", "demo.nested_script", "demo.ordinary", "demo.script"]

    async with build(settings=_settings(tmp_path), script=model, enabled_tools=names) as built:
        registry = cast(StaticToolRegistry, built.tool_pipeline._registry)
        for tool in (nested, ordinary, script):
            registry.register(tool)
        run = await built.runs.wait_terminal(await built.runs.submit("Run the script."))

    refusal = {"status": "denied", "reason_code": "bridge.tool_not_bridgeable", "retryable": False}
    assert run.status is RunStatus.COMPLETED
    assert script.responses[:2] == [refusal, refusal]
    assert script.responses[2]["status"] == "succeeded"
    assert (nested.executions, ordinary.executions) == (0, 1)


# --- activation ---------------------------------------------------------------


def _code_mode_settings(tmp_path: Path, flag: str | None) -> Any:
    environment = {**base_environment(), "SANDBOX_MECHANISM": "fake"}
    if flag is not None:
        environment["AGENT_CODE_MODE_ENABLED"] = flag
    return replace(load_settings(environment), artifact_root=tmp_path / "artifacts")


@pytest.mark.parametrize(("flag", "registered"), [(None, False), ("0", False), ("1", True)])
async def test_the_tool_exists_only_when_code_mode_is_enabled(
    tmp_path: Path, flag: str | None, registered: bool
) -> None:
    model = FakeModelScript(turns=[ScriptedTurn(text="Hello.", stop_reason=StopReason.END_TURN)])

    async with build(settings=_code_mode_settings(tmp_path, flag), script=model) as built:
        registry = cast(StaticToolRegistry, built.tool_pipeline._registry)
        names = {name for name, _version in registry._tools}
        run = await built.runs.wait_terminal(await built.runs.submit("Hello?"))
        async with built.uow_factory() as uow:
            agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    assert ("sandbox.run_code" in names) is registered
    assert ("sandbox.run_code" in agent.enabled_tools) is registered
    assert "sandbox.run_command" in names


def test_the_setting_accepts_only_zero_or_one() -> None:
    with pytest.raises(ConfigurationError, match="AGENT_CODE_MODE_ENABLED must be 0 or 1"):
        load_settings({**base_environment(), "AGENT_CODE_MODE_ENABLED": "yes"})
