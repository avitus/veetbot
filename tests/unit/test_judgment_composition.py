"""Composition-root selection for the typed-judgment provider."""

from __future__ import annotations

import logging
from pathlib import Path

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
from agent_core.folders.grouping import ModelAssistedThreadGrouper
from agent_core.folders.matching import JudgmentFolderMatcher
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


def _folder_environment(tmp_path: Path, *, matching: bool, **overrides: str) -> dict[str, str]:
    overlay = tmp_path / "folders" / "profiles.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        f"schema_version: 1\nproposals:\n  judgment_matching_enabled: {str(matching).lower()}\n",
        encoding="utf-8",
    )
    return _environment(
        AGENT_THREAD_FOLDERS_API_ENABLED="1", AGENT_CONFIG_DIR=str(tmp_path), **overrides
    )


async def test_the_matcher_wraps_the_grouper_only_with_its_knob_and_a_provider(
    tmp_path: Path,
) -> None:
    fake = FakeJudgmentProvider([])
    settings = load_settings(_folder_environment(tmp_path, matching=True))

    async with build(settings=settings, judgment_provider_override=fake) as composition:
        assert composition.folder_proposals is not None
        grouper = composition.folder_proposals._grouper
        assert isinstance(grouper, JudgmentFolderMatcher)
        assert isinstance(grouper._inner, ModelAssistedThreadGrouper)
        assert grouper._judge is fake
        assert grouper._match_threshold == 0.8


async def test_a_provider_without_the_knob_leaves_the_grouper_exactly_as_before(
    tmp_path: Path,
) -> None:
    fake = FakeJudgmentProvider([])
    settings = load_settings(_folder_environment(tmp_path, matching=False))

    async with build(settings=settings, judgment_provider_override=fake) as composition:
        assert composition.folder_proposals is not None
        assert isinstance(composition.folder_proposals._grouper, ModelAssistedThreadGrouper)


async def test_the_knob_without_a_provider_uses_the_inner_grouper_and_says_so_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = load_settings(_folder_environment(tmp_path, matching=True))

    with caplog.at_level(logging.WARNING, logger="agent_core.bootstrap"):
        async with build(settings=settings) as composition:
            assert composition.folder_proposals is not None
            assert isinstance(composition.folder_proposals._grouper, ModelAssistedThreadGrouper)

    unavailable = [r for r in caplog.records if r.message == "folder_judgment_matching_unavailable"]
    assert len(unavailable) == 1
