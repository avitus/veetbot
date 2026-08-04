"""PostgreSQL and filesystem round trip for Milestone 8 skills and MCP."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.bootstrap import build
from agent_core.domain.errors import NotFoundError
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    MCPServerConfig,
    MCPTransport,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.skills import (
    SkillPackage,
    SkillPackageMember,
    SkillRef,
    SkillRevision,
    SkillSource,
)
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings


def _package(
    name: str = "persist-m8",
    body: str = "DURABLE_SKILL_BODY",
    *,
    version: str = "1.0.0",
) -> SkillPackage:
    return SkillPackage(
        directory_name=name,
        members=(
            SkillPackageMember(
                path="SKILL.md",
                data=(
                    f"---\nname: {name}\nversion: {version}\n"
                    "description: Durable.\nrequired_tools: []\n---\n"
                    f"{body}"
                ).encode(),
            ),
        ),
    )


async def test_postgres_skill_and_mcp_round_trip(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path)
    config = MCPServerConfig(
        tenant_id="local",
        server_id="durable",
        transport=MCPTransport.STDIO,
        endpoint="/fixture/durable",
        operator_configured=True,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )
    server = ScriptedMCPServer(
        name="durable",
        discovery=MCPDiscovery(
            tools=(
                MCPRemoteTool(
                    name="echo",
                    input_schema={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                ),
            )
        ),
        responses=(
            ScriptedMCPResponse(
                name="echo",
                result=MCPCallResult(content=("durable MCP result",)),
            ),
        ),
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="skill.load",
                        arguments={"name": "persist-m8"},
                        call_id="initial_skill_load",
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="mcp.durable.echo",
                        arguments={"value": "round trip"},
                    )
                ]
            ),
            ScriptedTurn(text="durable completion"),
        ]
    )
    async with build(
        settings=settings,
        storage="postgres",
        script=script,
        enabled_tools=["skill.load", "mcp.durable.echo"],
        enabled_skills=["persist-m8"],
        skill_packages=((_package(), SkillSource.OPERATOR),),
        mcp_servers=(config,),
        mcp_scripts={"durable": server},
    ) as composition:
        rollback_key = ""
        with pytest.raises(RuntimeError, match="force outer rollback"):
            async with composition.uow_factory() as uow:
                rolled_back = await uow.skills.install(
                    "local",
                    _package("rollback-m8", "ROLLBACK_BODY"),
                    SkillSource.OPERATOR,
                    None,
                    None,
                )
                rollback_key = rolled_back.package_key
                raise RuntimeError("force outer rollback")
        assert rollback_key
        assert not (tmp_path / "skill-packages" / rollback_key).exists()
        async with composition.uow_factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.skills.resolve("local", SkillRef.parse("rollback-m8"))

        async def install_concurrent(version: str, body: str) -> SkillRevision:
            async with composition.uow_factory() as uow:
                return await uow.skills.install(
                    "local",
                    _package("concurrent-m8", body, version=version),
                    SkillSource.OPERATOR,
                    None,
                    None,
                )

        concurrent = await asyncio.gather(
            install_concurrent("1.0.0", "FIRST"),
            install_concurrent("1.1.0", "SECOND"),
        )
        assert len({item.skill_id for item in concurrent}) == 1
        assert {item.revision for item in concurrent} == {1, 2}

        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit("exercise durable M8", session_id)
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="m8-persistence-worker",
        )
        assert await worker.run_once()
        completed = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
            revision = await uow.skills.resolve("local", SkillRef.parse("persist-m8"))
            servers = await uow.mcp_servers.list_enabled("local")
            events = await uow.events.list_after(session_id, 0, composition.principal)

    assert completed.final_message == "durable completion"
    assert checkpoint is not None
    assert checkpoint.loaded_skills[0].content == "DURABLE_SKILL_BODY"
    assert revision.body == "DURABLE_SKILL_BODY"
    assert any(item.server_id == "durable" for item in servers)
    assert any(event.event_type == "mcp.server.connected" for event in events)

    resumed_script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="skill.load",
                        arguments={"name": "persist-m8"},
                        call_id="resumed_skill_load",
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="mcp.durable.echo",
                        arguments={"value": "after reconstruction"},
                        call_id="resumed_mcp_echo",
                    )
                ]
            ),
            ScriptedTurn(text="resumed completion"),
        ]
    )
    resumed_server = server.model_copy(
        update={
            "responses": (
                ScriptedMCPResponse(
                    name="echo",
                    result=MCPCallResult(content=("reconstructed MCP result",)),
                ),
            )
        },
        deep=True,
    )
    async with build(
        settings=settings,
        storage="postgres",
        script=resumed_script,
        enabled_tools=["skill.load", "mcp.durable.echo"],
        enabled_skills=["persist-m8"],
        mcp_scripts={"durable": resumed_server},
    ) as resumed:
        resumed_run_id = await resumed.runs.submit("resume durable M8", session_id)
        worker = DurableWorker(
            uow_factory=resumed.uow_factory,
            executor=resumed.executor,
            clock=resumed.clock,
            worker_id="m8-reconstructed-worker",
        )
        assert await worker.run_once()
        reconstructed = await resumed.runs.get(resumed_run_id)
        async with resumed.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(resumed_run_id)

    assert reconstructed.final_message == "resumed completion"
    assert checkpoint is not None
    assert checkpoint.loaded_skills[0].content == "DURABLE_SKILL_BODY"
