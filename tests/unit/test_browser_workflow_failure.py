"""Positive failure evidence must survive the reviewed workflow boundary."""

from copy import deepcopy

import pytest

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


@pytest.mark.parametrize("mode", ["observe", "action", "lost_response"])
@pytest.mark.parametrize(
    "evidence,declared,expected",
    [
        ("Report rejected", True, "observed_failure"),
        ("Report ready\nReport rejected", True, "unverified"),
        ("Pending", True, "unverified"),
        ("Report rejected", False, "unverified"),
    ],
)
async def test_workflow_reports_only_positive_declared_failure(
    monkeypatch: pytest.MonkeyPatch, mode: str, evidence: str, declared: bool, expected: str
) -> None:
    catalog = deepcopy(CATALOG)
    steps = catalog["recipes"][0]["steps"]
    condition = steps[1]["postcondition"]
    if declared:
        condition["failure_evidence"] = {"kind": "text", "text": "Report rejected"}
    if mode == "observe":
        steps[1] = {"kind": "observe", "postcondition": condition}
    # A negative outcome must not reach any later action.
    steps.append(deepcopy(CATALOG["recipes"][0]["steps"][1]))
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            catalog
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )

    class Provider(FakeBrowserProvider):
        def _observation(self, url: str) -> BrowserObservation:
            observed = super()._observation(url)
            return observed.model_copy(
                update={"text": evidence if self.actions or mode == "observe" else "Pending"}
            )

        async def act(
            self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
        ) -> BrowserObservation:
            result = await super().act(action, constraint=constraint)
            if mode == "lost_response":
                raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
            return result

    provider = Provider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported the observed outcome.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        assert len(approvals) == int(mode != "observe")
        assert not provider.actions
        if approvals:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        assert not await app.approvals.list_pending(run_id=run_id)
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        assert saved.working_state["browser_workflow"]["status"] == expected
        assert saved.working_state["browser_workflow"]["completed_steps"] == 1
        assert saved.working_state["browser_workflow_report_only"]
    assert len(provider.actions) == int(mode != "observe")
    if mode == "action" and expected == "observed_failure":
        assert provider.observation_count == 1, "positive failure needs no reconciliation read"
