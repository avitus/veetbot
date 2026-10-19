"""The repair compatibility exception cannot hide a model or behavior change."""

import gzip
import hashlib
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_recall_checkpoint import CheckpointStore
from agent_core.evals.general_recall_formation_repair import verify_deadline_source

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "src/agent_core/memory/distillation.py"


def original_source() -> bytes:
    # A shallow checkout or release archive must not depend on old Git objects.
    raw = gzip.decompress(
        (ROOT / "tests/fixtures/general_recall/formation-16153a5f.py.gz").read_bytes()
    )
    assert (
        hashlib.sha256(raw).hexdigest()
        == "e7ce0e0f0e93f2279126ba8a7d6f9e554222c22f2c9fd074836ed48c2bad6823"
    )
    return raw


def test_only_declared_deadline_source_change_is_compatible() -> None:
    verify_deadline_source(original_source(), (ROOT / SOURCE).read_bytes())


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("default_stage_timeout_seconds = 600.0", "default_stage_timeout_seconds = 900.0"),
        (
            "DISTILLATION_MAXIMUM_OUTPUT_TOKENS = 16_384",
            "DISTILLATION_MAXIMUM_OUTPUT_TOKENS = 32_768",
        ),
        ('"Favor useful recall over timidity. ', '"Favor short answers. '),
        ("temperature=0,", "temperature=1,"),
        ("stream_idle_seconds=min(10.0,", "stream_idle_seconds=min(60.0,"),
        ("candidates: list[_PeopleDistilledCandidate]", "candidates: list[_DistilledCandidate]"),
        ("_safe_failure(exc), metric", "_safe_failure(ValueError()), metric"),
    ],
)
def test_model_request_schema_and_parser_changes_are_not_deadline_compatibility(
    before: str, after: str
) -> None:
    source = (ROOT / SOURCE).read_text()
    assert before in source
    with pytest.raises(ValueError, match="deadline repair"):
        verify_deadline_source(original_source(), source.replace(before, after).encode())


def test_extra_runtime_code_is_not_hidden_by_compatibility() -> None:
    with pytest.raises(ValueError, match="deadline repair"):
        verify_deadline_source(original_source(), (ROOT / SOURCE).read_bytes() + b"\nx = 1\n")


def repair_fixture(tmp_path: Path) -> tuple[CheckpointStore, dict[str, Any]]:

    from agent_core.evals.general_recall_checkpoint import _digest, _json, _read
    from agent_core.evals.general_recall_checkpoint_recovery import (
        admit_bounded_recovery,
        recovery_inventory,
    )
    from tests.unit.test_general_recall_checkpoint_recovery import PREPARATION, READER, fixture

    store, previous = fixture(tmp_path)
    admit_bounded_recovery(store, **previous)
    store.begin(READER)
    row = _json(
        {
            "identity_sha256": store.plan.identity_sha256,
            "scenario_id": READER[0],
            "path": READER[1],
            "pipeline": READER[2],
            "channel": READER[3],
        }
    )
    store.complete(READER, row, validate=lambda _: None)
    store.begin(PREPARATION)
    entry = {
        "index": 1,
        "provider": "openai",
        "model": "frozen",
        "slot": PREPARATION,
        "request_sha256": "9" * 64,
        "reservation_usd": "1",
    }
    journal = _json({**entry, "outcome": "reserved"}) + _json(
        {**entry, "outcome": "uncertain", "error_class": "CancelledError"}
    )
    (store.directory / "budget/000002.jsonl").write_bytes(journal)
    (store.directory / "budget/000002.sealed.json").write_bytes(
        _json({"journal_sha256": _digest(journal), "reported_usd": "0", "held_usd": "1"})
    )
    store.fail(
        PREPARATION, stage="formation", error_class="CancelledError", ledger_sha256=_digest(journal)
    )
    event = store._states[PREPARATION]
    report = _json(
        {
            "ledger_sha256": _digest(journal),
            "attempts": [
                {
                    "key": PREPARATION,
                    "state": "failed",
                    "stage": "formation",
                    "error_class": "CancelledError",
                }
            ],
        }
    )
    report_file = "segments/" + _digest(report) + ".json"
    (store.directory / report_file).write_bytes(report)
    bank_file = (
        "banks/" + _digest(PREPARATION[0].encode()) + ".final-replacement.json.pending.jsonl"
    )
    (store.directory / bank_file).write_bytes(b'{"state":"reserved"}\n')
    artifacts = recovery_inventory(store.directory)
    artifacts.update(
        {"recovery/" + p.name: _digest(_read(p)) for p in (store.directory / "recovery").iterdir()}
    )
    owner = _json(
        {
            "exact_owner_instruction": (
                "I approve the formation repair, including runtime changes. "
                "Fix the root cause, preserve the 413 observations "
                "and raise the cap to $500 if necessary"
            ),
            "completed_observations": 2,
            "new_allocation_usd": "0",
        }
    )
    assessment = _json(
        {
            "completed_rows_preserved": 2,
            "provider_calls_this_action": 0,
            "source_sha256": _digest(original_source()),
        }
    )
    manifest = _json(
        {
            "version": "recall-formation-deadline-repair@1",
            "plan_sha256": _digest(store.plan.encoded()),
            "predecessor_protocol_sha256": previous["protocol_sha256"],
            "history_events": store._count,
            "history_sha256": store._previous,
            "artifacts": artifacts,
            "owner_sha256": _digest(owner),
            "assessment_sha256": _digest(assessment),
            "completed_rows": 2,
            "key": PREPARATION,
            "failed_event": event.index,
            "ledger_file": "budget/000002.jsonl",
            "report_file": report_file,
            "bank_file": bank_file,
            "original_source_sha256": _digest(original_source()),
            "repaired_source_sha256": _digest((ROOT / SOURCE).read_bytes()),
        }
    )
    return store, {
        "manifest": manifest,
        "owner": owner,
        "assessment": assessment,
        "original_source": original_source(),
        "repaired_source": (ROOT / SOURCE).read_bytes(),
        "protocol_sha256": "f" * 64,
    }


def test_repair_preserves_every_original_and_is_single_use(tmp_path: Path) -> None:
    from agent_core.evals.general_recall_checkpoint import CheckpointStore, _read
    from agent_core.evals.general_recall_formation_repair import admit_formation_repair
    from tests.unit.test_general_recall_checkpoint_recovery import PREPARATION, READER

    store, args = repair_fixture(tmp_path)
    original = {
        p.relative_to(store.directory): p.read_bytes()
        for p in store.directory.rglob("*")
        if p.is_file() and p.name != ".lock"
    }
    with store:
        admit_formation_repair(store, **args)
        assert len(store.completed()) == 2 and PREPARATION in store.pending()
        assert not store.begin(READER)
        for name, raw in original.items():
            assert _read(store.directory / name) == raw
    with CheckpointStore(store.directory, store.plan) as reopened:
        admit_formation_repair(reopened, **args)
        assert reopened.begin(PREPARATION)
        reopened.fail(
            PREPARATION, stage="formation", error_class="TimeoutError", ledger_sha256="a" * 64
        )
        assert reopened.pending() == ()
        with pytest.raises(ValueError):
            admit_formation_repair(reopened, **args)


@pytest.mark.parametrize(
    "damage",
    ["owner", "source", "result", "omission", "deadline", "ledger", "bank", "report", "key"],
)
def test_repair_refuses_unbound_or_changed_evidence_before_append(
    tmp_path: Path, damage: str
) -> None:
    import json

    from agent_core.evals.general_recall_checkpoint import _json
    from agent_core.evals.general_recall_formation_repair import admit_formation_repair

    store, args = repair_fixture(tmp_path)
    manifest = json.loads(args["manifest"])
    count = store._count
    if damage == "owner":
        args["owner"] = b"{}"
    elif damage == "source":
        args["repaired_source"] += b"\nx = 1\n"
    elif damage == "key":
        manifest["key"][0] = "case-0"
    elif damage == "omission":
        manifest["artifacts"].pop(
            next(n for n in manifest["artifacts"] if n.startswith("results/"))
        )
    else:
        name = {
            "result": next(n for n in manifest["artifacts"] if n.startswith("results/")),
            "deadline": "deadline.json",
            "ledger": manifest["ledger_file"],
            "bank": manifest["bank_file"],
            "report": manifest["report_file"],
        }[damage]
        (store.directory / name).write_bytes(b"{}\n")
    args["manifest"] = _json(manifest)
    with store, pytest.raises(ValueError):
        admit_formation_repair(store, **args)
    assert store._count == count


def test_repair_preserves_three_protocol_epochs_and_uses_new_bank(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent
    from agent_core.evals.general_recall_checkpoint_recovery import (
        protocol_ancestry,
        protocol_for_segment,
    )
    from agent_core.evals.general_recall_formation_repair import admit_formation_repair
    from tests.unit.test_general_recall_checkpoint_recovery import PREPARATION

    store, args = repair_fixture(tmp_path)
    with store:
        admit_formation_repair(store, **args)
        assert protocol_ancestry(store, args["protocol_sha256"])[0] == "d" * 64
        assert [protocol_for_segment(store, args["protocol_sha256"], i) for i in (1, 2, 3, 4)] == [
            "d" * 64,
            "e" * 64,
            "f" * 64,
            "f" * 64,
        ]
        parent: Any = SimpleNamespace(
            by_id={PREPARATION[0]: None}, banks={}, pending_banks={PREPARATION[0]: None}
        )
        bank = CheckpointParent.bank_path(parent, store, PREPARATION[0])
        assert bank.name.endswith(".deadline-repair.json") and not bank.exists()
        with pytest.raises(ValueError):
            protocol_ancestry(store, "e" * 64)


def test_repair_rows_have_distinct_execution_provenance_without_retagging_old_rows(
    tmp_path: Path,
) -> None:
    from agent_core.evals.general_recall_formation_repair import (
        admit_formation_repair,
        execution_claim,
        validate_execution_claim,
    )

    store, args = repair_fixture(tmp_path)
    with store:
        admit_formation_repair(store, **args)
        claim = execution_claim(store)
        assert claim and claim["base_identity_sha256"] == store.plan.identity_sha256
        assert claim["execution_identity_sha256"] != store.plan.identity_sha256
        validate_execution_claim(claim, {}, repaired=False)
        with pytest.raises(ValueError):
            validate_execution_claim(claim, {}, repaired=True)
        row = {"execution_repair": claim}
        validate_execution_claim(claim, row, repaired=True)
        with pytest.raises(ValueError):
            validate_execution_claim(claim, row, repaired=False)
        row["execution_repair"] = {**claim, "repaired_source_sha256": "0" * 64}
        with pytest.raises(ValueError):
            validate_execution_claim(claim, row, repaired=True)
