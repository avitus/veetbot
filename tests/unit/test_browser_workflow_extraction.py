"""Reviewed extraction uses a fresh read rather than earlier conversation evidence."""

from copy import deepcopy

import pytest

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.browser import BrowserObservation, BrowserProviderError
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_extraction import extracted_page
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment

EXTRACT = {
    "kind": "extract",
    "collection_kind": "table",
    "fields": [{"name": "item", "column": 0, "type": "string"}],
}


@pytest.mark.parametrize(
    "mode",
    ["normal", "read_error", "read_stale", "read_auth", "read_foreign", "extract_stale", "missing"],
)
async def test_workflow_refreshes_evidence_before_extracting(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    catalog = deepcopy(CATALOG)
    catalog["recipes"][0]["steps"] = [catalog["recipes"][0]["steps"][0], EXTRACT]
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
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[BrowserExtractionRequest] = []

        async def observe(self) -> BrowserObservation:
            self.observation_count += 1
            reason = {
                "read_error": "provider_unavailable",
                "read_stale": "page_changed",
                "read_auth": "needs_user",
            }.get(mode)
            if reason:
                raise BrowserProviderError(f"tool.browser.{reason}", retryable=False)
            url = (
                "https://foreign.example/account"
                if mode == "read_foreign"
                else "https://example.org/account"
            )
            return self._observation(url).model_copy(update={"revision": "fresh-revision"})

        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            self.requests.append(request)
            if mode == "extract_stale" or request.expected_revision != "fresh-revision":
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            if mode == "missing":
                return self._observation("https://example.org/account")
            return extracted_page(request)

    provider = Provider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Checked available rows.")]),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        assert (await app.runs.get(run_id)).status is (
            RunStatus.WAITING_FOR_USER if mode == "read_auth" else RunStatus.COMPLETED
        )
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.working_state["browser_workflow"]
        expected = (
            "running" if mode == "read_auth" else "verified" if mode == "normal" else "unverified"
        )
        assert state["status"] == expected, "extraction reused stale conversation evidence"
        assert state["completed_steps"] == (2 if mode == "normal" else 1)
        if mode == "normal":
            assert state["calls"] == 3
        if mode != "read_auth":
            assert checkpoint.working_state["browser_workflow_report_only"]
        assert not await app.approvals.list_pending(run_id=run_id)
        if mode == "read_auth":
            await app.services.runs.cancel(app.principal, run_id)
            assert (await app.runs.get(run_id)).status is RunStatus.CANCELLED
    assert len(provider.requests) == int(mode in {"normal", "extract_stale", "missing"})
    if provider.requests:
        assert provider.requests[0].expected_revision == "fresh-revision"
    assert provider.observation_count == (2 if mode in {"read_stale", "extract_stale"} else 1)
    assert not provider.actions
