"""Runtime control collection must observe the store, never copy expected labels."""

from pathlib import Path

import pytest

from agent_core.evals.memory_reconsolidation import load_corpus
from agent_core.evals.memory_reconsolidation_control import (
    ControlBudget,
    collect_case,
    runtime_case,
)

ROOT = Path(__file__).resolve().parents[2]


async def test_control_collects_originals_and_actual_recall_without_answer_labels() -> None:
    case = load_corpus(ROOT).cases[0]
    source = runtime_case(case)
    assert set(source.model_dump()) == {"id", "seeds", "probes"}
    assert set(source.probes[0].model_dump()) == {"id", "question"}
    result = await collect_case(source)
    assert result.failure is None
    assert result.observation.surviving_ids == ("m1", "m2")
    assert len(result.originals) == 2
    assert len(result.traces) == 1
    trace = result.traces[0]
    # Ordinary retrieval's exact-statement collapse renders only the newest copy.
    # Surviving storage is a different observation from rendered recall.
    assert trace.returned_ids == ("m2",)
    assert "The owner prefers Celsius." in trace.rendered
    assert result.observation.answers == ()
    assert result.observation.merges == ()
    assert result.observation.hypotheses == ()


@pytest.mark.parametrize(
    "category", ["same_evidence", "scope", "attribution", "expiry", "correction"]
)
async def test_control_uses_real_lifecycle_source_identity_and_scope(category: str) -> None:
    case = next(c for c in load_corpus(ROOT).cases if c.id == f"development-{category}")
    result = await collect_case(runtime_case(case))
    assert result.failure is None
    assert set(result.observation.surviving_ids) == set(case.required_survivors)
    trace = result.traces[0]
    if category == "scope":
        assert trace.returned_ids == ()
    elif category == "same_evidence":
        first, second = (row.record for row in result.originals)
        assert first.source_session_id == second.source_session_id
        assert first.source_event_ids == second.source_event_ids
        assert first.evidence_count == second.evidence_count == 1
    elif category == "attribution":
        attributed = result.originals[1].record
        assert attributed.authority.value == "inferred"
        assert attributed.portability.value == "local"
        assert attributed.sensitivity.value == "sensitive"
        assert "m2" not in trace.returned_ids
    elif category == "expiry":
        assert result.expired == 1
        assert result.originals[0].record.status.value == "expired"
        assert "m1" not in trace.returned_ids
    else:
        assert tuple(row.seed_id for row in result.originals) == ("m2",)
        assert "m1" not in trace.returned_ids


async def test_control_records_budget_drops_and_has_no_label_dependent_recall() -> None:
    case = next(c for c in load_corpus(ROOT).cases if c.id == "development-quantity")
    full = await collect_case(runtime_case(case), budget=ControlBudget(min_score=0))
    small = await collect_case(runtime_case(case), budget=ControlBudget(tokens=1, min_score=0))
    assert full.failure is small.failure is None
    assert set(full.traces[0].returned_ids) == {"m1", "m2"}
    assert small.traces[0].returned_ids == ()
    assert set(small.traces[0].dropped_ids) == {"m1", "m2"}
    assert small.traces[0].tokens <= 1
    assert full.originals == small.originals
    changed_labels = case.model_copy(update={"duplicate_classes": (), "hypotheses": ()})
    assert runtime_case(changed_labels) == runtime_case(case)


async def test_control_keeps_runtime_failure_without_source_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.memory.retrieval import HybridMemoryRetriever

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("SECRET source text")

    monkeypatch.setattr(HybridMemoryRetriever, "recall", fail)
    case = load_corpus(ROOT).cases[0]
    result = await collect_case(runtime_case(case))
    assert result.failure == "runtime_failure"
    assert result.case_id == case.id
    assert "SECRET" not in result.model_dump_json()


async def test_control_refuses_a_corrupt_persisted_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_core.adapters.memory.in_memory import InMemoryTraceStore

    original = InMemoryTraceStore.get

    async def corrupted(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        trace = await original(self, *args, **kwargs)
        return trace.model_copy(update={"rendered": "invented recall"})

    monkeypatch.setattr(InMemoryTraceStore, "get", corrupted)
    result = await collect_case(runtime_case(load_corpus(ROOT).cases[0]))
    assert result.failure == "invalid_trace"


async def test_control_preserves_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from agent_core.memory.retrieval import HybridMemoryRetriever

    async def cancelled(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(HybridMemoryRetriever, "recall", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await collect_case(runtime_case(load_corpus(ROOT).cases[0]))


async def test_control_runs_three_complete_repeats_without_omitting_failed_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_core.evals import memory_reconsolidation_control_report as reporting

    rows = await reporting.run_control(ROOT)
    assert len(rows) == 72
    assert {(row.repeat, row.case_id) for row in rows} == {
        (repeat, case.id) for repeat in (1, 2, 3) for case in load_corpus(ROOT).cases
    }
    assert all(row.failure is None for row in rows)
    # Both chronological repeats replay the same store/trace observations.
    assert [r.model_dump(exclude={"repeat"}) for r in rows[:24]] == [
        r.model_dump(exclude={"repeat"}) for r in rows[24:48]
    ]
    output = tmp_path / "control.json"
    reporting.write_control(ROOT, rows, output)
    assert output.is_file()
    with pytest.raises(FileExistsError):
        reporting.write_control(ROOT, rows, output)
    with pytest.raises(ValueError, match="complete"):
        reporting.write_control(ROOT, rows[:-1], tmp_path / "partial.json")
    with pytest.raises(ValueError, match="complete"):
        reporting.write_control(ROOT, (*rows[:-1], rows[0]), tmp_path / "duplicate.json")
    with pytest.raises(ValueError, match="implementation"):
        reporting.write_control(
            ROOT,
            tuple(r.model_copy(update={"implementation_sha256": "0" * 64}) for r in rows),
            tmp_path / "drift.json",
        )
    with pytest.raises(ValueError, match="survivor"):
        altered = rows[0].model_copy(
            update={"observation": rows[0].observation.model_copy(update={"surviving_ids": ()})}
        )
        reporting.write_control(ROOT, (altered, *rows[1:]), tmp_path / "survivors.json")
    with pytest.raises(ValueError, match="budget"):
        trace = rows[0].traces[0]
        altered_trace = trace.model_copy(
            update={"query": trace.query.model_copy(update={"budget_tokens": 1})}
        )
        altered = rows[0].model_copy(update={"traces": (altered_trace,)})
        reporting.write_control(ROOT, (altered, *rows[1:]), tmp_path / "budget.json")
    with pytest.raises(ValueError, match="question"):
        altered_trace = trace.model_copy(
            update={
                "query": trace.query.model_copy(update={"text": "an easier different question"})
            }
        )
        altered = rows[0].model_copy(update={"traces": (altered_trace,)})
        reporting.write_control(ROOT, (altered, *rows[1:]), tmp_path / "question.json")
    with pytest.raises(ValueError, match="seed"):
        first = rows[0].originals[0]
        altered_original = first.model_copy(
            update={
                "record": first.record.model_copy(
                    update={"statement": "A changed unrendered original."}
                )
            }
        )
        altered = rows[0].model_copy(
            update={"originals": (altered_original, *rows[0].originals[1:])}
        )
        reporting.write_control(ROOT, (altered, *rows[1:]), tmp_path / "source.json")

    import asyncio
    import json

    from typer.testing import CliRunner

    from agent_core.cli.main import app

    async def completed(root: Path):  # type: ignore[no-untyped-def]
        assert root == ROOT
        return rows

    command_output = tmp_path / "command.json"
    with monkeypatch.context() as command_patch:
        command_patch.setattr(reporting, "run_control", completed)
        result = await asyncio.to_thread(
            CliRunner().invoke,
            app,
            ["eval", "memory-reconsolidation-control", "--output", str(command_output)],
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == json.loads(command_output.read_text())
    assert json.loads(result.stdout)["answer_evaluation"] == "not_run"

    calls = 0

    async def failed(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise RuntimeError("SECRET")

    monkeypatch.setattr(reporting, "collect_case", failed)
    failed_rows = await reporting.run_control(ROOT)
    assert len(failed_rows) == calls == 72
    assert all(row.failure == "runtime_failure" for row in failed_rows)
    with pytest.raises(ValueError, match="failed"):
        reporting.write_control(ROOT, failed_rows, tmp_path / "failed.json")


def test_control_command_is_available_as_an_offline_recording_surface() -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    result = CliRunner().invoke(app, ["eval", "memory-reconsolidation-control", "--help"])
    assert result.exit_code == 0
    assert "--output" in result.stdout
    assert "offline" in result.stdout


def test_control_command_preserves_existing_output_and_redacts_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app
    from agent_core.evals import memory_reconsolidation_control_report as reporting

    output = tmp_path / "control.json"
    output.write_text("preserve this recording")
    calls = 0

    async def fail(root: Path) -> tuple[()]:
        nonlocal calls
        calls += 1
        raise ValueError("SECRET source text")

    monkeypatch.setattr(reporting, "run_control", fail)
    result = CliRunner().invoke(
        app, ["eval", "memory-reconsolidation-control", "--output", str(output)]
    )
    assert result.exit_code == 1 and calls == 0
    assert output.read_text() == "preserve this recording"
    absent = tmp_path / "absent.json"
    result = CliRunner().invoke(
        app, ["eval", "memory-reconsolidation-control", "--output", str(absent)]
    )
    assert result.exit_code == 1 and calls == 1
    assert not absent.exists()
    assert "SECRET" not in result.output


def test_control_command_reports_failed_case_census_without_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from typer.testing import CliRunner

    from agent_core.cli.main import app
    from agent_core.evals import memory_reconsolidation_control_report as reporting
    from agent_core.evals.memory_reconsolidation import Observation
    from agent_core.evals.memory_reconsolidation_control import ControlCaseResult

    async def fail(root: Path) -> tuple[ControlCaseResult, ...]:
        return (
            ControlCaseResult(
                case_id="failed",
                repeat=1,
                observation=Observation(case_id="failed"),
                failure="runtime_failure",
            ),
        )

    monkeypatch.setattr(reporting, "run_control", fail)
    output = tmp_path / "control.json"
    result = CliRunner().invoke(
        app, ["eval", "memory-reconsolidation-control", "--output", str(output)]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"cases": 1, "failed_cases": 1}
    assert not output.exists()


def test_frozen_recording_is_complete_and_cannot_claim_answer_quality() -> None:
    from agent_core.evals.memory_reconsolidation_control_report import load_recorded_control

    recording = load_recorded_control(ROOT)
    assert len(recording.rows) == 72
    assert recording.answer_evaluation == "not_run"
    assert recording.activation_evidence is False
    assert recording.provider_calls == 0
    assert all(
        score.counts.lost_facts == score.counts.false_merge_pairs == 0 for score in recording.scores
    )


@pytest.mark.parametrize("change", ["bytes", "counts", "question"])
def test_frozen_recording_refuses_drift_and_recomputes_its_claims(
    tmp_path: Path, change: str
) -> None:
    import hashlib
    import json
    from shutil import copyfile

    from agent_core.evals.memory_reconsolidation import (
        CONTROL_PATHS,
        CORPUS_PATH,
        MANIFEST_PATH,
        SCORER_PATHS,
    )
    from agent_core.evals.memory_reconsolidation_control_report import (
        BASELINE_PATH,
        RECORDING_MANIFEST_PATH,
        load_recorded_control,
    )

    for path in (
        str(CORPUS_PATH),
        str(MANIFEST_PATH),
        *CONTROL_PATHS,
        *SCORER_PATHS,
        str(BASELINE_PATH),
        str(RECORDING_MANIFEST_PATH),
    ):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ROOT / path, destination)
    artifact = tmp_path / BASELINE_PATH
    if change == "bytes":
        artifact.write_bytes(artifact.read_bytes() + b"\n")
    else:
        payload = json.loads(artifact.read_bytes())
        if change == "counts":
            payload["scores"][0]["counts"]["covered_probes"] = 12
        else:
            payload["rows"][0]["traces"][0]["query"]["text"] = "A changed question"
        artifact.write_text(json.dumps(payload))
        manifest_path = tmp_path / RECORDING_MANIFEST_PATH
        manifest = json.loads(manifest_path.read_bytes())
        manifest["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_recorded_control(tmp_path)
