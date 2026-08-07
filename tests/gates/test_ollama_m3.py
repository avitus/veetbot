"""Credential-free Ollama-compatible calculator gate."""

from types import MappingProxyType

from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from tests.contract.model_fixtures import (
    ScriptedRawSource,
    chat_text_events,
    chat_tool_events,
)
from tests.contract.support import NOW


async def test_ollama_wire_calculator_has_zero_network_cost() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
    )
    source = ScriptedRawSource(
        [chat_tool_events('{"expression":"17 * 23"}'), chat_text_events("391")]
    )
    provider = ChatCompletionsProvider(
        base_url="http://127.0.0.1:11434/v1",
        event_source=source,
    )
    async with build(
        settings=settings,
        fixed_clock_at=NOW,
        sequential_ids=True,
        model_policy="local",
        model_provider_overrides={"chat_completions": provider},
    ) as composition:
        run_id = await composition.runs.submit("What is 17 multiplied by 23?")
        run = await composition.runs.wait_terminal(run_id)
    assert run.final_message == "391"
    assert run.usage.cost == 0
    assert run.model_call_count == 2
    assert len(source.requests) == 2
    assert source.requests[0]["model"] == "qwen3:8b"
