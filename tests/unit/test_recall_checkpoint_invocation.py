"""Paid invocation rejects stale, uncommitted or changed inputs before bootstrap."""

import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_recall_release_binding import git

pytestmark = pytest.mark.static
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/collect_recall_checkpoint.py"
spec = importlib.util.spec_from_file_location("checkpoint_invocation_test", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(tmp_path: Path) -> dict[str, Any]:
    roots = []
    for name in ("collector", "runtime"):
        root = tmp_path / name
        root.mkdir()
        (root / "original").write_text("Unchanged")
        git(root, "init")
        git(root, "config", "user.name", "Synthetic fixture")
        git(root, "config", "user.email", "fixture@example.invalid")
        git(root, "add", ".")
        git(root, "commit", "-m", "Frozen fixture")
        roots.append(root)
    collector, runtime = roots
    protocol = {
        "version": "recall-checkpoint-operator@1",
        "collector_root": str(collector),
        "collector_commit": git(collector, "rev-parse", "HEAD"),
        "frozen_root": str(runtime),
        "frozen_commit": git(runtime, "rev-parse", "HEAD"),
        "files": {
            str(runtime / "original"): hashlib.sha256(b"Unchanged").hexdigest(),
            str(SCRIPT): hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
        },
        "state_directory": str(tmp_path / "state"),
        "plan_sha256": "a" * 64,
    }
    admission = {
        "version": "recall-checkpoint-paid-admission@1",
        "paid_launch_authorized": True,
        "checked_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": "pending",
        "plan_sha256": "a" * 64,
        "databases": ["veetbot_m33_eval_seg1_a", "veetbot_m33_eval_seg1_b"],
        "output": str(tmp_path / "segment-output"),
        "initialize_store": True,
        "initialize_deadline": True,
    }
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps(protocol))
    digest = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    admission["protocol_sha256"] = digest
    segment = tmp_path / "segment.json"
    segment.write_text(json.dumps(admission))
    return {
        "protocol_path": protocol_path,
        "protocol_sha256": digest,
        "segment_path": segment,
        "segment_sha256": hashlib.sha256(segment.read_bytes()).hexdigest(),
    }


def test_frozen_invocation_is_separate_from_paid_bootstrap(tmp_path: Path) -> None:
    args = fixture(tmp_path)
    protocol, segment = module.verify_invocation(**args)
    assert segment["paid_launch_authorized"] is True
    assert not Path(protocol["state_directory"]).exists()


@pytest.mark.parametrize(
    "change",
    [
        "protocol_hash",
        "segment_hash",
        "source",
        "dirty",
        "ref",
        "symlink",
        "expired",
        "future",
        "unapproved",
        "wrong_protocol",
        "duplicate_db",
        "production_db",
        "existing",
        "missing_reopen",
        "deadline",
    ],
)
def test_invocation_refuses_before_runtime_or_credentials(tmp_path: Path, change: str) -> None:
    args = fixture(tmp_path)
    protocol = json.loads(args["protocol_path"].read_bytes())
    segment = json.loads(args["segment_path"].read_bytes())
    if change == "protocol_hash":
        args["protocol_sha256"] = "b" * 64
    elif change == "segment_hash":
        args["segment_sha256"] = "b" * 64
    elif change == "source":
        Path(protocol["frozen_root"]).joinpath("original").write_text("Changed")
    elif change == "dirty":
        Path(protocol["collector_root"]).joinpath("untracked.py").write_text("Changed")
    elif change == "ref":
        protocol["collector_commit"] = "0" * 40
    elif change == "symlink":
        args["protocol_path"].rename(tmp_path / "target.json")
        args["protocol_path"].symlink_to(tmp_path / "target.json")
    elif change in {"expired", "future"}:
        segment["checked_at"] = (
            "2000-01-01T00:00:00+00:00" if change == "expired" else "2099-01-01T00:00:00+00:00"
        )
    elif change == "unapproved":
        segment["paid_launch_authorized"] = False
    elif change == "wrong_protocol":
        segment["protocol_sha256"] = "b" * 64
    elif change == "duplicate_db":
        segment["databases"][1] = segment["databases"][0]
    elif change == "production_db":
        segment["databases"][0] = "veetbot"
    elif change == "existing":
        Path(protocol["state_directory"]).mkdir()
    elif change == "missing_reopen":
        segment["initialize_store"] = False
    else:
        segment["initialize_deadline"] = False
    if change == "ref":
        args["protocol_path"].write_text(json.dumps(protocol))
        args["protocol_sha256"] = hashlib.sha256(args["protocol_path"].read_bytes()).hexdigest()
        segment["protocol_sha256"] = args["protocol_sha256"]
    if change not in {"segment_hash", "protocol_hash", "symlink"}:
        args["segment_path"].write_text(json.dumps(segment))
        args["segment_sha256"] = hashlib.sha256(args["segment_path"].read_bytes()).hexdigest()
    with pytest.raises((ValueError, OSError)):
        module.verify_invocation(**args)


@pytest.mark.parametrize("cancelled", [False, True])
def test_incomplete_or_cancelled_segment_has_safe_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cancelled: bool
) -> None:
    import asyncio
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collector",
            "--protocol",
            "/protocol",
            "--protocol-sha256",
            "a",
            "--segment",
            "/segment",
            "--segment-sha256",
            "b",
        ],
    )
    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    monkeypatch.setattr(module, "verify_invocation", lambda *_a: ({}, {}))
    monkeypatch.setattr(module, "_bootstrap", lambda *_a: None)

    async def stopped(*_args: Any) -> dict[str, Any]:
        if cancelled:
            raise asyncio.CancelledError
        return {"state": "incomplete", "completed_slots": 410, "required_slots": 1600}

    monkeypatch.setattr(module, "_run", stopped)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] in {"stopped", "incomplete"}
