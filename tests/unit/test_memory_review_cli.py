"""The operator reviews a flagged belief from the CLI with the same outcomes as the clients."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from typer.testing import CliRunner

from agent_core.cli import main as cli_main
from agent_core.cli.main import app
from tests.contract.memory_fixtures import memory


def test_memory_review_takes_one_outcome_and_prints_the_reviewed_belief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    belief = memory().model_copy(update={"flagged_for_review": True})
    seen: list[tuple[UUID, str, str | None]] = []

    async def fake_review(belief_id: UUID, outcome: str, replacement: str | None) -> object:
        seen.append((belief_id, outcome, replacement))
        return belief.model_copy(update={"flagged_for_review": False})

    monkeypatch.setattr(cli_main, "_memory_review", fake_review)
    runner = CliRunner()
    reviewed = runner.invoke(app, ["memory", "review", str(belief.id), "--outcome", "dismiss"])
    assert reviewed.exit_code == 0, reviewed.output
    assert json.loads(reviewed.stdout)["flagged_for_review"] is False
    assert seen == [(belief.id, "dismiss", None)]
    # ADR-0224: changed supersedes with the operator's wording, and only with it.
    refused = runner.invoke(app, ["memory", "review", str(belief.id), "--outcome", "changed"])
    assert refused.exit_code == 2
    stray = runner.invoke(
        app,
        ["memory", "review", str(belief.id), "--outcome", "dismiss", "--replacement", "New."],
    )
    assert stray.exit_code == 2
    changed = runner.invoke(
        app,
        ["memory", "review", str(belief.id), "--outcome", "changed", "--replacement", "New."],
    )
    assert changed.exit_code == 0, changed.output
    assert seen[-1] == (belief.id, "changed", "New.")
