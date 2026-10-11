"""ADR-0170 review is offline, blinded, complete and cannot rewrite old results."""

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from typer.testing import CliRunner

from agent_core.cli.main import app
from agent_core.evals import memory_reconsolidation_review as review
from agent_core.evals.memory_reconsolidation import (
    CONTROL_PATHS,
    CORPUS_PATH,
    SCORER_PATHS,
    Answer,
    load_corpus,
)
from agent_core.evals.memory_reconsolidation_comparison import ComparisonRow
from agent_core.evals.memory_reconsolidation_report import record
from tests.contract.reconsolidation_admission_cases import model
from tests.contract.reconsolidation_cases import Factory
from tests.unit.test_reconsolidation_comparison import ideal_rows

ReviewInputs = tuple[Path, Path, Path]

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_review_commands_are_available_without_starting_a_runtime() -> None:
    result = runner.invoke(app, ["eval", "memory-reconsolidation-review", "--help"])
    assert result.exit_code == 0, result.output
    assert "prepare" in result.output and "score" in result.output


def test_legacy_comparison_cannot_be_relabelled_as_review_measurement(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "eval",
            "memory-reconsolidation-review",
            "prepare",
            "--report",
            str(
                ROOT / "evals/observations/memory-reconsolidation/20261008-verified-comparison.json"
            ),
            "--directory",
            str(tmp_path / "packet"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "pre-run review contract" in result.output
    assert not (tmp_path / "packet").exists()


class ReviewIds:
    def new_id(self) -> UUID:
        return UUID("b38b3b11-c95a-43a5-9b7e-c5230d9913ec")


def measured_rows() -> tuple[ComparisonRow, ...]:
    cases = {c.id: c for c in load_corpus(ROOT).cases}
    return tuple(
        row.model_copy(
            update={
                "provider_calls": 3,
                "cost_usd": Decimal("0.001"),
                "trace_sha256": ("d" * 64,),
                "observation": row.observation.model_copy(
                    update={
                        "answers": row.observation.answers
                        or tuple(
                            Answer(probe_id=p.id, text="unknown")
                            for p in cases[row.observation.case_id].probes
                        )
                    }
                ),
            }
        )
        for row in ideal_rows()
    )


@pytest.fixture
def inputs(tmp_path: Path) -> ReviewInputs:
    root = tmp_path / "root"
    for name in (*review.REVIEW_PATHS, *CONTROL_PATHS, str(review.CONTRACT_PATH)):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    path = tmp_path / "report.json"
    value = record(
        ROOT,
        measured_rows(),
        model(),
        purpose="three_arm_comparison",
        unknown_usage_calls=0,
        original_control_sha256="a" * 64,
        review_contract_sha256=review.load_review_contract(ROOT),
    )
    path.write_text(value.model_dump_json(indent=2) + "\n")
    return root, path, tmp_path / "review"


def decide(directory: Path, verdict: str = "equivalent") -> dict[str, Any]:
    packet = review.ReviewPacket.model_validate_json((directory / "packet.json").read_bytes())
    payload: dict[str, Any] = json.loads((directory / "decisions.json").read_text())
    payload.update(reviewer="owner", human_reviewed=True)
    payload["decisions"] = [
        {
            "candidate_id": e.id,
            "verdict": verdict,
            "reference_id": e.references[0].id if verdict == "equivalent" else None,
        }
        for e in packet.entries
    ]
    (directory / "decisions.json").write_text(json.dumps(payload))
    return payload


def test_packet_is_blinded_and_template_requires_actual_human_decisions(
    inputs: ReviewInputs,
) -> None:
    root, path, directory = inputs
    packet = review.prepare_review(root, path, directory, ReviewIds())
    encoded = packet.model_dump_json()
    for hidden in (
        '"arm"',
        '"repeat"',
        '"case_id"',
        '"model"',
        '"provider"',
        "development-",
        "holdout-",
    ):
        assert hidden not in encoded
    assert packet.holdout_status == "previously_inspected"
    assert len(packet.entries) == 12
    assert all(e.sources and e.references and len(e.id) == 64 for e in packet.entries)
    assert all(len(s.id) == len(s.session) == 64 for e in packet.entries for s in e.sources)
    output = directory / "assessment.json"
    with pytest.raises(ValueError):
        review.score_review(root, path, directory, output)
    assert not output.exists()
    decide(directory)
    result = review.score_review(root, path, directory, output)
    assert result.failures == ()
    assert not result.activation_evidence
    assert result.counts["development:connections"]["hypothesis_matches"] == 6
    with pytest.raises(FileExistsError):
        review.score_review(root, path, directory, output)
    with pytest.raises(FileExistsError):
        review.prepare_review(root, path, directory, ReviewIds())


@pytest.mark.parametrize("verdict", ["uncertain", "not_equivalent"])
def test_negative_and_uncertain_exact_text_receive_no_credit(
    inputs: ReviewInputs, verdict: str
) -> None:
    root, path, directory = inputs
    review.prepare_review(root, path, directory, ReviewIds())
    decide(directory, verdict)
    result = review.score_review(root, path, directory, directory / "result.json")
    for split in ("development", "holdout"):
        assert result.counts[f"{split}:connections"]["hypothesis_matches"] == 0
        assert result.counts[f"{split}:connections"]["hypothesis_outputs"] == 6
        assert f"{split}:hypothesis_quality" in result.failures


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "duplicate",
        "unknown",
        "foreign_reference",
        "wrong_packet",
        "unknown_verdict",
        "no_human",
        "foreign_reviewer",
        "reference_on_uncertain",
        "unknown_field",
    ],
)
def test_incomplete_or_unbound_decisions_fail_closed(inputs: ReviewInputs, mode: str) -> None:
    root, path, directory = inputs
    review.prepare_review(root, path, directory, ReviewIds())
    payload = decide(directory)
    if mode == "missing":
        payload["decisions"].pop()
    elif mode == "duplicate":
        payload["decisions"].append(payload["decisions"][0])
    elif mode == "unknown":
        payload["decisions"][0]["candidate_id"] = "0" * 64
    elif mode == "foreign_reference":
        payload["decisions"][0]["reference_id"] = payload["decisions"][1]["reference_id"]
    elif mode == "wrong_packet":
        payload["packet_sha256"] = "0" * 64
    elif mode == "unknown_verdict":
        payload["decisions"][0]["verdict"] = "looks_good"
    elif mode == "no_human":
        payload["human_reviewed"] = False
    elif mode == "foreign_reviewer":
        payload["reviewer"] = "automated_judge"
    elif mode == "reference_on_uncertain":
        payload["decisions"][0]["verdict"] = "uncertain"
    else:
        payload["decisions"][0]["new_reference"] = "invented meaning"
    (directory / "decisions.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        review.score_review(root, path, directory, directory / "result.json")
    assert not (directory / "result.json").exists()


@pytest.mark.parametrize(
    "target",
    [
        "statement",
        "support",
        "source",
        "reference",
        "key",
        "observation",
        "scorer",
        "corpus",
        "contract",
    ],
)
def test_altered_review_inputs_are_rejected(inputs: ReviewInputs, target: str) -> None:
    root, path, directory = inputs
    review.prepare_review(root, path, directory, ReviewIds())
    decide(directory)
    packet_path = directory / "packet.json"
    packet = json.loads(packet_path.read_text())
    if target == "statement":
        packet["entries"][0]["candidate"]["statement"] += " never"
    elif target == "support":
        packet["entries"][0]["candidate"]["support"][0] = "0" * 64
    elif target == "source":
        packet["entries"][0]["sources"][0]["attribution"] = "someone else"
    elif target == "reference":
        packet["entries"][0]["references"][0]["meaning"]["statement"] += " 99"
    elif target == "key":
        key = json.loads((directory / "private-key.json").read_text())
        key["nonce"] = str(UUID(int=123))
        (directory / "private-key.json").write_text(json.dumps(key))
    else:
        selected = (
            path
            if target == "observation"
            else root
            / (
                review.CONTRACT_PATH
                if target == "contract"
                else SCORER_PATHS[0]
                if target == "scorer"
                else CORPUS_PATH
            )
        )
        selected.write_bytes(selected.read_bytes() + b" ")
    if target in {"statement", "support", "source", "reference"}:
        packet_path.write_text(json.dumps(packet))
    with pytest.raises(ValueError):
        review.score_review(root, path, directory, directory / "result.json")


def test_equivalent_duplicate_outputs_are_still_false_positives(inputs: ReviewInputs) -> None:
    root, path, directory = inputs
    rows = tuple(
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={"hypotheses": row.observation.hypotheses * 2}
                )
            }
        )
        for row in measured_rows()
    )
    value = record(
        ROOT,
        rows,
        model(),
        purpose="three_arm_comparison",
        unknown_usage_calls=0,
        original_control_sha256="a" * 64,
        review_contract_sha256=review.load_review_contract(ROOT),
    )
    path.write_text(value.model_dump_json())
    review.prepare_review(root, path, directory, ReviewIds())
    decide(directory)
    result = review.score_review(root, path, directory, directory / "result.json")
    assert result.counts["development:connections"]["hypothesis_matches"] == 6
    assert result.counts["development:connections"]["hypothesis_outputs"] == 12
    assert "development:hypothesis_quality" in result.failures


@pytest.mark.parametrize(
    "mode", ["answers", "false_merge", "lost_fact", "failed_case", "boundary", "unknown_usage"]
)
def test_human_equivalence_never_clears_other_quality_failures(
    inputs: ReviewInputs, mode: str
) -> None:
    root, path, directory = inputs
    rows = measured_rows()
    if mode == "answers":
        rows = tuple(
            row.model_copy(
                update={
                    "observation": row.observation.model_copy(
                        update={
                            "answers": tuple(
                                Answer(probe_id=a.probe_id, text="unknown")
                                for a in row.observation.answers
                            )
                        }
                    )
                }
            )
            for row in rows
        )
    elif mode in {"false_merge", "lost_fact"}:
        index = next(
            i
            for i, r in enumerate(rows)
            if r.arm == "connections" and r.observation.case_id == "development-connection_focus"
        )
        changed = rows[index].observation.model_copy(
            update={"merges": (("m1", "m2"),)} if mode == "false_merge" else {"surviving_ids": ()}
        )
        rows = (
            *rows[:index],
            rows[index].model_copy(update={"observation": changed}),
            *rows[index + 1 :],
        )
    elif mode in {"failed_case", "boundary"}:
        rows = (
            rows[0].model_copy(
                update={"failure": "runtime_failure"}
                if mode == "failed_case"
                else {"boundary_failures": 1}
            ),
            *rows[1:],
        )
    value = record(
        ROOT,
        rows,
        model(),
        purpose="three_arm_comparison",
        unknown_usage_calls=int(mode == "unknown_usage"),
        original_control_sha256="a" * 64,
        review_contract_sha256=review.load_review_contract(ROOT),
    )
    path.write_text(value.model_dump_json())
    review.prepare_review(root, path, directory, ReviewIds())
    decide(directory)
    result = review.score_review(root, path, directory, directory / "result.json")
    assert result.failures
    assert not any("hypothesis_quality" in failure for failure in result.failures)
    assert not result.activation_evidence


@pytest.mark.parametrize("change_contract", [False, True])
async def test_comparison_pins_review_before_calls_and_refuses_midrun_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_contract: bool
) -> None:
    from agent_core.domain.messages import ResolvedModel
    from agent_core.evals import memory_reconsolidation_report as reporting
    from agent_core.evals.memory_reconsolidation_comparison import Arm
    from agent_core.evals.memory_reconsolidation_control import RuntimeCase
    from agent_core.evals.memory_reconsolidation_runtime import MeasuredProvider
    from tests.contract.reconsolidation_execution_cases import ExecutionProvider, TrackedFactory
    from tests.contract.support import memory_uow_factory

    _, factory = await memory_uow_factory()
    contract = tmp_path / "contract.json"
    contract.write_bytes((ROOT / review.CONTRACT_PATH).read_bytes())
    digest = review.load_review_contract(ROOT, contract)
    rows = {(r.arm, r.repeat, r.observation.case_id): r for r in measured_rows()}
    calls = 0

    async def collect(
        case: RuntimeCase,
        arm: Arm,
        repeat: int,
        *,
        model: ResolvedModel,
        provider: MeasuredProvider,
    ) -> ComparisonRow:
        nonlocal calls
        if calls == 0:
            assert review.load_review_contract(ROOT, contract) == digest
            if change_contract:
                contract.write_bytes(contract.read_bytes() + b" ")
        calls += 1
        return rows[(arm, repeat, case.id)]

    monkeypatch.setattr(reporting, "collect_comparison_case", collect)
    control, report = tmp_path / "control.json", tmp_path / "new-report.json"
    if change_contract:
        with pytest.raises(ValueError, match="review contract changed"):
            await reporting.run_comparison(
                ROOT,
                control,
                report,
                model(),
                ExecutionProvider(TrackedFactory(cast(Factory, factory))),
                review_contract=contract,
            )
        assert not report.exists() and not control.exists()
    else:
        value = await reporting.run_comparison(
            ROOT,
            control,
            report,
            model(),
            ExecutionProvider(TrackedFactory(cast(Factory, factory))),
            review_contract=contract,
        )
        assert calls == 216
        assert value.review_contract_sha256 == digest
        assert json.loads(control.read_text())["review_contract_sha256"] == digest


def test_cli_review_happy_path_validation_failure_and_retry(inputs: ReviewInputs) -> None:
    _, path, directory = inputs
    command = ["eval", "memory-reconsolidation-review"]
    args = [*command, "prepare", "--report", str(path), "--directory", str(directory)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert runner.invoke(app, args).exit_code == 1
    output = directory / "assessment.json"
    score = [
        *command,
        "score",
        "--report",
        str(path),
        "--directory",
        str(directory),
        "--output",
        str(output),
    ]
    assert runner.invoke(app, score).exit_code == 1
    assert not output.exists()
    decide(directory)
    assert runner.invoke(app, score).exit_code == 0
    before = output.read_bytes()
    assert runner.invoke(app, score).exit_code == 1
    assert output.read_bytes() == before
    assert runner.invoke(app, [*command, "score"]).exit_code == 2


def test_unknown_support_cannot_be_omitted_from_human_review(inputs: ReviewInputs) -> None:
    root, path, directory = inputs
    rows = list(measured_rows())
    index = next(i for i, row in enumerate(rows) if row.observation.hypotheses)
    row = rows[index]
    candidate = row.observation.hypotheses[0].model_copy(update={"support": ("m1", "missing")})
    rows[index] = row.model_copy(
        update={"observation": row.observation.model_copy(update={"hypotheses": (candidate,)})}
    )
    value = record(
        ROOT,
        tuple(rows),
        model(),
        purpose="three_arm_comparison",
        unknown_usage_calls=0,
        original_control_sha256="a" * 64,
        review_contract_sha256=review.load_review_contract(ROOT),
    )
    path.write_text(value.model_dump_json())
    with pytest.raises(ValueError, match="unknown original support"):
        review.prepare_review(root, path, directory, ReviewIds())
    assert not directory.exists()


def test_human_equivalence_can_credit_different_wording(inputs: ReviewInputs) -> None:
    root, path, directory = inputs
    rows = tuple(
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "hypotheses": tuple(
                            h.model_copy(update={"statement": "Tentatively: " + h.statement})
                            for h in row.observation.hypotheses
                        )
                    }
                )
            }
        )
        for row in measured_rows()
    )
    value = record(
        ROOT,
        rows,
        model(),
        purpose="three_arm_comparison",
        unknown_usage_calls=0,
        original_control_sha256="a" * 64,
        review_contract_sha256=review.load_review_contract(ROOT),
    )
    assert "development:hypothesis_quality" in value.failures
    path.write_text(value.model_dump_json())
    original = path.read_bytes()
    review.prepare_review(root, path, directory, ReviewIds())
    decide(directory)
    result = review.score_review(root, path, directory, directory / "assessment.json")
    assert result.failures == ()
    assert result.counts["development:connections"]["hypothesis_matches"] == 6
    assert path.read_bytes() == original


def test_unknown_observation_case_is_a_validation_error(inputs: ReviewInputs) -> None:
    root, path, directory = inputs
    value = json.loads(path.read_text())
    value["rows"][0]["observation"]["case_id"] = "unknown-case"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="census"):
        review.prepare_review(root, path, directory, ReviewIds())
    assert not directory.exists()


def test_merge_answer_regression_survives_equivalence_review(inputs: ReviewInputs) -> None:
    root, path, directory = inputs
    cases = {c.id: c for c in load_corpus(ROOT).cases}
    rows = tuple(
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "answers": tuple(
                            Answer(probe_id=p.id, text=p.answer.values[0])
                            for p in cases[row.observation.case_id].probes
                        )
                    }
                )
            }
        )
        if row.arm == "original_only"
        else row
        for row in measured_rows()
    )
    value = record(
        ROOT,
        rows,
        model(),
        purpose="three_arm_comparison",
        unknown_usage_calls=0,
        original_control_sha256="a" * 64,
        review_contract_sha256=review.load_review_contract(ROOT),
    )
    path.write_text(value.model_dump_json())
    review.prepare_review(root, path, directory, ReviewIds())
    decide(directory)
    result = review.score_review(root, path, directory, directory / "assessment.json")
    assert "development:merge_answer_regression" in result.failures
    assert "holdout:merge_answer_regression" in result.failures
