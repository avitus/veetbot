"""ADR-0216: the CLI sets the persona voice and traits and keeps every entry."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli import main as cli_main
from agent_core.cli.main import app
from agent_core.domain.persona import KEEP_PERSONA_FIELD, PersonaTrait


class _View:
    def model_dump_json(self) -> str:
        return "{}"


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, Any, Any]]:
    seen: list[tuple[int, Any, Any]] = []

    async def fake(expected_version: int, voice: Any, traits: Any) -> _View:
        seen.append((expected_version, voice, traits))
        return _View()

    monkeypatch.setattr(cli_main, "_persona_tone", fake)
    return seen


def test_voice_sets_or_clears_and_leaves_traits_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    runner = CliRunner()
    set_voice = runner.invoke(
        app, ["persona", "voice", "--expected-version", "3", "Plain and calm."]
    )
    assert set_voice.exit_code == 0, set_voice.output
    cleared = runner.invoke(app, ["persona", "voice", "--expected-version", "4", "--clear"])
    assert cleared.exit_code == 0, cleared.output
    assert seen == [(3, "Plain and calm.", KEEP_PERSONA_FIELD), (4, None, KEEP_PERSONA_FIELD)]
    both = runner.invoke(app, ["persona", "voice", "--expected-version", "4", "x", "--clear"])
    assert both.exit_code == 2


def test_traits_take_the_closed_set_and_none_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    runner = CliRunner()
    chosen = runner.invoke(app, ["persona", "traits", "--expected-version", "1", "brief", "warm"])
    assert chosen.exit_code == 0, chosen.output
    cleared = runner.invoke(app, ["persona", "traits", "--expected-version", "2"])
    assert cleared.exit_code == 0, cleared.output
    assert seen == [
        (1, KEEP_PERSONA_FIELD, [PersonaTrait.BRIEF, PersonaTrait.WARM]),
        (2, KEEP_PERSONA_FIELD, []),
    ]
    unknown = runner.invoke(app, ["persona", "traits", "--expected-version", "1", "sarcastic"])
    assert unknown.exit_code == 2
