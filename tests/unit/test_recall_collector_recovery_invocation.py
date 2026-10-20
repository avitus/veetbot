"""Collector repair cannot change the measured runtime, allocation or deadline."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_recall_checkpoint import _digest, _json
from tests.unit.test_general_recall_formation_repair import ROOT
from tests.unit.test_recall_checkpoint_invocation import module
from tests.unit.test_recall_formation_repair_invocation import repair_invocation
from tests.unit.test_recall_release_binding import git

pytestmark = pytest.mark.static


def fixture(tmp_path: Path) -> dict[str, Any]:
    args = repair_invocation(tmp_path)
    previous = json.loads(args["protocol_path"].read_bytes())
    old = tmp_path / "deadline-protocol.json"
    old.write_bytes(args["protocol_path"].read_bytes())
    protocol = {
        **previous,
        "version": "recall-checkpoint-operator@4",
        "terminal_predecessor_file": str(old),
        "terminal_predecessor_sha256": _digest(old.read_bytes()),
        "files": dict(previous["files"]),
    }
    root = Path(protocol["collector_root"])
    bank_root = root / "src/agent_core/evals"
    bank_root.mkdir(parents=True)
    protocol["bank_patch_root"] = str(bank_root)
    for short in ("general_recall_formation_bank", "general_recall_observation"):
        path = bank_root / (short + ".py")
        path.write_bytes((ROOT / "src/agent_core/evals" / path.name).read_bytes())
        protocol["files"][str(path)] = _digest(path.read_bytes())
    git(root, "add", ".")
    git(root, "commit", "-m", "Terminal bank source")
    protocol["collector_commit"] = git(root, "rev-parse", "HEAD")
    for key, raw in {
        "collector_recovery_manifest_file": _json(
            {"predecessor_protocol_sha256": protocol["terminal_predecessor_sha256"]}
        ),
        "collector_recovery_owner_file": b"{}",
        "collector_recovery_assessment_file": b"{}",
    }.items():
        path = tmp_path / (key + ".json")
        path.write_bytes(raw)
        protocol[key] = str(path)
        protocol["files"][str(path)] = _digest(raw)
    protocol["files"][str(old)] = _digest(old.read_bytes())
    segment = json.loads(args["segment_path"].read_bytes())
    segment["collector_recovery_manifest_sha256"] = protocol["files"][
        protocol["collector_recovery_manifest_file"]
    ]
    return save(args, protocol, segment)


def save(args: dict[str, Any], protocol: dict[str, Any], segment: dict[str, Any]) -> dict[str, Any]:
    args["protocol_path"].write_bytes(_json(protocol))
    args["protocol_sha256"] = _digest(args["protocol_path"].read_bytes())
    segment["protocol_sha256"] = args["protocol_sha256"]
    args["segment_path"].write_bytes(_json(segment))
    args["segment_sha256"] = _digest(args["segment_path"].read_bytes())
    return args


def test_collector_successor_pins_original_inputs_and_two_bank_modules(tmp_path: Path) -> None:
    protocol, segment = module.verify_invocation(**fixture(tmp_path))
    assert protocol["version"] == "recall-checkpoint-operator@4"
    assert not segment["initialize_deadline"] and not segment["initialize_store"]


@pytest.mark.parametrize(
    "damage",
    [
        "runtime",
        "allocation",
        "state",
        "deadline",
        "unpin",
        "approval",
        "bank_root",
        "extra_runtime",
    ],
)
def test_collector_successor_refuses_unrelated_changes_before_bootstrap(
    tmp_path: Path, damage: str
) -> None:
    args = fixture(tmp_path)
    protocol = json.loads(args["protocol_path"].read_bytes())
    segment = json.loads(args["segment_path"].read_bytes())
    if damage == "runtime":
        protocol["frozen_commit"] = "0" * 40
    elif damage == "allocation":
        protocol["plan_sha256"] = "0" * 64
    elif damage == "state":
        protocol["state_directory"] = str(tmp_path / "new-state")
    elif damage == "deadline":
        segment["initialize_deadline"] = True
    elif damage == "unpin":
        protocol["files"].pop(
            str(Path(protocol["bank_patch_root"]) / "general_recall_observation.py")
        )
    elif damage == "approval":
        segment["collector_recovery_manifest_sha256"] = "0" * 64
    elif damage == "bank_root":
        protocol["bank_patch_root"] = str(tmp_path)
    else:
        protocol["other_runtime_patch"] = "unrelated.py"
    with pytest.raises(ValueError):
        module.verify_invocation(**save(args, protocol, segment))
