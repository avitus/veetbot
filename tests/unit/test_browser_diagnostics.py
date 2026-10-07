"""Content-free phase evidence through the real tool pipeline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.domain.browser import BrowserObservation
from agent_core.domain.browser_diagnostics import BrowserDiagnostics, Phase
from agent_core.domain.events import conversation_items
from agent_core.domain.messages import ToolCallItem
from agent_core.domain.runs import Step
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.browser_observe import BrowserObserveTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW, agent, principal, run
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_history_cache_window import _checkpoint, _factory


@pytest.mark.parametrize("fails", [False, True])
async def test_terminal_browser_event_has_safe_diagnostics_without_changing_replay(
    fails: bool,
) -> None:
    class Provider(FakeBrowserProvider):
        async def observe(self) -> BrowserObservation:
            if fails:
                raise RuntimeError("secret-cookie-canary")
            return await super().observe()

    clock, factory = await _factory()
    registry = StaticToolRegistry()
    tool = BrowserObserveTool(Provider())
    registry.register(tool)
    pipeline = ToolPipeline(registry, factory, clock, SequenceIdFactory())
    active = run()
    async with factory() as uow:
        await uow.runs.create(active)
    call = ToolCallItem(
        call_id="observe", item_index=0, name=tool.spec.name, arguments={}, raw_arguments="{}"
    )
    results = await pipeline.dispatch(
        run=active,
        checkpoint=_checkpoint(active, []),
        tool_calls=[call],
        principal=principal(),
        step=Step(run_id=active.id, step_number=1, started_at=NOW),
        agent=agent().model_copy(update={"enabled_tools": [tool.spec.name]}),
        token=RunCancellationToken(clock, None),
    )
    async with factory() as uow:
        events = await uow.events.list_after(active.session_id, 0, principal())
    event = next(
        e for e in reversed(events) if e.event_type in {"tool.call.completed", "tool.call.failed"}
    )
    report = event.payload.get("browser_diagnostics")
    assert report is not None, "browser execution has no phase diagnostic evidence"
    assert report["version"] == 1
    assert report["phases"]
    assert "secret-cookie-canary" not in json.dumps(event.payload)
    assert conversation_items(event)[0] == results[0].model_copy(
        update={"source_event_sequence": event.sequence}
    )
    assert "browser_diagnostics" not in str(results[0].content)


async def test_collectors_are_isolated_bounded_and_reject_untrusted_metadata() -> None:
    from agent_core.domain.browser_diagnostics import (
        admit_browser_diagnostics,
        browser_phase,
        collect_browser_diagnostics,
    )

    async def collect(phase: Phase) -> BrowserDiagnostics:
        with collect_browser_diagnostics(clock=asyncio.get_running_loop().time) as collector:
            for _ in range(70):
                with browser_phase(phase):
                    await asyncio.sleep(0)
            before = collector.snapshot()
            admit_browser_diagnostics(
                json.dumps(
                    {
                        "version": 1,
                        "elapsed_ms": 1,
                        "truncated": False,
                        "phases": [
                            {
                                "phase": "dispatch",
                                "placement": "runtime",
                                "outcome": "failed",
                                "failure": "secret-cookie-canary",
                                "elapsed_ms": 1,
                            }
                        ],
                    }
                )
            )
            assert collector.snapshot().phases == before.phases
            return collector.snapshot()

    one, two = await asyncio.gather(collect("navigation"), collect("cleanup"))
    assert one.truncated and two.truncated
    assert len(one.phases) == len(two.phases) == 64
    assert {phase.phase for phase in one.phases} == {"navigation"}
    assert {phase.phase for phase in two.phases} == {"cleanup"}
    with collect_browser_diagnostics(
        clock=asyncio.get_running_loop().time, enabled=False
    ) as collector:
        with browser_phase("dispatch"):
            pass
        assert not collector.snapshot().phases


async def test_phase_cancellation_and_failure_never_copy_exception_text() -> None:
    from agent_core.domain.browser import BrowserProviderError
    from agent_core.domain.browser_diagnostics import browser_phase, collect_browser_diagnostics

    with collect_browser_diagnostics(clock=asyncio.get_running_loop().time) as collector:
        with pytest.raises(BrowserProviderError), browser_phase("dispatch"):
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
        with pytest.raises(asyncio.CancelledError), browser_phase("observation"):
            raise asyncio.CancelledError("secret-cookie-canary")
    report = collector.snapshot()
    assert report.phases[0].failure == "outcome_unknown"
    assert report.phases[1].outcome == "cancelled"
    assert "secret-cookie-canary" not in report.model_dump_json()


async def test_hosted_response_includes_validated_phases_on_success_and_refusal(
    tmp_path: Path,
) -> None:
    from tests.unit.test_browser_dispatch_constraint import SERVICE_AUTH, _app, leased

    sessions, _runtime, lease = await leased(tmp_path)
    async with _app(sessions) as client:
        for ref, code in [(lease, 200), ("missing-lease-" * 4, 409)]:
            response = await client.post(
                "/v1/browser-sessions:observe",
                headers={"Authorization": f"Bearer {SERVICE_AUTH}"},
                json={"lease_ref": ref},
            )
            assert response.status_code == code
            report = response.json().get("browser_diagnostics")
            assert report is not None, "hosted service discarded phase diagnostics"
            assert report["version"] == 1 and report["phases"]
            assert ref not in json.dumps(report)


@pytest.mark.parametrize("malformed", [False, True])
async def test_hosted_client_admits_only_valid_metadata_without_affecting_observation(
    malformed: bool,
) -> None:
    import httpx

    from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
    from agent_core.adapters.credentials import MappingCredentialResolver
    from agent_core.domain.browser_diagnostics import collect_browser_diagnostics

    def serve(request: httpx.Request) -> httpx.Response:
        observation = (
            FakeBrowserProvider()._observation("https://example.org/").model_dump(mode="json")
        )
        phase = {
            "phase": "observation",
            "placement": "runtime",
            "outcome": "completed",
            "failure": "none",
            "elapsed_ms": 1,
        }
        if malformed:
            phase["secret"] = "secret-cookie-canary"
        observation["browser_diagnostics"] = {
            "version": 1,
            "elapsed_ms": 1,
            "phases": [phase],
            "truncated": False,
        }
        return httpx.Response(200, json=observation)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        client = HostedBrowserSessionControlPlane(
            base_url="https://browser.example.org",
            credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
            client=http,
        )
        with collect_browser_diagnostics(clock=asyncio.get_running_loop().time) as collector:
            observed = await client.observe("opaque-lease" * 4)
        assert observed.revision == "revision-1"
        report = collector.snapshot()
        assert len(report.phases) == (0 if malformed else 1)
        assert "secret-cookie-canary" not in report.model_dump_json()


async def test_tool_deadline_after_browser_dispatch_is_uncertain_and_not_retryable() -> None:
    from agent_core.domain.policies import PolicyDecisionType
    from agent_core.tools.browser_act import BrowserActTool
    from tests.unit.test_tool_pipeline_recovery_policy import _decision, _Engine

    class Provider(FakeBrowserProvider):
        async def observe(self) -> BrowserObservation:
            await asyncio.sleep(10)
            return await super().observe()

    clock, factory = await _factory()
    provider = Provider()
    tool = BrowserActTool(provider)
    tool.spec = tool.spec.model_copy(update={"timeout_seconds": 1})
    registry = StaticToolRegistry()
    registry.register(tool)
    pipeline = ToolPipeline(
        registry,
        factory,
        clock,
        SequenceIdFactory(),
        policy=_Engine(_decision(PolicyDecisionType.ALLOW)),
    )
    active = run()
    async with factory() as uow:
        await uow.runs.create(active)
    arguments = {
        "kind": "click",
        "expected_revision": "revision-1",
        "ref": "element-1",
        "postcondition": {"role": "button", "name": "Finished", "timeout_ms": 5000},
    }
    call = ToolCallItem(
        call_id="act",
        item_index=0,
        name=tool.spec.name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )
    await pipeline.dispatch(
        run=active,
        checkpoint=_checkpoint(active, []),
        tool_calls=[call],
        principal=principal(),
        step=Step(run_id=active.id, step_number=1, started_at=NOW),
        agent=agent().model_copy(update={"enabled_tools": [tool.spec.name]}),
        token=RunCancellationToken(clock, None),
    )
    async with factory() as uow:
        invocation = (await uow.invocations.list_for_run(active.id, principal()))[0]
    assert len(provider.actions) == 1 and invocation.effect_sent_at is not None
    assert invocation.status.value == "UNCERTAIN"
    assert invocation.outcome is not None and not invocation.outcome.retryable
    assert invocation.outcome.reason_code == "tool.browser.outcome_unknown"
