"""ADR-0277: a call sees the trust of results earlier in its batch or bridge script."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from agent_core.bootstrap import build
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
)
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.tools.registry import StaticToolRegistry
from tests.unit.test_reply_attachments import _settings


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


def _ok(text: str, trust: TrustLevel | None = None) -> ToolResult:
    return ToolResult(ok=True, content=[TextPart(text=text)], output_trust=trust)


class _UntrustedRead:
    spec = _spec("demo.untrusted_read", output_trust=TrustLevel.EXTERNAL_UNTRUSTED)

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        return _ok("text from a web page", TrustLevel.EXTERNAL_UNTRUSTED)


class _TrustProbe:
    spec = _spec("demo.trust_probe")

    def __init__(self) -> None:
        self.seen: list[TrustLevel] = []

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        self.seen.append(context.origin_trust)
        return _ok("probed")


class _BridgeScript:
    spec = _spec(
        "demo.bridge_script",
        target_kind="sandbox",
        side_effect=SideEffectClass.CODE_EXECUTION,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        dispatch = cast(Any, context.bridge_dispatch)
        await dispatch("demo.untrusted_read", {}, "bridge-read")
        await dispatch("demo.trust_probe", {}, "bridge-probe")
        return _ok("script finished", TrustLevel.EXTERNAL_UNTRUSTED)


async def _run(tmp_path: Path, calls: list[ScriptedToolCall]) -> tuple[RunStatus, list[TrustLevel]]:
    probe = _TrustProbe()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(tool_calls=calls, stop_reason=StopReason.TOOL_USE),
            ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
        ]
    )
    names = ["demo.untrusted_read", "demo.trust_probe", "demo.bridge_script"]
    async with build(settings=_settings(tmp_path), script=script, enabled_tools=names) as built:
        registry = cast(StaticToolRegistry, built.tool_pipeline._registry)
        for tool in (_UntrustedRead(), probe, _BridgeScript()):
            registry.register(tool)
        run = await built.runs.wait_terminal(await built.runs.submit("Run the probes."))
    return run.status, probe.seen


async def test_a_serial_batch_carries_an_earlier_untrusted_result(tmp_path: Path) -> None:
    status, seen = await _run(
        tmp_path,
        [
            ScriptedToolCall(name="demo.untrusted_read", arguments={}, call_id="read"),
            ScriptedToolCall(name="demo.trust_probe", arguments={}, call_id="probe"),
        ],
    )
    assert status is RunStatus.COMPLETED
    assert seen == [TrustLevel.EXTERNAL_UNTRUSTED]


async def test_a_bridge_call_carries_an_earlier_bridge_result(tmp_path: Path) -> None:
    status, seen = await _run(
        tmp_path, [ScriptedToolCall(name="demo.bridge_script", arguments={}, call_id="script")]
    )
    assert status is RunStatus.COMPLETED
    assert seen == [TrustLevel.EXTERNAL_UNTRUSTED]
