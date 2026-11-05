"""The job package uses the widget privacy screen without evaluating its source."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("source", "screen", "expected"),
    [
        ("def main(inputs, job): return inputs['count']", {}, True),
        (
            "def main(inputs, job): return 'Synthetic Private Owner'",
            {"names": ["Synthetic Private Owner"]},
            False,
        ),
        (
            "def main(inputs, job): return 'private-account-value'",
            {"settingsValues": ["private-account-value"]},
            False,
        ),
        ("raise RuntimeError('Do not execute during screening')", {}, True),
    ],
)
def test_job_screen_shares_widget_privacy_rules(
    source: str, screen: dict[str, object], expected: bool
) -> None:
    result = subprocess.run(
        ["node", str(ROOT / "cockpit/builder/job-screen.mjs")],
        input=json.dumps(
            {"files": {"main.py": source, "job.json": '{"title":"Count items"}'}, "screen": screen}
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    report = json.loads(result.stdout)
    assert report["ok"] is expected
    assert result.returncode == (0 if expected else 1)
    if not expected:
        assert report["diagnostics"][0]["rule"].startswith("screen.")
        assert "Synthetic Private Owner" not in result.stdout
        assert "private-account-value" not in result.stdout
