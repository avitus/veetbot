"""The forward-only cutover is an explicit, owner-bound operator action."""

import pytest
from typer.testing import CliRunner

from agent_core.cli.main import app


def test_forward_only_never_connects_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(**kwargs: object) -> None:
        raise AssertionError("Must validate confirmation before building services")

    monkeypatch.setattr("agent_core.cli.main.build", unavailable)
    result = CliRunner().invoke(
        app, ["memory", "reconsolidations", "forward-only", "--owner", "a/b"]
    )
    assert result.exit_code == 2
    assert "--confirm is required" in result.output


def test_forward_only_refuses_another_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from tests.contract.support import principal

    @asynccontextmanager
    async def configured(**kwargs: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(principal=principal())

    monkeypatch.setattr("agent_core.cli.main.build", configured)
    result = CliRunner().invoke(
        app, ["memory", "reconsolidations", "forward-only", "--owner", "other/owner", "--confirm"]
    )
    assert result.exit_code == 1
    assert "Owner must match" in result.output
