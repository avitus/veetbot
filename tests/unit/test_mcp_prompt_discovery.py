"""Discovery must not render parameterized prompts without their required inputs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import Prompt, PromptArgument

from tests.unit.test_mcp_sdk_lifecycle import stdio_client


@pytest.mark.parametrize("required", [True, False, None])
async def test_discovery_only_renders_prompts_that_accept_empty_arguments(
    monkeypatch: pytest.MonkeyPatch, required: bool | None
) -> None:
    """Required company inputs never become empty remote requests, even on rediscovery."""
    prompt = Prompt(
        name="meeting_prep_memo",
        arguments=[PromptArgument(name="company", required=required)],
    )
    remote = SimpleNamespace(
        server_capabilities=SimpleNamespace(prompts=object(), resources=None),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
        list_prompts=AsyncMock(
            return_value=SimpleNamespace(prompts=[prompt, Prompt(name="general_guidance")])
        ),
        get_prompt=AsyncMock(
            return_value=SimpleNamespace(
                messages=[SimpleNamespace(content=SimpleNamespace(text="Guidance"))]
            )
        ),
    )
    client = stdio_client()
    monkeypatch.setattr(client, "_connected", lambda: remote)
    for _ in range(2):
        remote.get_prompt.reset_mock()
        discovery = await client.discover()
        expected = ["general_guidance"] if required else ["meeting_prep_memo", "general_guidance"]
        assert [call.args[0] for call in remote.get_prompt.await_args_list] == expected
        assert [item.name for item in discovery.prompts] == expected
        assert all(call.args[1] == {} for call in remote.get_prompt.await_args_list)
