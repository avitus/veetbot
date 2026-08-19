"""PostgreSQL coverage for the provider-neutral web tranche.

The unit and contract partitions prove the tool, policy, and adapter
behavior in memory; this file proves the durable half of the acceptance
contract: a web invocation persists with its external-untrusted result,
and enabling web access over an existing database recomposes cleanly
instead of conflicting with the persisted fallback agent.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_core.bootstrap import Composition, build
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings
from tests.unit.test_web_tools import FakeWebProvider


async def _run_worker(composition: Composition, worker_id: str) -> None:
    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=composition.clock,
        worker_id=worker_id,
    )
    assert await worker.run_once()


async def test_postgres_web_search_persists_an_external_untrusted_invocation(
    tmp_path: Path,
) -> None:
    provider = FakeWebProvider()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="web.search",
                        arguments={"query": "Ada Lovelace", "max_results": 5},
                        call_id="web-postgres-search",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I found a public source."),
        ]
    )
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")

    async with build(
        settings=settings,
        storage="postgres",
        script=script,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Find public information about Ada Lovelace.")
        await _run_worker(composition, "web-postgres-worker")
        run = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "I found a public source."
    assert provider.searches[0].query == "Ada Lovelace"
    assert len(invocations) == 1
    invocation = invocations[0]
    assert invocation.tool_name == "web.search"
    assert invocation.status is ToolInvocationStatus.SUCCEEDED
    assert invocation.result_item is not None
    assert invocation.result_item.trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert invocation.structured_result is not None
    assert invocation.structured_result["provider"] == "fake-web"
    assert invocation.output_bytes is not None
    assert invocation.truncated is False


async def test_enabling_web_access_recomposes_against_an_existing_database(
    tmp_path: Path,
) -> None:
    provider = FakeWebProvider()
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")

    async with build(
        settings=settings,
        storage="postgres",
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
    ) as composition:
        run_id = await composition.runs.submit("ready?")
        await _run_worker(composition, "web-postgres-before")
        before = await composition.runs.get(run_id)

    async with build(
        settings=settings,
        storage="postgres",
        script=FakeModelScript(turns=[ScriptedTurn(text="web ready")]),
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("web ready?")
        await _run_worker(composition, "web-postgres-after")
        after = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            plain_agent = await uow.agents.get_version(before.agent_id, before.agent_version)
            web_agent = await uow.agents.get_version(after.agent_id, after.agent_version)

    assert after.status is RunStatus.COMPLETED
    assert before.agent_id == after.agent_id
    assert before.agent_version != after.agent_version
    assert "web.search" not in plain_agent.enabled_tools
    assert "web.search" in web_agent.enabled_tools
    assert "web.fetch" in web_agent.enabled_tools
