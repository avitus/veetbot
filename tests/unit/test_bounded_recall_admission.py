"""Synthetic owner findings exercise refusal; none is production review evidence."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.domain.general_recall_release import RecallReleaseIdentity
from agent_core.memory.bounded_recall_admission import bind_bounded_recall
from agent_core.memory.recall_release_binding import (
    InputFile,
    RecallInputManifest,
    capture_inputs,
    input_dependencies,
)
from tests.contract.support import principal
from tests.unit.test_general_recall_checkpoint_scoring import fixture

pytestmark = pytest.mark.static
NOW = datetime(2026, 11, 1, tzinfo=UTC)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def files(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = tmp_path / "source"
    root.mkdir()
    (root / "runtime.py").write_text("new runtime")
    value = fixture().model_dump(mode="json")
    deps = input_dependencies(value["identity"])
    measured = RecallInputManifest(
        files={"runtime.py": InputFile(sha256=digest(b"old runtime"), executable=False)},
        dependencies=deps,
    )
    value["identity"]["evaluated_inputs_sha256"] = measured.digest
    identity = digest(
        RecallReleaseIdentity.model_validate(value["identity"]).model_dump_json().encode()
    )
    for row in (*value["observations"], *value["gates"]):
        row["identity_sha256"] = identity
    value["lineage"].update(
        parent_identity_sha256=identity, evaluated_inputs_sha256=measured.digest
    )
    bundle = CheckpointReleaseBundle.model_validate(value)
    target = capture_inputs(root, deps)
    items = [{"kind": "uncertain_usage", "amount_usd": "2"}]
    observations = b"".join(r.model_dump_json().encode() + b"\n" for r in bundle.observations)
    review = {
        "version": "recall-owner-review-submission@1",
        "reviewer": "Synthetic test reviewer",
        "reviewer_kind": "human",
        "reviewed_at": NOW.isoformat(),
        "decision": "accept_bounded_rollout",
        "findings": (
            "Synthetic review: accept recorded quality failures only within "
            "the owner rollout and rollback limits."
        ),
        "observations_sha256": digest(observations),
        "review_items_sha256": digest(encoded(items) + b"\n"),
        "reviewed_scenarios": [s.id for s in bundle.scenarios],
        "reviewed_items": [digest(encoded(i)) for i in items],
        "independent_of_implementation": True,
    }
    evidence = {
        "version": "recall-bounded-evidence@1",
        "bundle": value,
        "review_items": items,
        "human_review": review,
        "owner_authorization_sha256": "1" * 64,
        "provenance_verification_sha256": "2" * 64,
        "retained_archives": ["3" * 64],
        "measured_inputs": measured.model_dump(mode="json"),
        "compatibility": {
            "target_inputs_sha256": target.digest,
            "changes": {
                "runtime.py": {
                    "before": measured.files["runtime.py"].model_dump(),
                    "after": target.files["runtime.py"].model_dump(),
                    "rationale": "Synthetic changed runtime verified by the recorded check.",
                    "checks": ["full-check"],
                }
            },
            "dependency_changes": {},
            "checks": [
                {
                    "id": "full-check",
                    "command": "make check",
                    "exit_code": 0,
                    "inputs_sha256": target.digest,
                    "report_sha256": "4" * 64,
                }
            ],
        },
    }
    admission = {
        "version": "recall-bounded-admission@1",
        "deployment_build_ref": "a" * 40,
        "evidence_sha256": digest(encoded(evidence)),
        "target_inputs": target.model_dump(mode="json"),
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "activated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=7)).isoformat(),
        "rollback_build_ref": "b" * 40,
    }
    kwargs = {
        "root": root,
        "manifest": tmp_path / "manifest.json",
        "evidence": tmp_path / "evidence.json",
        "release_id": "release-aaaaaaa",
        "dependencies": {
            k: deps[k]
            for k in (
                "configuration_sha256",
                "embedding_configuration_sha256",
                "formation_configuration_sha256",
                "reader_configuration_sha256",
            )
        },
        "principal": principal(),
        "clock": Clock(),
    }
    write(kwargs, admission, evidence)
    return kwargs, admission, evidence


def write(kwargs: dict[str, Any], admission: dict[str, Any], evidence: dict[str, Any]) -> None:
    admission["evidence_sha256"] = digest(encoded(evidence))
    kwargs["evidence"].write_bytes(encoded(evidence))
    kwargs["manifest"].write_bytes(encoded(admission))


def test_bounded_admission_expires_and_withdrawal_cannot_silently_revive(tmp_path: Path) -> None:
    kwargs, _, _ = files(tmp_path)
    admitted = bind_bounded_recall(**kwargs)
    assert admitted()
    kwargs["clock"].value += timedelta(days=7)
    assert not admitted()
    kwargs["clock"].value = NOW
    assert not admitted()


def test_public_manifest_router_requires_real_owner_and_clock(tmp_path: Path) -> None:
    from agent_core.memory.semantic_deployment import bind_index_manifest

    kwargs, _, _ = files(tmp_path)
    assert bind_index_manifest(**kwargs)()
    kwargs.pop("clock")
    with pytest.raises(ValueError, match="owner and clock"):
        bind_index_manifest(**kwargs)


@pytest.mark.parametrize(
    "change",
    [
        "human",
        "findings",
        "decision",
        "review-time",
        "review-item",
        "scenario",
        "row",
        "archive",
        "owner",
        "tenant",
        "release",
        "expiry",
        "future",
        "configuration",
        "source",
        "new-file",
        "unassessed",
        "unverified",
        "before",
        "measured",
        "check-input",
        "check-failed",
        "rollback",
    ],
)
def test_bounded_admission_refuses_missing_review_and_unassessed_inputs(
    tmp_path: Path, change: str
) -> None:
    kwargs, admission, evidence = files(tmp_path)
    if change == "human":
        evidence["human_review"]["reviewer_kind"] = "agent"
    elif change == "findings":
        evidence["human_review"]["findings"] = " "
    elif change == "decision":
        evidence["human_review"]["decision"] = "reject"
    elif change == "review-time":
        evidence["human_review"]["reviewed_at"] = (NOW + timedelta(days=1)).isoformat()
    elif change == "review-item":
        evidence["human_review"]["reviewed_items"] = []
    elif change == "scenario":
        evidence["human_review"]["reviewed_scenarios"] = []
    elif change == "row":
        evidence["bundle"]["observations"][0]["cost_usd"] += 0.1
    elif change == "archive":
        evidence["retained_archives"] = []
    elif change == "owner":
        admission["principal_id"] = "someone-else"
    elif change == "tenant":
        admission["tenant_id"] = "someone-else"
    elif change == "release":
        kwargs["release_id"] = "release-ccccccc"
    elif change == "expiry":
        admission["expires_at"] = (NOW + timedelta(days=8)).isoformat()
    elif change == "future":
        admission["activated_at"] = (NOW + timedelta(days=1)).isoformat()
    elif change == "configuration":
        kwargs["dependencies"]["reader_configuration_sha256"] = "9" * 64
    elif change == "source":
        (kwargs["root"] / "runtime.py").write_text("foreign")
    elif change == "new-file":
        (kwargs["root"] / "extra.py").write_text("foreign")
    elif change == "unassessed":
        evidence["compatibility"]["changes"] = {}
    elif change == "unverified":
        evidence["compatibility"]["changes"]["runtime.py"]["checks"] = ["absent"]
    elif change == "before":
        evidence["compatibility"]["changes"]["runtime.py"]["before"]["sha256"] = "9" * 64
    elif change == "measured":
        evidence["measured_inputs"]["files"]["runtime.py"]["sha256"] = "9" * 64
    elif change == "check-input":
        evidence["compatibility"]["checks"][0]["inputs_sha256"] = "9" * 64
    elif change == "check-failed":
        evidence["compatibility"]["checks"][0]["exit_code"] = 1
    elif change == "rollback":
        admission["rollback_build_ref"] = admission["deployment_build_ref"]
    write(kwargs, admission, evidence)
    with pytest.raises(ValueError):
        bind_bounded_recall(**kwargs)


@pytest.mark.parametrize("changed", ["manifest", "evidence", "source"])
def test_each_bound_artifact_mutation_latches_withdrawal(tmp_path: Path, changed: str) -> None:
    kwargs, _, _ = files(tmp_path)
    admitted = bind_bounded_recall(**kwargs)
    target = kwargs["root"] / "runtime.py" if changed == "source" else kwargs[changed]
    original = target.read_bytes()
    target.write_bytes(b"changed")
    assert not admitted()
    target.write_bytes(original)
    assert not admitted()
