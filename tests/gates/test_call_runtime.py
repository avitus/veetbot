"""Calling through the ordinary model, approval, MCP and durable tool pipeline."""

import asyncio
import json
from dataclasses import replace

import pytest
from pydantic import SecretStr

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from bland_mcp.client import BlandClient
from bland_mcp.server import create_server
from tests.contract.test_bland_client_contract import CALL_ID, KEY, NUMBER
from tests.gates.test_call_lifecycle import arguments
from tests.gates.test_call_m27 import call_configuration
from tests.integration.m2_support import memory_settings


@pytest.mark.parametrize("approved", [False, True])
async def test_call_dispatch_occurs_only_after_the_bound_owner_approval(approved: bool) -> None:
    configuration = call_configuration()
    catalog = create_server("call", BlandClient(KEY, NUMBER), configuration.model_dump())
    discovered = MCPDiscovery(
        tools=tuple(
            MCPRemoteTool(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
                application_only=(tool.meta or {}).get("veetbot/application-only") is True,
            )
            for tool in await catalog.list_tools()
        )
    )
    factory = ScriptedMCPClientFactory(
        {
            "bland_read": ScriptedMCPServer(name="bland_read"),
            "bland_call": ScriptedMCPServer(
                name="bland_call",
                discovery=discovered,
                responses=(
                    ScriptedMCPResponse(
                        name="start_call",
                        result=MCPCallResult(
                            structured={"provider_call_id": CALL_ID, "status": "accepted"}
                        ),
                    ),
                ),
            ),
        }
    )
    settings = replace(
        memory_settings(),
        call_enabled=True,
        call_configuration=configuration,
        credentials={
            name: SecretStr(
                json.dumps({"api_key": KEY, "configuration": configuration.model_dump()})
            )
            for name in ("bland_read", "bland_call")
        },
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="mcp.bland_call.start_call", arguments=arguments())
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The call request was processed.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=settings, script=script, sequential_ids=True, mcp_client_factory=factory
    ) as app:
        run_id = await app.runs.submit("Call to ask about ticket 12.")
        [approval] = await app.approvals.list_pending(run_id=run_id)
        assert sum(client.call_count for client in factory.created) == 0
        async with app.uow_factory() as uow:
            assert await uow.calls.list(app.principal, "dispatch") == []
        await app.approvals.resolve(
            approval.id,
            ApprovalResolutionType.APPROVE_ONCE if approved else ApprovalResolutionType.DENY,
        )
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=5)
        assert terminal.status is RunStatus.COMPLETED
        assert sum(client.call_count for client in factory.created) == int(approved)
        async with app.uow_factory() as uow:
            reservations = await uow.calls.list(app.principal, "dispatch")
        assert len(reservations) == int(approved)
        if approved:
            assert reservations[0].payload["provider_call_id"] == CALL_ID
            assert reservations[0].payload["status"] == "accepted"
