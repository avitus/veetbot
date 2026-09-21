"""Optional one-call smoke check against the credentialed judgment vendor.

The state is synthetic. This is not gate evidence: the contract suite binds the
gates, and this only confirms the documented wire shapes still hold.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.judgment import TypeSafeJudgmentProvider
from agent_core.domain.judgment import (
    ChoiceAnswer,
    ChoiceOption,
    ChoiceQuestion,
    JudgmentRequest,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
)
from agent_core.domain.messages import CostSource

NOW = datetime(2026, 9, 20, tzinfo=UTC)
STATE_MARKER = "synthetic-live-state-marker"


def live_enabled() -> None:
    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set RUN_LIVE_MODEL_TESTS=1 to enable provider smoke tests")


async def test_typesafe_one_call_smoke(caplog: pytest.LogCaptureFixture) -> None:
    live_enabled()
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        pytest.skip("TYPESAFE_API_KEY is absent")
    provider = TypeSafeJudgmentProvider(
        credentials=MappingCredentialResolver({"typesafe": api_key}),
        clock=FixedClock(NOW),
    )
    request = JudgmentRequest(
        state={
            "conversation": {
                "title": "Fixing the carburetor on the old motorcycle",
                "marker": STATE_MARKER,
            }
        },
        questions={
            "folder": ChoiceQuestion(
                instructions="Which folder does the conversation in `conversation` belong in?",
                options=(
                    ChoiceOption(
                        key="f0",
                        description="Motorcycle restoration",
                        examples=("Rebuilding the front forks",),
                    ),
                    ChoiceOption(key="f1", description="Tax paperwork"),
                    ChoiceOption(key="none", description="None of the listed folders fits."),
                ),
            ),
            "vehicle": NoulQuestion(
                instructions="Does `conversation.title` concern a vehicle?",
                true_when="It names or describes a vehicle or a vehicle part.",
                false_when="It concerns anything else.",
            ),
            "technical": ScoreQuestion(
                instructions="How technical is the subject of `conversation.title`?",
                levels=("Not technical", "Somewhat technical", "Deeply technical"),
            ),
        },
    )

    with caplog.at_level(logging.DEBUG):
        try:
            result = await provider.judge(request)
        finally:
            await provider.close()

    folder, vehicle, technical = (
        result.answers["folder"],
        result.answers["vehicle"],
        result.answers["technical"],
    )
    assert isinstance(folder, ChoiceAnswer)
    assert folder.choice in {"f0", "f1", "none"}
    assert set(folder.probabilities) == {"f0", "f1", "none"}
    assert isinstance(vehicle, NoulAnswer)
    assert isinstance(technical, ScoreAnswer)
    assert len(technical.probabilities) == 3
    assert 0 <= technical.score <= 2
    assert result.usage.provider == "typesafe"
    assert result.usage.input_tokens > 0
    assert result.usage.cost_source is CostSource.DOCS_SNAPSHOT
    assert result.usage.cost == Decimal("0.042") * result.usage.input_tokens / 1_000_000
    # Computed first so that a failure can never print the key or the state.
    key_logged = api_key in caplog.text
    state_logged = STATE_MARKER in caplog.text
    assert not key_logged
    assert not state_logged
