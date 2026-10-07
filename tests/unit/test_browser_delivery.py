"""Browser verification must execute Chromium before a release can ship."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_browser_verification_blocks_packaging_and_runs_required_chromium() -> None:
    config = yaml.safe_load((ROOT / ".circleci/config.yml").read_text())
    assert "browser" in config["jobs"]
    job = config["jobs"]["browser"]
    commands = [step["run"]["command"] for step in job["steps"] if "run" in step]
    assert any("make test-browser" in command for command in commands)
    packaging = next(
        item["package-release"]
        for item in config["workflows"]["verify"]["jobs"]
        if isinstance(item, dict) and "package-release" in item
    )
    assert "browser" in packaging["requires"]
    makefile = (ROOT / "Makefile").read_text()
    check = next(line for line in makefile.splitlines() if line.startswith("check:"))
    assert "test-browser" in check
    assert "VEETBOT_REQUIRE_REAL_BROWSER=1" in makefile
