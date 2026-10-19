"""Formation repair is explicit and cannot smuggle broader runtime changes."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_recall_checkpoint import _digest, _json
from tests.unit.test_general_recall_formation_repair import ROOT, SOURCE, original_source
from tests.unit.test_recall_checkpoint_invocation import module, successor_fixture
from tests.unit.test_recall_release_binding import git

pytestmark = pytest.mark.static


def repair_invocation(tmp_path: Path) -> dict[str, Any]:
    args = successor_fixture(tmp_path)
    previous = json.loads(args["protocol_path"].read_bytes())
    old = tmp_path / "bounded-protocol.json"
    old.write_bytes(args["protocol_path"].read_bytes())
    protocol = {
        **previous,
        "version": "recall-checkpoint-operator@3",
        "repair_predecessor_file": str(old),
        "repair_predecessor_sha256": args["protocol_sha256"],
        "files": dict(previous["files"]),
    }
    files = {
        "repair_manifest_file": _json({"predecessor_protocol_sha256": args["protocol_sha256"]}),
        "repair_owner_file": b"{}",
        "repair_assessment_file": b"{}",
    }
    for key, raw in files.items():
        path = tmp_path / (key + ".json")
        path.write_bytes(raw)
        protocol[key] = str(path)
    for key, root, raw in [
        ("repair_original_source", previous["frozen_root"], original_source()),
        ("repair_runtime_source", previous["collector_root"], (ROOT / SOURCE).read_bytes()),
    ]:
        path = Path(root) / SOURCE
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        protocol[key] = str(path)
        git(Path(root), "add", ".")
        git(Path(root), "commit", "-m", "Explicit runtime source")
        protocol["frozen_commit" if key == "repair_original_source" else "collector_commit"] = git(
            Path(root), "rev-parse", "HEAD"
        )
    # The predecessor's actual frozen root already included the original runtime.
    previous["frozen_commit"] = protocol["frozen_commit"]
    previous["files"][protocol["repair_original_source"]] = _digest(original_source())
    old.write_bytes(_json(previous))
    protocol["repair_predecessor_sha256"] = _digest(old.read_bytes())
    manifest_path = Path(protocol["repair_manifest_file"])
    manifest_path.write_bytes(
        _json({"predecessor_protocol_sha256": protocol["repair_predecessor_sha256"]})
    )
    protocol["files"].update(previous["files"])
    for key in [
        "repair_predecessor_file",
        *files,
        "repair_original_source",
        "repair_runtime_source",
    ]:
        path = Path(protocol[key])
        protocol["files"][str(path)] = _digest(path.read_bytes())
    segment = json.loads(args["segment_path"].read_bytes())
    segment["repair_manifest_sha256"] = _digest(manifest_path.read_bytes())
    args["protocol_path"].write_bytes(_json(protocol))
    args["protocol_sha256"] = _digest(args["protocol_path"].read_bytes())
    segment["protocol_sha256"] = args["protocol_sha256"]
    args["segment_path"].write_bytes(_json(segment))
    args["segment_sha256"] = _digest(args["segment_path"].read_bytes())
    return args


def test_only_explicit_formation_source_override_is_admitted(tmp_path: Path) -> None:
    args = repair_invocation(tmp_path)
    protocol, segment = module.verify_invocation(**args)
    assert protocol["version"] == "recall-checkpoint-operator@3"
    assert not segment["initialize_store"] and not segment["initialize_deadline"]


@pytest.mark.parametrize(
    "change", ["runtime", "plan", "state", "deadline", "source", "original", "unpin", "approval"]
)
def test_repair_cannot_change_other_inputs_or_reauthorize_old_work(
    tmp_path: Path, change: str
) -> None:
    args = repair_invocation(tmp_path)
    protocol = json.loads(args["protocol_path"].read_bytes())
    segment = json.loads(args["segment_path"].read_bytes())
    if change == "runtime":
        protocol["frozen_commit"] = "0" * 40
    elif change == "plan":
        protocol["plan_sha256"] = "0" * 64
    elif change == "state":
        protocol["state_directory"] = str(tmp_path / "new-state")
    elif change == "deadline":
        segment["initialize_deadline"] = True
    elif change == "approval":
        segment["repair_manifest_sha256"] = "0" * 64
    elif change == "unpin":
        protocol["files"].pop(next(iter(protocol["files"])))
    else:
        key = "repair_runtime_source" if change == "source" else "repair_original_source"
        path = Path(protocol[key])
        path.write_bytes(path.read_bytes() + b"\nx=1\n")
        protocol["files"][str(path)] = _digest(path.read_bytes())
    args["protocol_path"].write_bytes(_json(protocol))
    args["protocol_sha256"] = _digest(args["protocol_path"].read_bytes())
    segment["protocol_sha256"] = args["protocol_sha256"]
    args["segment_path"].write_bytes(_json(segment))
    args["segment_sha256"] = _digest(args["segment_path"].read_bytes())
    with pytest.raises(ValueError):
        module.verify_invocation(**args)


def test_repaired_module_loads_before_memory_package_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    root = tmp_path / "frozen"
    source = root / "src/agent_core"
    for name in ("", "evals", "memory"):
        folder = source / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").write_text("")
    (source / "memory/__init__.py").write_text("from .distillation import value\n")
    (source / "memory/distillation.py").write_text('value="original"\n')
    patch = tmp_path / "repaired.py"
    patch.write_text('value="repaired"\n')
    for name in tuple(sys.modules):
        if name == "agent_core" or name.startswith("agent_core."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "executable", str(root / ".venv/bin/python"))
    monkeypatch.setattr(module, "MODULES", ())
    verified: list[bool] = []
    monkeypatch.setattr(module, "verify_formation_sources", lambda p: verified.append(True))
    try:
        module._bootstrap(
            {
                "version": "recall-checkpoint-operator@3",
                "frozen_root": str(root),
                "collector_root": str(tmp_path),
                "files": {},
                "repair_runtime_source": str(patch),
            }
        )
        assert verified == [True]
        assert sys.modules["agent_core.memory"].value == "repaired"
        assert sys.modules["agent_core.memory.distillation"].__file__ == str(patch)
    finally:
        for name in tuple(sys.modules):
            if name == "agent_core" or name.startswith("agent_core."):
                sys.modules.pop(name)
