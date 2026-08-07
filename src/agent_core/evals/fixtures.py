"""Authored fake-model fixture resolution and compatibility translation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import yaml
from pydantic import ValidationError

from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPRemotePrompt,
    MCPRemoteResource,
    MCPRemoteTool,
    MCPServerConfig,
    MCPTransport,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ModelError,
    ModelPermanentError,
    ModelTransientError,
    ModelUsage,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.skills import SkillPackage, SkillPackageMember, SkillSource


@dataclass(frozen=True, slots=True)
class ResolvedMCPFixture:
    config: MCPServerConfig
    script: ScriptedMCPServer


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _legacy_turn(raw: object, usage: ModelUsage | None) -> ScriptedTurn:
    row = _mapping(raw, "model fixture turn")
    kind = row.get("kind")
    context_contains = None if row.get("context_contains") is None else str(row["context_contains"])
    if kind == "tool_call":
        return ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name=str(row["tool_name"]),
                    arguments=row.get("arguments", {}),
                    call_id=str(row["call_id"]),
                )
            ],
            stop_reason=StopReason.TOOL_USE,
            usage=usage,
            context_contains=context_contains,
        )
    if kind == "final":
        return ScriptedTurn(
            text=str(row.get("text", "")),
            stop_reason=StopReason(str(row.get("stop_reason", StopReason.END_TURN.value))),
            usage=usage,
            context_contains=context_contains,
        )
    if kind == "error":
        error_class = str(row.get("error_class", "permanent"))
        message = str(row.get("message", f"scripted {error_class} model failure"))
        error: ModelError
        if error_class == "transient":
            error = ModelTransientError(
                provider="fake",
                model="scripted",
                attempt_id=UUID(int=0),
                message=message,
                stream_had_output=int(row.get("after_bytes", 0)) > 0,
            )
        elif error_class == "permanent":
            error = ModelPermanentError(
                provider="fake",
                model="scripted",
                attempt_id=UUID(int=0),
                message=message,
            )
        else:
            raise ValueError(f"unsupported model fixture error_class {error_class!r}")
        return ScriptedTurn(
            fail_with=error,
            usage=usage,
            context_contains=context_contains,
        )
    raise ValueError(f"unsupported legacy model fixture turn kind {kind!r}")


def load_model_fixture(path: Path) -> FakeModelScript:
    """Parse the canonical shape, or the plan's authored shorthand shape."""

    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = _mapping(loaded, str(path))
    turns = root.get("turns")
    if not isinstance(turns, list):
        raise ValueError(f"{path} must declare a turns list")
    if not any(isinstance(turn, dict) and "kind" in turn for turn in turns):
        try:
            return FakeModelScript.model_validate(loaded)
        except ValidationError as exc:
            raise ValueError(f"{path} is not a valid FakeModelScript: {exc}") from exc
    else:
        usage_raw = root.get("usage")
        usage = ModelUsage.model_validate(usage_raw) if usage_raw is not None else None
        on_exhausted = cast(Literal["error", "repeat_last"], str(root.get("on_exhausted", "error")))
        return FakeModelScript(
            turns=[_legacy_turn(turn, usage) for turn in turns],
            on_exhausted=on_exhausted,
        )


def resolve_model_fixture(fixture_root: Path, name: str) -> FakeModelScript:
    path = fixture_root / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"model fixture {name!r} does not resolve to {path}")
    return load_model_fixture(path)


def resolve_skill_fixture(fixture_root: Path, name: str) -> tuple[SkillPackage, SkillSource]:
    path = fixture_root / name / "SKILL.md"
    if not path.is_file():
        raise ValueError(f"skill fixture {name!r} does not resolve to {path}")
    package = SkillPackage(
        directory_name=name,
        members=(SkillPackageMember(path="SKILL.md", data=path.read_bytes()),),
    )
    return package, SkillSource.OPERATOR


def resolve_mcp_fixture(
    fixture_root: Path,
    name: str,
    *,
    tenant_id: str,
) -> ResolvedMCPFixture:
    path = fixture_root / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"MCP fixture {name!r} does not resolve to {path}")
    root = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))
    server_id = str(root.get("server_id", ""))
    transport = MCPTransport(str(root.get("transport", "stdio")))
    raw_tools = root.get("tools", [])
    raw_prompts = root.get("prompts", [])
    raw_resources = root.get("resources", [])
    raw_script = root.get("script", [])
    collections = (raw_tools, raw_prompts, raw_resources, raw_script)
    if not all(isinstance(value, list) for value in collections):
        raise ValueError(f"{path} fixture collections must be lists")
    tools = tuple(
        MCPRemoteTool(
            name=str(row["name"]),
            description=str(row.get("description", "")),
            input_schema=_mapping(row.get("input_schema", {}), "MCP tool input_schema"),
        )
        for raw in raw_tools
        for row in [_mapping(raw, "MCP tool")]
    )
    prompts = tuple(
        MCPRemotePrompt(
            name=str(row["name"]),
            description=str(row.get("description", "")),
            body=str(row["body"]),
        )
        for raw in raw_prompts
        for row in [_mapping(raw, "MCP prompt")]
    )
    resources = tuple(
        MCPRemoteResource(
            uri=str(row["uri"]),
            name=str(row["name"]),
            description=str(row.get("description", "")),
        )
        for raw in raw_resources
        for row in [_mapping(raw, "MCP resource")]
    )
    actions: dict[int, Literal["result", "disconnect", "unauthorized"]] = {}
    for raw in raw_script:
        row = _mapping(raw, "MCP script action")
        at_call = row.get("at_call")
        action = str(row.get("action", ""))
        if isinstance(at_call, bool) or not isinstance(at_call, int) or at_call < 1:
            raise ValueError("MCP script at_call must be a positive integer")
        if action not in {"result", "disconnect", "unauthorized"}:
            raise ValueError(f"unsupported MCP script action {action!r}")
        if at_call in actions:
            raise ValueError(f"MCP script action is duplicated for call {at_call}")
        actions[at_call] = cast(Literal["result", "disconnect", "unauthorized"], action)
    responses: list[ScriptedMCPResponse] = []
    ordinal = 0
    for raw in raw_tools:
        row = _mapping(raw, "MCP tool")
        replies = row.get("replies", [])
        if not isinstance(replies, list):
            raise ValueError("MCP tool replies must be a list")
        for raw_reply in replies:
            reply = _mapping(raw_reply, "MCP tool reply")
            ordinal += 1
            outcome = actions.get(ordinal, "result")
            error = reply.get("error")
            content = error if error is not None else reply.get("content", "")
            responses.append(
                ScriptedMCPResponse(
                    name=str(row["name"]),
                    outcome=outcome,
                    result=MCPCallResult(
                        content=(str(content),) if content != "" else (),
                        is_error=error is not None,
                    ),
                )
            )
    unmatched_actions = set(actions) - set(range(1, ordinal + 1))
    if unmatched_actions:
        raise ValueError(f"MCP script actions reference missing calls: {sorted(unmatched_actions)}")
    endpoint = str(root.get("endpoint", "/fixture/mcp-server"))
    config = MCPServerConfig(
        tenant_id=tenant_id,
        server_id=server_id,
        transport=transport,
        endpoint=endpoint,
        operator_configured=transport is MCPTransport.STDIO,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )
    return ResolvedMCPFixture(
        config=config,
        script=ScriptedMCPServer(
            name=name,
            discovery=MCPDiscovery(tools=tools, prompts=prompts, resources=resources),
            responses=tuple(responses),
        ),
    )
