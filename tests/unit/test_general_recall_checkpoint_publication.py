"""Synthetic review records exercise validation, never impersonate a real reviewer."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_core.evals import general_recall_checkpoint_publication as module
from agent_core.evals.general_recall_checkpoint import _digest, _json
from agent_core.evals.general_recall_checkpoint_collection import CheckpointCollection
from tests.unit.test_general_recall_checkpoint_scoring import fixture

pytestmark = pytest.mark.static


def publication_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    bundle = fixture()

    def write(name: str, value: Any) -> Path:
        path = tmp_path / name
        path.write_bytes(_json(value))
        return path

    approval = write("approval.json", {"synthetic_owner_approval": True})
    operator = write("operator.json", {"collector_commit": bundle.lineage.collector_commit})
    review_items = (
        {"kind": "parent_uncertainty", "ledger_sha256": "a" * 64, "held_usd": "7.83424"},
    )
    manifest = _json({"protocol_sha256": _digest(operator.read_bytes())})
    observed_sha = _digest(
        b"".join(r.model_dump_json().encode() + b"\n" for r in bundle.observations)
    )
    independence = write(
        "independence.json",
        {
            "version": "recall-independence-review@1",
            "reviewer": "Synthetic fixture human",
            "reviewed_at": bundle.preregistration.registered_at.isoformat(),
            "decision": "accepted",
            "corpus_sha256": bundle.identity.corpus_sha256,
            "scenario_manifest_sha256": bundle.identity.scenario_manifest_sha256,
            "reviewed_scenarios": [s.id for s in bundle.scenarios],
        },
    )
    identity = bundle.identity.model_copy(
        update={"independence_review_sha256": _digest(independence.read_bytes())}
    )
    identity_sha = _digest(identity.model_dump_json().encode())
    observations = tuple(
        r.model_copy(update={"identity_sha256": identity_sha}) for r in bundle.observations
    )
    observed_sha = _digest(b"".join(r.model_dump_json().encode() + b"\n" for r in observations))
    failures = write(
        "failures.json",
        {
            "version": "recall-checkpoint-failure-review@1",
            "reviewer": "Synthetic fixture human",
            "reviewer_kind": "human",
            "independent_of_implementation": True,
            "reviewed_at": max(r.observed_at for r in observations).isoformat(),
            "decision": "accepted",
            "identity_sha256": identity_sha,
            "observations_sha256": observed_sha,
            "review_items_sha256": _digest(_json(review_items)),
            "reviewed_items": [_digest(_json(item)) for item in review_items],
            "reviewed_scenarios": [s.id for s in bundle.scenarios],
        },
    )
    gates = []
    receipts = []
    for gate in range(1, 12):
        path = write(
            f"gate-{gate}.json",
            {
                "version": "recall-gate-report@1",
                "identity_sha256": identity_sha,
                "gate": gate,
                "checks": [{"id": "verified", "passed": True}],
            },
        )
        gates.append(path)
        receipts.append(
            bundle.gates[gate - 1].model_copy(
                update={
                    "identity_sha256": identity_sha,
                    "report_sha256": _digest(path.read_bytes()),
                    "passed": 1,
                    "failed": 0,
                }
            )
        )
    lineage = bundle.lineage.model_copy(
        update={
            "parent_identity_sha256": identity_sha,
            "approval_sha256": _digest(approval.read_bytes()),
            "collector_manifest_sha256": _digest(operator.read_bytes()),
            "collection_manifest_sha256": _digest(manifest),
        }
    )
    bundle = bundle.model_copy(
        update={
            "identity": identity,
            "observations": observations,
            "gates": tuple(receipts),
            "lineage": lineage,
        }
    )
    path = tmp_path / "bundle.json"
    path.write_text(bundle.model_dump_json())
    collected = CheckpointCollection(
        bundle.scenarios,
        observations,
        Decimal("140"),
        Decimal("7.83424"),
        manifest,
        review_items,
    )
    monkeypatch.setattr(
        module, "compile_checkpoint_collection", lambda *_a: collected, raising=False
    )
    monkeypatch.setattr(module, "require_committed_tree", lambda *_a: None, raising=False)
    monkeypatch.setattr(
        module,
        "capture_inputs",
        lambda *_a: SimpleNamespace(digest=identity.evaluated_inputs_sha256),
        raising=False,
    )
    monkeypatch.setattr(
        module, "checkpoint_scorer_digest", lambda *_a: lineage.scorer_sha256, raising=False
    )
    parent = SimpleNamespace(
        identity=identity,
        identity_sha256=identity_sha,
        preregistration=bundle.preregistration,
        archive=SimpleNamespace(sha256=lineage.parent_archive_sha256),
    )
    store = SimpleNamespace(plan=SimpleNamespace(encoded=lambda: b""))
    monkeypatch.setattr(module, "_plan_digest", lambda *_a: lineage.plan_sha256, raising=False)
    return {
        "root": tmp_path,
        "frozen_root": tmp_path / "frozen",
        "bundle_path": path,
        "store": store,
        "parent": parent,
        "parents": (),
        "approval": approval,
        "operator_manifest": operator,
        "independence_review": independence,
        "failure_review": failures,
        "gate_reports": tuple(gates),
        "output": tmp_path / "publication",
    }


def test_publication_retains_unknown_costs_and_cannot_activate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = publication_fixture(tmp_path, monkeypatch)
    path = module.publish_checkpoint_release(**args)
    assert path.read_bytes() == args["bundle_path"].read_bytes()
    provenance = json.loads((path.parent / "provenance.json").read_bytes())
    assert provenance["held_usd"] == "7.83424"
    assert provenance["release_sha256"] == _digest(path.read_bytes())
    assert provenance["activation_eligible"] is False
    assert (path.parent / "collection.json").is_file()
    with pytest.raises(ValueError, match=r"existing|exists"):
        module.publish_checkpoint_release(**args)


@pytest.mark.parametrize(
    "change",
    [
        "machine",
        "not_independent",
        "missing_interruptions",
        "wrong_observations",
        "missing_difficult",
        "failed_gate",
        "gate_identity",
        "different_corpus",
        "late_corpus",
        "changed_protocol",
        "changed_source",
        "changed_scorer",
        "unbound_manifest",
    ],
)
def test_publication_refuses_missing_human_review_or_foreign_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    args = publication_fixture(tmp_path, monkeypatch)

    def edit(path: Path, **changes: Any) -> None:
        data = json.loads(path.read_bytes())
        data.update(changes)
        path.write_bytes(_json(data))

    if change == "machine":
        edit(args["failure_review"], reviewer_kind="agent")
    elif change == "not_independent":
        edit(args["failure_review"], independent_of_implementation=False)
    elif change == "missing_interruptions":
        edit(args["failure_review"], reviewed_items=[])
    elif change == "wrong_observations":
        edit(args["failure_review"], observations_sha256="f" * 64)
    elif change == "missing_difficult":
        edit(args["failure_review"], reviewed_scenarios=[])
    elif change == "failed_gate":
        edit(args["gate_reports"][0], checks=[{"id": "failed", "passed": False}])
    elif change == "gate_identity":
        edit(args["gate_reports"][0], identity_sha256="f" * 64)
    elif change == "different_corpus":
        edit(args["independence_review"], corpus_sha256="f" * 64)
    elif change == "late_corpus":
        edit(args["independence_review"], reviewed_at="2099-01-01T00:00:00Z")
    elif change == "changed_protocol":
        edit(args["operator_manifest"], collector_commit="f" * 40)
    elif change == "changed_source":
        monkeypatch.setattr(module, "capture_inputs", lambda *_a: SimpleNamespace(digest="f" * 64))
    elif change == "changed_scorer":
        monkeypatch.setattr(module, "checkpoint_scorer_digest", lambda *_a: "f" * 64)
    else:
        edit(
            args["bundle_path"],
            lineage={
                **json.loads(args["bundle_path"].read_bytes())["lineage"],
                "collection_manifest_sha256": "f" * 64,
            },
        )
    with pytest.raises(ValueError):
        module.publish_checkpoint_release(**args)
    assert not args["output"].exists()
