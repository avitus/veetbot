"""Unavailable source recovery never calls the model or overwrites an existing draft."""

from dataclasses import replace
from typing import Any

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _current_mail_factory, _page, _profile, _seed_draft
from tests.unit.test_email_scan_cost import expire, record_model_requests


@pytest.mark.parametrize("problem", ["unavailable", "partial", "wrong_account"])
async def test_failed_source_recovery_is_actionable_and_preserves_the_draft(
    problem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await _current_mail_factory()
    reads: list[str] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            reads.append(name)
            if name == "get_profile" and problem == "wrong_account":
                return MCPCallResult(
                    structured={**_profile(), "email_address": "foreign@example.test"}
                )
            if name == "get_thread_page":
                if problem == "unavailable":
                    return MCPCallResult(is_error=True, content=("PRIVATE_UPSTREAM_BODY",))
                if problem == "partial":
                    value = _page()
                    value.update(complete=False, next_page_token="another-page")
                    return MCPCallResult(structured=value)
            return await original(name, arguments)

        client.call_tool = call  # type: ignore[method-assign]
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[ScriptedTurn(text='{"body":"Must not replace edits"}')]),
        mcp_client_factory=factory,
    ) as app:
        thread, draft = await _seed_draft(app)
        await expire(app, thread.id)
        requests = record_model_requests(monkeypatch)
        operation = await app.services.email.submit_task(
            app.principal, kind="draft", thread_id=thread.id
        )
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.FAILED and run.failure is not None
        assert run.failure.reason.value == "dependency_unavailable"
        assert "preserved" in run.failure.message
        assert requests == []
        preserved = await app.services.email.draft(app.principal, draft.id)
        assert preserved.body == draft.body and preserved.revision == draft.revision
        assert reads.count("get_thread_page") <= 10
        assert not {"modify_labels", "send_message"}.intersection(reads)
        assert "PRIVATE_UPSTREAM_BODY" not in run.failure.model_dump_json()
