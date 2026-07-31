"""Committed-file secret scanner gate."""

import subprocess
from pathlib import Path

from scripts.architecture_checks import secret_findings

ROOT = Path(__file__).resolve().parents[2]


def test_no_committed_secrets() -> None:
    findings, errors = secret_findings(ROOT)
    assert errors == []
    assert [finding.render() for finding in findings] == []


def test_scanner_reports_rule_without_echoing_secret_and_requires_reason(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
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
