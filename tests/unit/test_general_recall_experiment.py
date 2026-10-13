import json
from decimal import Decimal
from pathlib import Path

import pytest

from agent_core.evals.general_recall_experiment import Experiment, validate_options


@pytest.mark.parametrize(
    "failure", ["disabled", "duplicate_path", "missing_embedding", "overwrite"]
)
def test_experiment_refuses_unsafe_or_ambiguous_launch_before_provider_access(
    tmp_path: Path,
    failure: str,
) -> None:
    options = Experiment(corpus=tmp_path / "corpus", output=tmp_path / "output")
    if failure == "duplicate_path":
        options = options.model_copy(update={"paths": ("automatic", "automatic")})
    elif failure == "missing_embedding":
        options = options.model_copy(update={"channel": "hybrid"})
    elif failure == "overwrite":
        options.output.mkdir()
    with pytest.raises(ValueError):
        validate_options(options, live_enabled=failure != "disabled")


def test_bounded_new_sparse_experiment_can_start(tmp_path: Path) -> None:
    validate_options(
        Experiment(corpus=tmp_path / "corpus", output=tmp_path / "output"), live_enabled=True
    )


async def test_provider_setup_failure_is_retained_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_core import config
    from agent_core.evals import general_recall_composition, general_recall_experiment
    from tests.integration.m2_support import memory_settings
    from tests.unit.test_general_recall_corpus import fixture

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(fixture().model_dump_json() + "\n")
    selected = Experiment(corpus=corpus, output=tmp_path / "run")
    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    monkeypatch.setattr(config, "load_settings", lambda *_: memory_settings())

    def setup_failure(*_: object) -> None:
        raise ValueError("Synthetic provider setup failure")

    monkeypatch.setattr(general_recall_composition, "provider_adapters", setup_failure)
    with pytest.raises(ValueError, match="Synthetic provider setup failure"):
        await general_recall_experiment.run_experiment(selected)
    record = json.loads((selected.output / "run.json").read_bytes())
    assert record["state"] == "failed"
    assert record["error_class"] == "ValueError" and record["finished_at"]
    assert record["completed"] == record["provider_calls"] == 0
    assert record["actual_cost_usd"] == record["held_cost_usd"] == "0"


async def test_paired_matrix_uses_one_nonrenewable_provider_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_core.evals import general_recall_experiment as experiment
    from agent_core.evals.people_execution import EvaluationBudget

    ledgers: list[EvaluationBudget] = []

    async def observe(
        option: Experiment, *, shared_budget: EvaluationBudget | None = None
    ) -> dict[str, object]:
        assert shared_budget is not None
        ledgers.append(shared_budget)
        if len(ledgers) == 2:
            assert ledgers[-1].spent == Decimal("0.25")
        ledgers[-1].spent += Decimal("0.25")
        return {}

    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    monkeypatch.setattr(experiment, "run_experiment", observe)
    await experiment.run_matrix(
        tuple(
            Experiment(
                corpus=tmp_path / "corpus",
                output=tmp_path / f"arm-{i}",
                maximum_cost_usd=Decimal("5"),
            )
            for i in range(2)
        ),
        output=tmp_path / "matrix",
        maximum_cost_usd=Decimal("5"),
    )
    assert ledgers[0] is not None, "A paired matrix must reserve against its shared ledger"
    assert ledgers[0] is ledgers[1]


@pytest.mark.parametrize("change", ["corpus", "budget", "duplicate_output"])
async def test_matrix_refuses_mixed_inputs_or_renewed_allowances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from agent_core.evals.general_recall_experiment import run_matrix

    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    first = Experiment(corpus=tmp_path / "corpus", output=tmp_path / "first")
    updates: dict[str, object] = {"output": tmp_path / "second"}
    if change == "corpus":
        updates["corpus"] = tmp_path / "different"
    elif change == "budget":
        updates["maximum_cost_usd"] = Decimal("3")
    else:
        updates["output"] = first.output
    with pytest.raises(ValueError):
        await run_matrix(
            (first, first.model_copy(update=updates)),
            output=tmp_path / "matrix",
            maximum_cost_usd=Decimal("2"),
        )
    assert not (tmp_path / "matrix").exists()
