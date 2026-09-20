"""Composition-root selection for the typed-judgment provider."""

from __future__ import annotations

import logging

import pytest

from agent_core.adapters.judgment import FakeJudgmentProvider, TypeSafeJudgmentProvider
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.judgment import (
    JudgmentFailure,
    JudgmentProviderError,
    JudgmentRequest,
    NoulQuestion,
)
from tests.unit.test_config import base_environment

CREDENTIAL = "synthetic-typesafe-credential"
REQUEST = JudgmentRequest(
    state={"text": "synthetic"}, questions={"holds": NoulQuestion(instructions="Does it hold?")}
)


def _environment(**overrides: str) -> dict[str, str]:
    return {**base_environment(), "SANDBOX_MECHANISM": "fake", **overrides}


async def test_no_judgment_provider_is_composed_until_the_selector_names_one() -> None:
    async with build(settings=load_settings(_environment())) as composition:
        assert composition.judgment_provider is None

    # A credential alone enables nothing.
    credentialed = load_settings(_environment(TYPESAFE_API_KEY=CREDENTIAL))
    async with build(settings=credentialed) as composition:
        assert composition.judgment_provider is None


async def test_the_typesafe_selector_composes_the_typesafe_provider() -> None:
    settings = load_settings(
        _environment(JUDGMENT_PROVIDER="typesafe", TYPESAFE_API_KEY=CREDENTIAL)
    )

    async with build(settings=settings) as composition:
        assert isinstance(composition.judgment_provider, TypeSafeJudgmentProvider)


async def test_a_selector_without_a_credential_warns_and_never_refuses_startup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = load_settings(_environment(JUDGMENT_PROVIDER="typesafe"))

    with caplog.at_level(logging.WARNING, logger="agent_core.bootstrap"):
        async with build(settings=settings) as composition:
            provider = composition.judgment_provider
            assert isinstance(provider, TypeSafeJudgmentProvider)
            with pytest.raises(JudgmentProviderError) as caught:
                await provider.judge(REQUEST)

    assert caught.value.reason is JudgmentFailure.AUTH_FAILED
    warnings = [
        record for record in caplog.records if record.message == "judgment_credential_missing"
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert getattr(warnings[0], "selector", None) == "JUDGMENT_PROVIDER"
    assert getattr(warnings[0], "credential", None) == "TYPESAFE_API_KEY"


async def test_a_composed_credential_raises_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    settings = load_settings(
        _environment(JUDGMENT_PROVIDER="typesafe", TYPESAFE_API_KEY=CREDENTIAL)
    )

    with caplog.at_level(logging.WARNING, logger="agent_core.bootstrap"):
        async with build(settings=settings):
            pass

    assert not [r for r in caplog.records if r.message == "judgment_credential_missing"]
    assert CREDENTIAL not in caplog.text


async def test_an_override_is_composed_and_closed_on_shutdown() -> None:
    fake = FakeJudgmentProvider([])

    async with build(settings=load_settings(_environment()), judgment_provider_override=fake) as c:
        assert c.judgment_provider is fake
        assert fake.closed is False

    assert fake.closed is True
