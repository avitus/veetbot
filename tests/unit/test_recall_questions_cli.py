"""Operator commands for the stored recall question set (ADR-0339)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli import main


def test_probe_takes_exactly_one_of_a_file_or_the_stored_set(tmp_path: Path) -> None:
    runner = CliRunner()
    output = str(tmp_path / "report.json")
    neither = runner.invoke(main.app, ["memory", "probe", "--output", output])
    both = runner.invoke(
        main.app,
        ["memory", "probe", "--output", output, "--probes", "x.jsonl", "--questions", "all"],
    )
    unknown = runner.invoke(
        main.app, ["memory", "probe", "--output", output, "--questions", "everything"]
    )
    assert [neither.exit_code, both.exit_code, unknown.exit_code] == [2, 2, 2]


def test_probe_runs_the_stored_set_it_is_asked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def probe(*arguments: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"probes": 3}

    monkeypatch.setattr(main, "_memory_probe", probe)
    output = tmp_path / "report.json"
    result = CliRunner().invoke(
        main.app, ["memory", "probe", "--output", str(output), "--questions", "holdout"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [(None, output, ["automatic", "deliberate"], False, "holdout")]


def test_import_names_the_half_and_reports_what_it_did(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Path, str]] = []

    async def imported(path: Path, split: str) -> dict[str, int]:
        calls.append((path, split))
        return {"created": 2, "updated": 1, "unchanged": 0}

    monkeypatch.setattr(main, "_questions_import", imported)
    probes = tmp_path / "questions.jsonl"
    runner = CliRunner()
    done = runner.invoke(
        main.app, ["memory", "questions", "import", "--probes", str(probes), "--split", "holdout"]
    )
    # A question joins one half for good, so the command will not guess it.
    wrong = runner.invoke(
        main.app, ["memory", "questions", "import", "--probes", str(probes), "--split", "all"]
    )
    assert done.exit_code == 0, done.output
    assert json.loads(done.output) == {"created": 2, "updated": 1, "unchanged": 0}
    assert calls == [(probes, "holdout")] and wrong.exit_code == 2


def test_import_failure_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def refused(_path: Path, _split: str) -> dict[str, int]:
        raise ValueError("questions.jsonl:3: a save probe is not a recall question")

    monkeypatch.setattr(main, "_questions_import", refused)
    result = CliRunner().invoke(
        main.app,
        [
            "memory",
            "questions",
            "import",
            "--probes",
            str(tmp_path / "q.jsonl"),
            "--split",
            "holdout",
        ],
    )
    assert result.exit_code == 1 and "save probe" in result.output
    assert "Traceback" not in result.output


def test_export_writes_a_file_only_its_owner_can_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exported(which: str) -> list[str]:
        assert which == "development"
        return ['{"id":"home-city"}', '{"id":"car-model"}']

    monkeypatch.setattr(main, "_questions_export", exported)
    output = tmp_path / "nested" / "questions.jsonl"
    result = CliRunner().invoke(
        main.app,
        ["memory", "questions", "export", "--output", str(output), "--split", "development"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"exported": 2}
    assert output.read_text(encoding="utf-8").splitlines() == [
        '{"id":"home-city"}',
        '{"id":"car-model"}',
    ]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_export_over_an_existing_file_leaves_it_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exported(which: str) -> list[str]:
        return ['{"id":"home-city"}']

    monkeypatch.setattr(main, "_questions_export", exported)
    output = tmp_path / "questions.jsonl"
    output.write_text("an older export\n", encoding="utf-8")
    output.chmod(0o644)
    result = CliRunner().invoke(
        main.app, ["memory", "questions", "export", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert output.read_text(encoding="utf-8") == '{"id":"home-city"}\n'
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    # Nothing but the export is left beside it.
    assert [item.name for item in tmp_path.iterdir()] == ["questions.jsonl"]


def test_a_failed_export_keeps_the_older_file_and_leaves_no_scrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exported(which: str) -> list[str]:
        return ['{"id":"home-city"}']

    def refuse(source: object, target: object) -> None:
        raise OSError("the disk refused the rename")

    monkeypatch.setattr(main, "_questions_export", exported)
    monkeypatch.setattr("os.replace", refuse)
    output = tmp_path / "questions.jsonl"
    output.write_text("an older export\n", encoding="utf-8")
    result = CliRunner().invoke(
        main.app, ["memory", "questions", "export", "--output", str(output)]
    )
    assert result.exit_code == 1
    assert output.read_text(encoding="utf-8") == "an older export\n"
    assert [item.name for item in tmp_path.iterdir()] == ["questions.jsonl"]


def test_record_reads_a_bounded_report_and_counts_what_it_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[bytes] = []

    async def recorded(data: bytes) -> dict[str, int]:
        seen.append(data)
        return {"recorded": 7, "stale": 2, "unknown": 1}

    monkeypatch.setattr(main, "_questions_record", recorded)
    report = tmp_path / "report.json"
    report.write_bytes(b"{}")
    runner = CliRunner()
    done = runner.invoke(main.app, ["memory", "questions", "record", "--report", str(report)])
    missing = runner.invoke(
        main.app, ["memory", "questions", "record", "--report", str(tmp_path / "absent.json")]
    )
    assert done.exit_code == 0
    assert json.loads(done.output) == {"recorded": 7, "stale": 2, "unknown": 1}
    assert seen == [b"{}"] and missing.exit_code == 1
