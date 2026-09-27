"""A deployment must not disguise an unavailable provider pin as a crash."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.bootstrap import build
from agent_core.config import PACKAGE_ROOT
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.runs import FailureReason, RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from tests.contract.model_fixtures import ScriptedRawSource, openai_text_events, openai_tool_events
from tests.gates.test_runtime_m4 import settings


@pytest.mark.parametrize("registry_changed", [False, True])
async def test_approval_resume_across_provider_registry_change(
    tmp_path: Path, registry_changed: bool
) -> None:
    events = openai_tool_events('{"destination":"demo","content":"approved text"}')
    events[0]["item"]["name"] = "demo.external_write"
    source = ScriptedRawSource([events, openai_text_events("done")])
    async with build(
        settings=settings(tmp_path),
        model_policy="astra",
        model_provider_overrides={"openai": OpenAIResponsesProvider(event_source=source)},
    ) as app:
        run_id = await app.runs.submit("write the approved text")
        parked = await app.runs.get(run_id)
        assert parked.status is RunStatus.WAITING_FOR_APPROVAL
        assert parked.provider_pin is not None
        original_pin = parked.provider_pin.model_copy(deep=True)

        # Rebuild the router as a new worker does. The production incident
        # changed only OpenAI's cache capability and breakpoint limit.
        overlay = tmp_path / "overlay"
        if registry_changed:
            path = overlay / "models/providers/openai.yaml"
            path.parent.mkdir(parents=True)
            profile = yaml.safe_load((PACKAGE_ROOT / "models/providers/openai.yaml").read_text())
            profile["capabilities"]["explicit_cache_control"] = False
            profile["limits"]["max_cache_breakpoints"] = 0
            path.write_text(yaml.safe_dump(profile))
        app.executor._model_router = StaticModelRouter(
            ProviderRegistry.load(
                PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS, overlay_root=overlay
            ),
            app.clock,
        )
        approval = (await app.approvals.list_pending(run_id=run_id))[0]
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        finished = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, app.principal)
            checkpoint = await uow.checkpoints.latest(run_id)
        assert finished.provider_pin == original_pin
        assert checkpoint is not None
        assert checkpoint.provider_pin == original_pin
        assert len(invocations) == 1
        if registry_changed:
            assert finished.status is RunStatus.FAILED
            assert finished.failure is not None
            assert finished.failure.reason is FailureReason.MODEL_PERMANENT_ERROR
            assert finished.failure.error_class == "ProviderPinUnavailableError"
            assert "configuration changed" in finished.failure.message
            assert "new message" in finished.failure.message
            assert len(source.requests) == 1
            assert invocations[0].status is ToolInvocationStatus.WAITING_FOR_APPROVAL
        else:
            assert finished.status is RunStatus.COMPLETED
            assert finished.final_message == "done"
            assert len(source.requests) == 2
            assert invocations[0].status is ToolInvocationStatus.SUCCEEDED
