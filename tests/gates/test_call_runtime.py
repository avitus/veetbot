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
from tests.gates.test_email_m18 import _credential as gmail_credential
from tests.gates.test_email_m18 import _generated_gmail_discovery as generated_gmail_discovery
from tests.integration.m2_support import memory_settings
from tests.unit.test_web_tools import FakeWebProvider


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


async def test_production_roster_offers_the_owner_the_call_tools() -> None:
    """The owner's full roster still reaches Chat's call tools, not only a reduced one.

    Production runs People, Email mode, scheduling and web together. Discovered
    tools fill what those leave, so the token cap, not the item cap, decided
    whether calling reached the model at all.
    """
    configuration = call_configuration()
    scripts = {
        f"gmail_{mode}": ScriptedMCPServer(
            name=f"gmail_{mode}", discovery=await generated_gmail_discovery(mode)
        )
        for mode in ("read", "write", "send")
    }
    for mode in ("read", "call"):
        catalog = create_server(mode, BlandClient(KEY, NUMBER), configuration.model_dump())
        scripts[f"bland_{mode}"] = ScriptedMCPServer(
            name=f"bland_{mode}",
            discovery=MCPDiscovery(
                tools=tuple(
                    MCPRemoteTool(
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=tool.input_schema,
                        application_only=(tool.meta or {}).get("veetbot/application-only") is True,
                    )
                    for tool in await catalog.list_tools()
                )
            ),
        )
    settings = replace(
        memory_settings(),
        people_enabled=True,
        email_enabled=True,
        email_mode_enabled=True,
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
        call_enabled=True,
        call_configuration=configuration,
        credentials={
            **{
                f"gmail_{mode}": SecretStr(gmail_credential(mode).as_json())
                for mode in ("read", "write", "send")
            },
            **{
                name: SecretStr(
                    json.dumps({"api_key": KEY, "configuration": configuration.model_dump()})
                )
                for name in ("bland_read", "bland_call")
            },
        },
    )
    provider = FakeWebProvider()
    async with build(
        settings=settings,
        script=FakeModelScript(
            turns=[ScriptedTurn(text="Here are your calls.", stop_reason=StopReason.END_TURN)]
        ),
        mcp_client_factory=ScriptedMCPClientFactory(scripts),
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit("List my recent phone calls.", session_id)
        terminal = await asyncio.wait_for(composition.runs.wait_terminal(run_id), timeout=30)
        plan = await composition.executor._context_planner.current(session_id)

    assert terminal.status is RunStatus.COMPLETED
    assert plan is not None
    assert {
        "mcp.bland_read.list_calls",
        "mcp.bland_read.get_call",
        "mcp.bland_call.start_call",
    }.issubset(plan.tool_names)
