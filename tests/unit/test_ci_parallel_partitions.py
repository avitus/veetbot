"""The hosted verify run's longest jobs split across executors (ADR-0230)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _config() -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(
        (ROOT / ".circleci" / "config.yml").read_text(encoding="utf-8")
    )
    return config


def _verify_entries() -> dict[str, dict[str, Any]]:
    """Each verify workflow entry by its name, with its settings."""
    entries: dict[str, dict[str, Any]] = {}
    for item in _config()["workflows"]["verify"]["jobs"]:
        if isinstance(item, str):
            entries[item] = {"job": item}
            continue
        job, settings = next(iter(item.items()))
        entries[settings.get("name", job)] = {"job": job, **settings}
    return entries


def test_the_simulator_ui_job_runs_once_per_device_on_its_own_executor() -> None:
    job = _config()["jobs"]["apple-ios-device"]
    assert job["parameters"] == {"device": {"type": "enum", "enum": ["iphone", "ipad"]}}
    commands = [step["run"]["command"] for step in job["steps"] if "run" in step]
    assert any(
        "IOS_UI_DEVICES=<< parameters.device >>" in command and "make test-apple-ui-ios" in command
        for command in commands
    )
    assert {"skip_verified_tree": {"job": "apple-ios-<< parameters.device >>"}} in job["steps"]
    assert {"record_verified_tree": {"job": "apple-ios-<< parameters.device >>"}} in job["steps"]
    assert {"store_apple_test_results": {"platform": "ios-<< parameters.device >>"}} in job["steps"]

    entries = _verify_entries()
    for device in ("iphone", "ipad"):
        assert entries[f"apple-ios-{device}"] == {
            "job": "apple-ios-device",
            "name": f"apple-ios-{device}",
            "device": device,
        }


def test_the_required_apple_ios_status_passes_only_after_both_devices() -> None:
    """main's branch protection requires the status named apple-ios."""
    gate = _config()["jobs"]["apple-ios"]
    assert [next(iter(step)) for step in gate["steps"]] == ["run"]
    entries = _verify_entries()
    assert entries["apple-ios"]["requires"] == ["apple-ios-iphone", "apple-ios-ipad"]
    assert "apple-ios" in entries["package-release"]["requires"]


def test_each_device_splits_its_cases_across_two_executors() -> None:
    job = _config()["jobs"]["apple-ios-device"]
    assert job["parallelism"] == 2
    commands = [step["run"]["command"] for step in job["steps"] if "run" in step]
    assert any(
        'IOS_UI_SHARD="$CIRCLE_NODE_INDEX/$CIRCLE_NODE_TOTAL"' in command for command in commands
    )


@pytest.mark.parametrize("name", ["static", "integration"])
def test_the_longest_python_partitions_split_across_two_executors(name: str) -> None:
    assert _config()["jobs"][name]["parallelism"] == 2


def test_static_runs_its_non_test_checks_on_one_node() -> None:
    steps = _config()["jobs"]["static"]["steps"]
    named = {step["run"]["name"]: step["run"]["command"] for step in steps if "run" in step}
    for check in ("Lint", "Type check", "Deployment tests", "Documentation checks"):
        assert '"$CIRCLE_NODE_INDEX" != 0' in named[check], check
    assert "CIRCLE_NODE_INDEX" not in named["Static tests"]
    assert "CIRCLE_NODE_INDEX" not in named["Reading lane floor"]


def _shell(command: str, job: str) -> str:
    return command.replace("<< parameters.job >>", job).replace("<< pipeline.git.branch >>", "main")


def test_each_node_records_its_own_pass_of_the_tree() -> None:
    commands = _config()["commands"]
    record = commands["record_verified_tree"]["steps"]
    key = (
        "verified-v2-<< parameters.job >>-{{ .Environment.CIRCLE_NODE_INDEX }}-"
        '{{ checksum "/tmp/veetbot-verified/tree" }}'
    )
    assert record[-1] == {"save_cache": {"key": key, "paths": ["/tmp/veetbot-verified"]}}
    touch = record[0]["run"]["command"]
    assert "<< parameters.job >>-$CIRCLE_NODE_INDEX-of-$CIRCLE_NODE_TOTAL-" in touch

    skip = commands["skip_verified_tree"]["steps"]
    restored = [step["restore_cache"]["keys"] for step in skip if "restore_cache" in step]
    assert restored == [
        [f'verified-v2-<< parameters.job >>-{node}-{{{{ checksum "/tmp/veetbot-verified/tree" }}}}']
        for node in (0, 1)
    ]


@pytest.mark.parametrize(
    ("total", "recorded", "halts"),
    [
        ("1", ["0-of-1"], True),
        ("2", ["0-of-2"], False),
        ("2", ["0-of-2", "1-of-2"], True),
        ("2", ["0-of-1", "1-of-1"], False),
    ],
    ids=["single-node-passed", "one-of-two-passed", "both-passed", "other-split-passed"],
)
def test_main_skips_a_job_only_when_every_node_passed_the_tree(
    tmp_path: Path, total: str, recorded: list[str], halts: bool
) -> None:
    skip = _config()["commands"]["skip_verified_tree"]["steps"]
    command = next(
        step["run"]["command"]
        for step in skip
        if "run" in step and "circleci-agent step halt" in step["run"]["command"]
    )
    records = tmp_path / "records"
    records.mkdir()
    (records / "tree").write_text("abc123\n", encoding="utf-8")
    for node in recorded:
        (records / f"static-{node}-abc123").touch()
    agent = tmp_path / "bin" / "circleci-agent"
    agent.parent.mkdir()
    agent.write_text(f"#!/bin/sh\ntouch {tmp_path}/halted\n", encoding="utf-8")
    agent.chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", _shell(command, "static").replace("/tmp/veetbot-verified", str(records))],
        env={
            **os.environ,
            "PATH": f"{agent.parent}:{os.environ['PATH']}",
            "CIRCLE_NODE_TOTAL": total,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "halted").exists() is halts


_SIMCTL = {
    "devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
            {
                "name": "iPhone 18 Pro",
                "udid": "IPHONE-1",
                "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-18-Pro",
            },
            {
                "name": "iPad (A16)",
                "udid": "IPAD-1",
                "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPad-A16",
            },
        ]
    }
}


@pytest.fixture
def simulator_checkout(tmp_path: Path) -> Path:
    """A copy of the Makefile with stand-ins that log every xcrun and xcodebuild call."""
    shutil.copyfile(ROOT / "Makefile", tmp_path / "Makefile")
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    (tmp_path / "simctl.json").write_text(json.dumps(_SIMCTL), encoding="utf-8")
    cases = [f"VeetbotUITests/ConversationNavigationUITests/test{name}" for name in "ABCD"]
    (tmp_path / "enumeration.json").write_text(
        json.dumps({"values": [{"enabledTests": [{"identifier": case} for case in cases]}]}),
        encoding="utf-8",
    )
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts", ignore=shutil.ignore_patterns("*.pyc"))
    log = tmp_path / "calls.log"
    for name, body in (
        (
            "xcrun",
            'case "$2 $3" in\n'
            f'"list devices") test "$4" = booted || cat "{tmp_path}/simctl.json" ;;\n'
            '"bootstatus "*) echo Finished ;;\n'
            "esac\n",
        ),
        (
            "xcodebuild",
            # -enumerate-tests writes its JSON to the path after this option.
            'while test "$#" -gt 0; do\n'
            '  if test "$1" = -test-enumeration-output-path; then\n'
            f'    cp "{tmp_path}/enumeration.json" "$2"\n'
            "  fi\n"
            "  shift\n"
            "done\n",
        ),
    ):
        script = stand_ins / name
        script.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "{log}"\n{body}', encoding="utf-8"
        )
        script.chmod(0o755)
    return tmp_path


def _run_simulator_target(checkout: Path, *settings: str) -> list[str]:
    environment = {
        **os.environ,
        "PATH": f"{checkout / 'bin'}:{os.environ['PATH']}",
        "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer",
        "TMPDIR": str(checkout),
    }
    for variable in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL"):
        environment.pop(variable, None)
    result = subprocess.run(
        ["make", "--no-print-directory", "test-apple-ui-ios", *settings],
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return (checkout / "calls.log").read_text(encoding="utf-8").splitlines()


def test_the_simulator_target_runs_both_devices_by_default(simulator_checkout: Path) -> None:
    calls = _run_simulator_target(simulator_checkout)

    runs = [call for call in calls if "test-without-building" in call]
    assert len(runs) == 2
    assert any("id=IPHONE-1" in call for call in runs)
    assert any("id=IPAD-1" in call for call in runs)


@pytest.mark.parametrize(
    ("device", "chosen", "other"),
    [("ipad", "IPAD-1", "IPHONE-1"), ("iphone", "IPHONE-1", "IPAD-1")],
)
def test_the_simulator_target_builds_boots_and_runs_only_the_named_device(
    simulator_checkout: Path, device: str, chosen: str, other: str
) -> None:
    calls = _run_simulator_target(simulator_checkout, f"IOS_UI_DEVICES={device}")

    build = [call for call in calls if "build-for-testing" in call]
    runs = [call for call in calls if "test-without-building" in call]
    boots = [call for call in calls if "bootstatus" in call]
    assert len(build) == 1 and f"id={chosen}" in build[0]
    assert len(runs) == 1 and f"id={chosen}" in runs[0]
    assert boots and all(chosen in call for call in boots)
    assert not any(other in call for call in calls if "simctl list" not in call)


def test_the_simulator_target_rejects_an_unknown_device(simulator_checkout: Path) -> None:
    environment = {
        **os.environ,
        "PATH": f"{simulator_checkout / 'bin'}:{os.environ['PATH']}",
        "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer",
    }
    result = subprocess.run(
        ["make", "--no-print-directory", "test-apple-ui-ios", "IOS_UI_DEVICES=watch"],
        cwd=simulator_checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0
    assert "watch" in result.stderr


def test_a_shard_runs_every_other_enumerated_case(simulator_checkout: Path) -> None:
    calls = _run_simulator_target(simulator_checkout, "IOS_UI_DEVICES=ipad", "IOS_UI_SHARD=1/2")

    enumerations = [call for call in calls if "-enumerate-tests" in call]
    runs = [call for call in calls if "test-without-building" in call and "-enumerate" not in call]
    assert len(enumerations) == 1 and "id=IPAD-1" in enumerations[0]
    # Pipeline 1306: the default hierarchical listing has no enabledTests list.
    assert "-test-enumeration-style flat" in enumerations[0]
    assert len(runs) == 1
    assert "ConversationNavigationUITests/testB" in runs[0]
    assert "ConversationNavigationUITests/testD" in runs[0]
    assert "testA" not in runs[0] and "testC" not in runs[0]


def test_an_unsharded_run_names_the_whole_suite(simulator_checkout: Path) -> None:
    calls = _run_simulator_target(simulator_checkout, "IOS_UI_DEVICES=iphone")

    assert not any("-enumerate-tests" in call for call in calls)
    runs = [call for call in calls if "test-without-building" in call]
    assert len(runs) == 1 and runs[0].endswith("-only-testing:VeetbotUITests")
