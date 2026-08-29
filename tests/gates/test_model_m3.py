"""Milestone 3 structural and normalized-stream hard gates."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.domain.messages import (
    ModelCompletedEvent,
    ModelEvent,
    ModelTurn,
    ModelUsage,
    StopReason,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    UsageEvent,
)
from agent_core.model.streaming import ModelStreamError, validated_stream
from scripts.architecture_checks import architecture_errors

ROOT = Path(__file__).resolve().parents[2]
ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000311")
RUN_ID = UUID("00000000-0000-0000-0000-000000000312")


def text(sequence: int, item_index: int, value: str = "ok") -> TextDeltaEvent:
    return TextDeltaEvent(
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        step_number=1,
        sequence=sequence,
        item_index=item_index,
        text=value,
    )


def tool(
    sequence: int,
    item_index: int,
    *,
    call_id: str = "call-1",
    name: str = "math.calculate",
) -> ToolCallDeltaEvent:
    return ToolCallDeltaEvent(
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        step_number=1,
        sequence=sequence,
        item_index=item_index,
        call_id=call_id,
        name=name,
        arguments_delta="{}",
    )


def completed(sequence: int) -> ModelCompletedEvent:
    turn = ModelTurn(usage=ModelUsage(), stop_reason=StopReason.END_TURN)
    return ModelCompletedEvent(
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        step_number=1,
        sequence=sequence,
        turn=turn,
        stop_reason=StopReason.END_TURN,
    )


async def source(*events: ModelEvent) -> AsyncIterator[ModelEvent]:
    for event in events:
        yield event


async def consume(*events: ModelEvent) -> None:
    typed = source(*events)
    async for _event in validated_stream(typed):
        pass


async def test_stream_invariant_1_sequence_starts_at_zero_and_is_gapless() -> None:
    with pytest.raises(ModelStreamError, match="sequence"):
        await consume(text(1, 0), completed(2))


async def test_stream_invariant_2_exactly_one_terminal_event_ends_the_stream() -> None:
    with pytest.raises(ModelStreamError, match="without exactly one terminal"):
        await consume(text(0, 0))
    with pytest.raises(ModelStreamError, match="followed the terminal"):
        await consume(completed(0), completed(1))


async def test_stream_invariant_3_item_deltas_are_contiguous() -> None:
    with pytest.raises(ModelStreamError, match="contiguous"):
        await consume(text(0, 0), text(1, 1), text(2, 0), completed(3))


async def test_stream_invariant_4_tool_identity_is_stable_and_present() -> None:
    with pytest.raises(ModelStreamError, match="identity changed"):
        await consume(tool(0, 0), tool(1, 0, call_id="call-2"), completed(2))
    with pytest.raises(ModelStreamError, match="identity was absent"):
        missing = tool(0, 0).model_copy(update={"call_id": None})
        await consume(missing, completed(1))


async def test_stream_invariant_5_usage_is_advisory_and_never_terminal() -> None:
    invalid = UsageEvent.model_construct(
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        step_number=1,
        sequence=0,
        usage=ModelUsage(),
        is_final=True,
    )
    with pytest.raises(ModelStreamError, match="provisional"):
        await consume(invalid, completed(1))


@pytest.mark.parametrize(
    "credential",
    (
        "sk-" + "not-a-real-provider-key",
        "Bearer " + "a1b2c3d4",
        "Bearer synthetic-" + "test-value-12345678901234567890",
    ),
)
async def test_stream_invariant_6_credential_shaped_content_is_rejected(
    credential: str,
) -> None:
    """Reject every governed credential shape before a normalized event escapes."""

    with pytest.raises(ModelStreamError, match="credential-shaped"):
        await consume(text(0, 0, credential), completed(1))


async def test_all_six_stream_invariants() -> None:
    """Give the registry one target that executes every individually named invariant."""

    await test_stream_invariant_1_sequence_starts_at_zero_and_is_gapless()
    await test_stream_invariant_2_exactly_one_terminal_event_ends_the_stream()
    await test_stream_invariant_3_item_deltas_are_contiguous()
    await test_stream_invariant_4_tool_identity_is_stable_and_present()
    await test_stream_invariant_5_usage_is_advisory_and_never_terminal()
    await test_stream_invariant_6_credential_shaped_content_is_rejected(
        "sk-" + "not-a-real-provider-key"
    )


@pytest.mark.parametrize(
    "documentation",
    (
        "Send Authorization: <token> only after approval.",
        "Compare OAuth 2.0 Bearer authentication with API keys.",
        "Compare Bearer token-based authorization with session cookies.",
        "Use Bearer YOUR_API_TOKEN_HERE in the quickstart example.",
    ),
)
async def test_authorization_documentation_is_not_a_credential(documentation: str) -> None:
    """Keep ordinary authentication prose outside the opaque-value detector."""

    events = [
        event async for event in validated_stream(source(text(0, 0, documentation), completed(1)))
    ]
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [documentation]


async def test_provider_key_detector_does_not_start_inside_an_ordinary_url_word() -> None:
    """Keep a production-derived ``risk-...`` URL outside the provider-key detector."""

    url = "https://valueaddvc.com/blog/anthropic-pentagon-supply-chain-risk-shows-why-this-matters"
    events = [event async for event in validated_stream(source(text(0, 0, url), completed(1)))]

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [url]


def test_provider_sdks_are_isolated_to_their_own_adapter_modules() -> None:
    assert architecture_errors(ROOT) == []
    adapter_dir = ROOT / "src/agent_core/adapters/models"
    imports: dict[str, set[str]] = {}
    for path in adapter_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".", 1)[0])
        imports[path.name] = roots
    assert "openai" in imports["openai_responses.py"]
    assert "anthropic" not in imports["openai_responses.py"]
    assert "anthropic" in imports["anthropic_messages.py"]
    assert "openai" not in imports["anthropic_messages.py"]
    assert not ({"openai", "anthropic"} & imports["chat_completions.py"])


def test_live_provider_smokes_execute_as_clean_credential_skips() -> None:
    environment = os.environ.copy()
    environment["RUN_LIVE_MODEL_TESTS"] = "1"
    environment.pop("VEETBOT_OPENAI_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("ANTHROPIC_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/live/test_model_providers_m3.py::test_vendor_one_call_smoke",
            "-q",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 skipped" in result.stdout
