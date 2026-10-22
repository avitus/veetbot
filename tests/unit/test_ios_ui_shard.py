"""Simulator UI cases split across executors by name (ADR-0230)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ios_ui_shard import shard_options

ROOT = Path(__file__).resolve().parents[2]

# The shape xcodebuild -enumerate-tests -test-enumeration-format json writes.
ENUMERATION = {
    "values": [
        {
            "testPlan": "Veetbot",
            "enabledTests": [
                {"identifier": "VeetbotUITests/ConversationNavigationUITests/testC"},
                {"identifier": "VeetbotUITests/ConversationNavigationUITests/testA"},
                {"identifier": "VeetbotUITests/ConversationNavigationUITests/testD"},
                {"identifier": "VeetbotUITests/ConversationNavigationUITests/testB"},
                {"identifier": "VeetbotUITests/ConversationNavigationUITests/testE"},
            ],
            "disabledTests": [
                {"identifier": "VeetbotUITests/ConversationNavigationUITests/testOff"}
            ],
        }
    ]
}


def _names(options: list[str]) -> list[str]:
    assert all(option.startswith("-only-testing:") for option in options)
    return [option.rsplit("/", 1)[1] for option in options]


def test_shards_partition_the_enabled_cases_in_name_order() -> None:
    first = _names(shard_options(ENUMERATION, "0/2"))
    second = _names(shard_options(ENUMERATION, "1/2"))

    assert first == ["testA", "testC", "testE"]
    assert second == ["testB", "testD"]
    assert "testOff" not in first + second


def test_one_shard_runs_every_enabled_case() -> None:
    assert _names(shard_options(ENUMERATION, "0/1")) == [
        "testA",
        "testB",
        "testC",
        "testD",
        "testE",
    ]


@pytest.mark.parametrize("shard", ["2/2", "-1/2", "0/0", "a/b", "1"])
def test_an_invalid_shard_is_refused(shard: str) -> None:
    with pytest.raises(ValueError):
        shard_options(ENUMERATION, shard)


def test_an_empty_enumeration_is_refused() -> None:
    with pytest.raises(ValueError):
        shard_options({"values": [{"enabledTests": []}]}, "0/2")


def test_the_command_prints_one_option_per_case(tmp_path: Path) -> None:
    enumeration = tmp_path / "tests.json"
    enumeration.write_text(json.dumps(ENUMERATION), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "scripts.ios_ui_shard", str(enumeration), "1/2"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _names(result.stdout.split()) == ["testB", "testD"]


def test_a_swift_call_suffix_is_dropped_from_xctest_names() -> None:
    """-only-testing names an XCTest case without the () a listing may show."""
    enumeration = {
        "values": [
            {
                "enabledTests": [
                    {"identifier": "VeetbotUITests/ConversationNavigationUITests/testA()"}
                ]
            }
        ]
    }

    assert shard_options(enumeration, "0/1") == [
        "-only-testing:VeetbotUITests/ConversationNavigationUITests/testA"
    ]


def test_the_command_reports_its_share_of_the_cases(tmp_path: Path) -> None:
    enumeration = tmp_path / "tests.json"
    enumeration.write_text(json.dumps(ENUMERATION), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "scripts.ios_ui_shard", str(enumeration), "0/2"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "shard 0/2 runs 3 of 5 enabled cases" in result.stderr
