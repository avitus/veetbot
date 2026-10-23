"""Widget code lives only on the public `cockpit/widgets` branch (docs/plan/cockpit.md)."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_main_never_contains_widget_packages() -> None:
    assert not (ROOT / "cockpit" / "widgets").exists()
    tracked = subprocess.run(
        ["git", "ls-files", "--", "cockpit/widgets"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert tracked == ""
