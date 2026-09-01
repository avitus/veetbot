"""The `agent persona` command group over an in-memory composition."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from typer.testing import CliRunner

import agent_core.cli.main as cli_main
from agent_core.bootstrap import build
from tests.integration.m2_support import memory_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def _memory_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build(**_kwargs: Any) -> Any:
        return build(settings=memory_settings(), storage="memory", sequential_ids=True)

    monkeypatch.setattr(cli_main, "build", fake_build)


def test_persona_show_prints_the_empty_document() -> None:
    result = runner.invoke(cli_main.app, ["persona", "show"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["version"] == 0
    assert body["entries"] == []


def test_persona_edit_creates_a_version_behind_the_guard() -> None:
    result = runner.invoke(
        cli_main.app,
        ["persona", "edit", "--expected-version", "0", "--entry", "User values honesty."],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["version"] == 1
    assert body["entries"][0]["text"] == "User values honesty."


def test_persona_edit_conflicts_loudly_on_a_stale_version() -> None:
    result = runner.invoke(
        cli_main.app,
        ["persona", "edit", "--expected-version", "3", "--entry", "User values honesty."],
    )
    assert result.exit_code == 3
    assert "version conflict" in result.output


def test_persona_edit_refuses_credential_material() -> None:
    result = runner.invoke(
        cli_main.app,
        [
            "persona",
            "edit",
            "--expected-version",
            "0",
            "--entry",
            "api_key: value-789",
        ],
    )
    assert result.exit_code == 2
    assert "credential" in result.output


def test_persona_affirm_and_decline_report_absence() -> None:
    affirm = runner.invoke(cli_main.app, ["persona", "affirm", str(uuid4())])
    assert affirm.exit_code == 1
    decline = runner.invoke(cli_main.app, ["persona", "decline", str(uuid4())])
    assert decline.exit_code == 1
