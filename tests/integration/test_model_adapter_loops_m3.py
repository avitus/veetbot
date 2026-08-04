"""Full malformed-argument recovery loop over every Milestone 3 adapter."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.models.recorded import (
    RecordedEventSource,
    RecordedFixture,
    RecordedModelProvider,
)
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.ports.models import ModelProvider
from tests.contract.model_fixtures import (
    ScriptedRawSource,
    anthropic_text_events,
    anthropic_tool_events,
    chat_text_events,
    chat_tool_events,
    openai_text_events,
    openai_tool_events,
)
from tests.contract.support import NOW

SETTINGS = Settings(
    database_url="postgresql+asyncpg://localhost/unused",
    deployment_mode=DeploymentMode.DEVELOPMENT,
    auth_mode=AuthMode.DEV,
    auth_token=None,
    sandbox=SandboxMechanism.FAKE,
    config_dir=None,
    credentials=MappingProxyType({}),
    interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
)


def real_cases() -> list[tuple[str, str, ModelProvider]]:
    openai_streams = [openai_tool_events('{"expression":'), openai_text_events("recovered")]
    recorded_fixture = RecordedFixture(
        schema_version=1,
        provider_api="responses",
        streams=openai_streams,
    )
    return [
        (
            "recorded",
            "balanced",
            RecordedModelProvider(
                OpenAIResponsesProvider(event_source=RecordedEventSource(recorded_fixture))
            ),
        ),
        (
            "openai",
            "balanced",
            OpenAIResponsesProvider(event_source=ScriptedRawSource(openai_streams)),
        ),
        (
            "anthropic",
            "flagship",
            AnthropicMessagesProvider(
                event_source=ScriptedRawSource(
                    [
                        anthropic_tool_events('{"expression":'),
                        anthropic_text_events("recovered"),
                    ]
                )
            ),
        ),
        (
            "chat_completions",
            "local",
            ChatCompletionsProvider(
                base_url="http://127.0.0.1:11434/v1",
                event_source=ScriptedRawSource(
                    [
                        chat_tool_events('{"expression":'),
                        chat_text_events("recovered"),
                    ]
                ),
            ),
        ),
    ]


@pytest.mark.parametrize("adapter_name,policy,provider", real_cases())
async def test_malformed_arguments_return_an_error_and_loop_continues(
    adapter_name: str,
    policy: str,
    provider: ModelProvider,
) -> None:
    override_name = "openai" if adapter_name == "recorded" else adapter_name
    async with build(
        settings=SETTINGS,
        fixed_clock_at=NOW,
        sequential_ids=True,
        model_policy=policy,
        model_provider_overrides={override_name: provider},
    ) as composition:
        run_id = await composition.runs.submit("recover from malformed tool arguments")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "recovered"
    assert any(
        event.event_type == "tool.call.failed"
        and event.payload.get("reason_code") == "tool.arguments_invalid"
        for event in events
    )


async def test_fake_malformed_arguments_return_an_error_and_loop_continues() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn.model_validate(
                {
                    "tool_calls": [
                        {
                            "name": "math.calculate",
                            "arguments": '{"expression":',
                            "call_id": "malformed-fake",
                        }
                    ]
                }
            ),
            ScriptedTurn(text="recovered"),
        ]
    )
    async with build(
        settings=SETTINGS,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        run_id = await composition.runs.submit("recover from malformed fake arguments")
        run = await composition.runs.wait_terminal(run_id)
    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "recovered"
