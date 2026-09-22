"""The tool-call budget ends research, never the answer (ADR-0078 amendment)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings, load_settings
from agent_core.domain.messages import (
    FakeModelScript,
    ModelRequest,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolResultItem,
)
from agent_core.domain.runs import FailureReason, RunLimits, RunStatus
from agent_core.domain.tools import ToolOutcome
from tests.contract.support import NOW

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "src" / "agent_core"


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/runtime",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
    )


def _calc(expression: str, call_id: str) -> ScriptedToolCall:
    return ScriptedToolCall(
        name="math.calculate", arguments={"expression": expression}, call_id=call_id
    )


def _tool_turn(*calls: ScriptedToolCall) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=list(calls), stop_reason=StopReason.TOOL_USE)


def _requests(composition: object) -> list[ModelRequest]:
    provider = composition.executor._model_provider  # type: ignore[attr-defined]
    assert isinstance(provider, FakeModelProvider)
    return provider.requests


def _control_texts(request: ModelRequest) -> list[str]:
    texts: list[str] = []
    for item in request.conversation:
        for part in getattr(item, "content", []):
            if isinstance(part, TextPart) and part.text.startswith("Runtime control:"):
                texts.append(part.text)
    return texts


@pytest.mark.asyncio
async def test_over_budget_batch_is_trimmed_and_the_run_still_answers() -> None:
    """Calls past the limit are refused before dispatch; the model still answers."""

    script = FakeModelScript(
        turns=[
            _tool_turn(_calc("1 + 1", "a"), _calc("2 + 2", "b")),
            _tool_turn(_calc("3 + 3", "c"), _calc("4 + 4", "d"), _calc("5 + 5", "e")),
            ScriptedTurn(text="the table, from what was found"),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        limits=RunLimits(max_steps=6, max_model_calls=6, max_tool_calls=3),
    ) as composition:
        run_id = await composition.runs.submit("build the table")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
        requests = _requests(composition)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.tool_call_count == 3
    kinds = [event.event_type for event in events]
    assert kinds.count("tool.call.proposed") == 3
    assert kinds.count("tool.call.completed") == 3

    # The model saw a refusal for the two calls that did not fit, and its next
    # request carried the synthesis-only control for the tool-call dimension.
    refused = [
        ToolOutcome.model_validate_json(part.text)
        for item in requests[2].conversation
        if isinstance(item, ToolResultItem) and item.is_error
        for part in item.content
        if isinstance(part, TextPart)
    ]
    assert [outcome.reason_code for outcome in refused] == [
        "tool.budget_exhausted",
        "tool.budget_exhausted",
    ]
    assert any("tool_calls budget" in text for text in _control_texts(requests[2]))


@pytest.mark.asyncio
async def test_a_run_at_the_tool_limit_gets_one_final_synthesis_turn() -> None:
    """Reaching max_tool_calls exactly is not a failure while an answer is owed."""

    script = FakeModelScript(
        turns=[
            _tool_turn(_calc("1 + 1", "a")),
            ScriptedTurn(text="two"),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        limits=RunLimits(max_steps=4, max_model_calls=4, max_tool_calls=1),
    ) as composition:
        run_id = await composition.runs.submit("one call then answer")
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.tool_call_count == 1
    assert any("tool_calls budget" in text for text in _control_texts(requests[1]))


@pytest.mark.asyncio
async def test_tool_call_inside_the_tool_call_reserve_fails_closed() -> None:
    """The tool-call reserve behaves like the other reserve dimensions."""

    script = FakeModelScript(
        turns=[
            _tool_turn(_calc("1 + 1", "a"), _calc("2 + 2", "b")),
            _tool_turn(_calc("3 + 3", "c")),
            ScriptedTurn(text="never reached"),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        limits=RunLimits(
            max_steps=6,
            max_model_calls=6,
            max_tool_calls=4,
            synthesis_reserve_tool_calls=2,
        ),
    ) as composition:
        run_id = await composition.runs.submit("research within the reserve")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)

    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.reason is FailureReason.BUDGET_EXCEEDED
    assert run.failure.details == {"synthesis_reserve": "tool_calls"}
    assert run.tool_call_count == 2
    assert [event.event_type for event in events].count("tool.call.proposed") == 2


def test_tool_call_reserve_must_fit_inside_the_limit() -> None:
    with pytest.raises(ValueError, match="tool-call reserve"):
        RunLimits(max_tool_calls=4, synthesis_reserve_tool_calls=4)


def test_interactive_defaults_leave_room_for_a_final_answer(tmp_path: Path) -> None:
    loaded = yaml.safe_load((PACKAGE_ROOT / "runtime/limits.yaml").read_text(encoding="utf-8"))
    assert loaded["run_defaults"] == {
        "max_steps": 32,
        "max_model_calls": 24,
        "max_tool_calls": 64,
        "synthesis_reserve_model_calls": 2,
        "synthesis_reserve_tool_calls": 4,
    }

    settings = load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://localhost/runtime",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "dev",
            "SANDBOX_MECHANISM": "fake",
            "AGENT_CONFIG_DIR": str(tmp_path),
            "OPENAI_MODEL": "",
        }
    )
    from agent_core.bootstrap import default_run_limits

    limits = default_run_limits(settings)
    assert limits == RunLimits(
        max_steps=32,
        max_model_calls=24,
        max_tool_calls=64,
        synthesis_reserve_model_calls=2,
        synthesis_reserve_tool_calls=4,
    )
    assert limits.max_cost is None and limits.synthesis_reserve_cost == Decimal("0")
