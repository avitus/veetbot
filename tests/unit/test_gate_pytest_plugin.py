"""Tests for the private gate-result reporter plugin."""

from pathlib import Path

import pytest

from agent_core.evals import gate_pytest_plugin


def test_session_finish_creates_the_report_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "nested" / "reports" / "gate.json"
    monkeypatch.setenv("AGENT_GATE_REPORT", str(destination))

    gate_pytest_plugin.pytest_sessionfinish(object(), 0)

    assert destination.is_file()


def test_session_finish_contains_report_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    destination = tmp_path / "gate.json"
    monkeypatch.setenv("AGENT_GATE_REPORT", str(destination))

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read only")

    monkeypatch.setattr(Path, "write_text", fail_write)

    gate_pytest_plugin.pytest_sessionfinish(object(), 0)

    assert "could not write gate report" in caplog.text
