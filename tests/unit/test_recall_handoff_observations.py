"""Independent controller observations use real file bytes and effective permissions."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_core.operations import recall_handoff_observe as observer

pytestmark = pytest.mark.static


def test_host_observation_excludes_only_release_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "APP", tmp_path)
    monkeypatch.setattr(observer, "run", lambda *argv: b"synthetic read-only host metadata")
    # Darwin lacks the Linux ACL APIs. Supply an empty Linux extended ACL in this fixture.
    monkeypatch.setattr(os, "listxattr", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **kw: b"", raising=False)
    inputs = tmp_path / "operator-settings"
    inputs.write_text("owner-only")
    profile = observer.HostProfile(
        settings=(inputs,),
        overlays=(inputs,),
        assets=(inputs,),
        units=tuple(
            f"veetbot-{role}.service"
            for role in (
                "api",
                "worker",
                "async-worker",
                "maintenance",
                "schedule",
                "call",
                "surface",
            )
        ),
        rollback_release="20261030-000000-bbbbbbb",
        rollback_commit="b" * 40,
        recovery_paths=(inputs, inputs, inputs),
        recovery_images=("app", "browser", "builder"),
        recovery_inventory_sha256="0" * 64,
        recovery_schema_sha256="1" * 64,
        recovery_guard_receipt_sha256="2" * 64,
    )
    before, after = "20261031-000000-aaaaaaa", "20261031-000001-ccccccc"
    for rel in (before, after):
        directory = tmp_path / "releases" / rel
        directory.mkdir(parents=True)
        (directory / ".release.env").write_text(f"VEETBOT_RELEASE_ID={rel}\nSCOPE=owner\n")
        (directory / ".owner-scopes.env").write_text("AUTH_SCOPES=memory.read")
    expected = observer.environment(profile, before)
    assert observer.environment(profile, after) == expected
    scopes = tmp_path / "releases" / after / ".owner-scopes.env"
    scopes.write_text("AUTH_SCOPES=memory.read,memory.write")
    changed = observer.environment(profile, after)
    assert changed.permissions_sha256 != expected.permissions_sha256
    inputs.chmod(0o777)
    assert observer.environment(profile, before).settings_sha256 != expected.settings_sha256


@pytest.mark.parametrize("reject", ["none", "tree", "review", "ci"])
def test_clearance_reuses_only_the_reviewed_and_verified_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject: str
) -> None:
    monkeypatch.setattr(observer, "CONTROLLER", tmp_path)
    from agent_core.memory import recall_handoff

    monkeypatch.setattr(recall_handoff, "OPERATOR_UID", os.getuid())
    target = "20261031-000000-ccccccc"
    head, tree = "d" * 40, "e" * 40
    record = tmp_path / "reviewed-heads" / head
    record.mkdir(parents=True)
    identity = {
        "release": target,
        "merge": "c" * 40,
        "reviewed_head": head,
        "reviewed_tree": tree,
        "merge_tree": tree if reject != "tree" else "a" * 40,
    }
    monkeypatch.setattr(observer, "merge_identity", lambda _: identity)
    review: dict[str, Any] = {
        "final_head": head,
        "all_review_forms_inspected": True,
        "all_threads_resolved": True,
        "remaining_actionable_findings": int(reject == "review"),
    }
    (record / "review-clearance.json").write_text(json.dumps(review))
    ci = {"head": head, "reviewed_tree": tree, "all_success": reject != "ci"}
    (record / "hosted-ci-evidence.json").write_text(json.dumps(ci))
    if reject == "none":
        assert observer.clearance(target).target_commit == identity["merge"]
    else:
        with pytest.raises(ValueError):
            observer.clearance(target)


@pytest.mark.parametrize(
    "change",
    [
        "normal",
        "open_thread",
        "untrusted_approver",
        "author",
        "old_head",
        "late_check",
        "wrong_issuer",
        "missing_job",
    ],
)
def test_trusted_final_pr_can_supply_clearance_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Normal release approval suffices when every independently observed fact is current."""
    from agent_core.memory import recall_handoff

    monkeypatch.setattr(recall_handoff, "OPERATOR_UID", os.getuid())
    monkeypatch.setattr(observer, "CONTROLLER", tmp_path)
    release = "20261031-040000-ccccccc"
    head, tree = "d" * 40, "e" * 40
    identity = {
        "release": release,
        "merge": "c" * 40,
        "reviewed_head": head,
        "reviewed_tree": tree,
        "merge_tree": tree,
        "pr": "42",
        "author_id": "5",
        "merged_at": "2026-10-31T04:00:00Z",
    }
    gates: dict[str, Any] = {
        "approver_ids": [6],
        "coderabbit_app_id": 7,
        "circleci_creator_id": 8,
        "required_contexts": ["ci/circleci: static", "ci/circleci: contract"],
    }
    (tmp_path / "github-gates.json").write_text(json.dumps(gates))
    monkeypatch.setattr(observer, "merge_identity", lambda _: identity)

    def read(path: str, query: dict[str, Any] | None = None) -> Any:
        if path == "/graphql":
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"id": "thread", "isResolved": change != "open_thread"}],
                            }
                        }
                    }
                }
            }
        if "/reviews?" in path:
            return [
                {
                    "id": 12,
                    "state": "APPROVED",
                    "commit_id": "a" * 40 if change == "old_head" else head,
                    "user": {
                        "id": 5
                        if change == "author"
                        else 9
                        if change == "untrusted_approver"
                        else 6
                    },
                    "submitted_at": "2026-10-31T03:50:00Z",
                }
            ]
        if "/check-runs?" in path:
            return {
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 1,
                        "name": "CodeRabbit",
                        "app": {"id": 7},
                        "conclusion": "success",
                        "head_sha": head,
                        "status": "completed",
                        "completed_at": "2026-10-31T03:40:00Z",
                    }
                ],
            }
        rows = [
            {
                "id": i,
                "creator": {"id": 9 if change == "wrong_issuer" else 8},
                "state": "success",
                "context": name,
                "updated_at": "2026-10-31T05:00:00Z"
                if change == "late_check"
                else "2026-10-31T03:55:00Z",
            }
            for i, name in enumerate(gates["required_contexts"])
        ]
        if "/statuses?" in path:
            return rows
        if change == "missing_job":
            rows.pop()
        for row in rows:
            row.pop("creator")
        return {"total_count": len(rows), "state": "success", "sha": head, "statuses": rows}

    monkeypatch.setattr(observer, "github", read, raising=False)
    if change == "normal":
        assert observer.clearance(release).target_commit == identity["merge"]
    else:
        with pytest.raises(ValueError):
            observer.clearance(release)
