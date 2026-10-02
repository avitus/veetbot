"""Hosted providers preserve upload bytes and abandon ambiguous transfers."""

from unittest.mock import AsyncMock

import pytest

from agent_core.domain.browser import BrowserObservation, BrowserProviderError
from agent_core.ports.browser_upload import BrowserImageUploader, upload_browser_image
from tests.contract.support import tool_context
from tests.contract.test_browser_image_session_uploader_contract import ACTION, IMAGE
from tests.unit.test_hosted_browser_provider import FakeSessions, ready_provider


@pytest.mark.parametrize("uncertain", [False, True])
async def test_hosted_image_uploader_contract(
    uncertain: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = FakeSessions()
    sent = AsyncMock(
        return_value=BrowserObservation(url="https://example.org/compose", revision="r2")
    )
    if uncertain:
        sent.side_effect = BrowserProviderError("tool.browser.provider_unavailable", retryable=True)
    monkeypatch.setattr(sessions, "upload", sent, raising=False)
    provider = ready_provider(sessions)
    assert isinstance(provider, BrowserImageUploader)
    await provider.bind_execution(tool_context())
    if uncertain:
        with pytest.raises(BrowserProviderError) as failed:
            await upload_browser_image(provider, ACTION, IMAGE)
        assert failed.value.reason_code == "tool.browser.outcome_unknown"
        assert sessions.closes
    else:
        result = await upload_browser_image(provider, ACTION, IMAGE)
        assert result.revision == "r2"
        await provider.act(ACTION)
        assert sessions.sequence == [2]
    sent.assert_awaited_once()
    assert sent.call_args.args[1:] == (ACTION, IMAGE)
    assert sent.call_args.kwargs == {"sequence": 1}
