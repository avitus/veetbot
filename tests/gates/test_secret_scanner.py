"""Committed-file secret scanner gate."""

import hashlib
import shutil
import subprocess
from pathlib import Path

from scripts.architecture_checks import secret_findings

ROOT = Path(__file__).resolve().parents[2]


def _git_executable() -> str:
    git = shutil.which("git")
    assert git is not None
    return git


def test_no_committed_secrets() -> None:
    findings, errors = secret_findings(ROOT)
    assert errors == []
    assert [finding.render() for finding in findings] == []


def test_scanner_reports_rule_without_echoing_secret_and_requires_reason(tmp_path: Path) -> None:
    subprocess.run([_git_executable(), "init", "-q"], cwd=tmp_path, check=True)
    source = tmp_path / "src" / "leak.py"
    source.parent.mkdir()
    synthetic_value = "sk-" + ("x" * 24)
    source.write_text("TOK" + f"EN = '{synthetic_value}'\n", encoding="utf-8")
    allowlist = tmp_path / "security" / "secret-allowlist.yaml"
    allowlist.parent.mkdir()
    allowlist.write_text(
        "allow:\n  - path: src/leak.py\n    line: 1\n    rule: provider_key\n",
        encoding="utf-8",
    )

    findings, errors = secret_findings(tmp_path)
    rendered = [finding.render() for finding in findings]
    assert any("provider_key" in finding for finding in rendered)
    assert all(synthetic_value not in finding for finding in rendered)
    assert any("requires a prose reason" in error for error in errors)


def test_scanner_covers_downloadable_client_sources(tmp_path: Path) -> None:
    subprocess.run([_git_executable(), "init", "-q"], cwd=tmp_path, check=True)
    source = tmp_path / "client" / "veetbot_client" / "leak.py"
    source.parent.mkdir(parents=True)
    synthetic_value = "sk-" + ("x" * 24)
    source.write_text(f"consume('{synthetic_value}')\n", encoding="utf-8")

    findings, errors = secret_findings(tmp_path)

    assert errors == []
    assert [finding.render() for finding in findings] == [
        "client/veetbot_client/leak.py:1: provider_key"
    ]


def test_scanner_covers_deployment_sources(tmp_path: Path) -> None:
    subprocess.run([_git_executable(), "init", "-q"], cwd=tmp_path, check=True)
    source = tmp_path / "deploy" / "app" / "leak.env"
    source.parent.mkdir(parents=True)
    synthetic_value = "sk-" + ("x" * 24)
    source.write_text(f"PROVIDER_KEY={synthetic_value}\n", encoding="utf-8")

    findings, errors = secret_findings(tmp_path)

    assert errors == []
    assert [finding.render() for finding in findings] == ["deploy/app/leak.env:1: provider_key"]


def test_secret_allowlist_suppresses_exact_match_and_reports_stale_entry(tmp_path: Path) -> None:
    subprocess.run([_git_executable(), "init", "-q"], cwd=tmp_path, check=True)
    source = tmp_path / "src" / "leak.py"
    source.parent.mkdir()
    expected_line = "consume('sk-" + ("x" * 24) + "')"
    source.write_text(expected_line + "\n", encoding="utf-8")
    line_sha256 = hashlib.sha256(expected_line.encode()).hexdigest()
    allowlist = tmp_path / "security" / "secret-allowlist.yaml"
    allowlist.parent.mkdir()
    allowlist.write_text(
        "allow:\n"
        "  - path: src/leak.py\n"
        "    line: 1\n"
        "    rule: provider_key\n"
        f"    line_sha256: {line_sha256}\n"
        "    reason: synthetic scanner fixture\n",
        encoding="utf-8",
    )
    findings, errors = secret_findings(tmp_path)
    assert findings == []
    assert errors == []

    source.write_text("consume('sk-" + ("y" * 24) + "')\n", encoding="utf-8")
    findings, errors = secret_findings(tmp_path)
    assert [finding.render() for finding in findings] == ["src/leak.py:1: provider_key"]
    assert errors == ["stale secret allowlist entry: src/leak.py:1:provider_key"]

    source.write_text(expected_line + "\n", encoding="utf-8")

    allowlist.write_text(
        "allow:\n"
        "  - path: src/leak.py\n"
        "    line: 2\n"
        "    rule: provider_key\n"
        f"    line_sha256: {line_sha256}\n"
        "    reason: intentionally stale fixture\n",
        encoding="utf-8",
    )
    findings, errors = secret_findings(tmp_path)
    assert [finding.render() for finding in findings] == ["src/leak.py:1: provider_key"]
    assert errors == ["stale secret allowlist entry: src/leak.py:2:provider_key"]
