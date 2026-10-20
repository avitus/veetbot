"""Frozen slot routing and refusal before paid adapter construction."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_core.evals import general_recall_checkpoint_runner as runner
from agent_core.evals.general_recall_checkpoint import CheckpointStore
from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent
from agent_core.evals.general_recall_checkpoint_runner import FrozenSlotEvaluator
from agent_core.evals.general_recall_continuation_budget import ContinuationBudget
from agent_core.evals.general_recall_continuation_collector import CollectionFailureError
from tests.unit.test_general_recall_checkpoint_parent import parent_fixture
from tests.unit.test_general_recall_checkpoint_runtime import preflight_fixture
from tests.unit.test_general_recall_confirmatory import scratch_settings
from tests.unit.test_general_recall_continuation_budget import parents
from tests.unit.test_people_execution import CompletingProvider

pytestmark = pytest.mark.static


def inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    archive, plan, _ = parent_fixture(tmp_path, monkeypatch)
    parent = CheckpointParent(archive, scratch_settings())
    arguments = preflight_fixture(tmp_path, monkeypatch)
    prepared = arguments["prepared"]
    prepared = replace(
        prepared,
        confirmation=parent.confirmation,
        preregistration=parent.preregistration,
        cases=parent.cases,
        snapshots=parent.inputs,
    )
    plan = replace(plan, prior_exposure_usd=Decimal("0.8"), allowance_usd=Decimal(10))
    store = CheckpointStore(tmp_path / "state", plan, create=True)
    parent.import_into(store)
    budget = ContinuationBudget(store, parents())
    sent: list[dict[str, Any]] = []
    constructions: list[str] = []

    async def evaluate_fixture(**kwargs: Any) -> dict[str, Any]:
        sent.append(kwargs)
        return {
            "scenario_id": kwargs["case"].id,
            "path": kwargs["path"],
            "pipeline": kwargs["pipeline"],
            "channel": kwargs["channel"],
            **parent.confirmation.observation_fields(),
        }

    def construct_provider(*args: Any) -> dict[str, Any]:
        constructions.append("provider")
        return {prepared.resolved.provider: CompletingProvider(Decimal("0.01"))}

    async def close() -> None:
        constructions.append("closed_encoder")

    monkeypatch.setattr(runner, "evaluate_fixture", evaluate_fixture, raising=False)
    monkeypatch.setattr(runner, "provider_adapters", construct_provider, raising=False)
    monkeypatch.setattr(
        runner, "local_embedding_provider", lambda *_: SimpleNamespace(close=close), raising=False
    )
    monkeypatch.setattr(runner, "verify_frozen_module", lambda *_: None, raising=False)
    monkeypatch.setattr(runner, "input_stamps", lambda *_: {"frozen": (1,)}, raising=False)
    monkeypatch.setattr(
        runner,
        "capture_inputs",
        lambda *_: SimpleNamespace(digest=plan.evaluated_inputs_sha256),
        raising=False,
    )
    monkeypatch.setattr(
        "agent_core.model.registry.ProviderRegistry.load", lambda *_args, **_kwargs: None
    )
    return (
        {
            "root": tmp_path / "frozen",
            "parent": parent,
            "prepared": prepared,
            "options": parent.options.model_copy(
                update={
                    "output": tmp_path / "segment-output",
                    "parallel_scratch_database": "veetbot_m33_eval_new_2",
                }
            ),
            "forbidden_databases": arguments["forbidden_databases"],
            "budget": budget,
            "deadline": SimpleNamespace(remaining=lambda: 100),
            "verify_protocol": lambda: None,
        },
        sent,
        constructions,
    )


async def test_bridge_uses_original_confirmation_and_selected_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments, sent, constructions = inputs(tmp_path, monkeypatch)
    evaluator = FrozenSlotEvaluator(**arguments)
    budget = arguments["budget"]
    try:
        key = budget.store.pending()[0]
        evaluator.admit(key)
        assert not constructions
        budget.store.begin(key)
        raw = json.loads(await evaluator.evaluate(key, 2))
        assert len(sent) == 1 and constructions == ["provider"]
        assert sent[0]["settings"] == arguments["prepared"].worker_settings[1]
        assert sent[0]["confirmation"] == arguments["parent"].confirmation
        assert sent[0]["reader"].slot == key
        assert sent[0]["production_reader"] is True
        assert sent[0]["disposable_database"] is True
        assert raw["identity_sha256"] == budget.store.plan.identity_sha256
        assert raw["observed_at"]
    finally:
        await evaluator.close()
        budget.seal()
        budget.store.close()


@pytest.mark.parametrize(
    "change",
    [
        "module",
        "capture",
        "stamp",
        "protocol",
        "deadline",
        "unstarted",
        "worker",
        "option",
        "duplicate_database",
    ],
)
async def test_bridge_refuses_before_provider_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    arguments, sent, constructions = inputs(tmp_path, monkeypatch)
    budget = arguments["budget"]

    def refuse(*args: Any) -> None:
        raise ValueError("Unadmitted protocol or runtime")

    evaluator = None
    try:
        if change == "option":
            arguments["options"] = arguments["options"].model_copy(update={"noise": 1})
        elif change == "duplicate_database":
            prepared = arguments["prepared"]
            arguments["prepared"] = replace(
                prepared, worker_settings=(prepared.settings, prepared.settings)
            )
        if change == "module":
            monkeypatch.setattr(runner, "verify_frozen_module", refuse)
        if change == "capture":
            monkeypatch.setattr(
                runner, "capture_inputs", lambda *_: SimpleNamespace(digest="0" * 64)
            )
        with pytest.raises((ValueError, TimeoutError)):
            evaluator = FrozenSlotEvaluator(**arguments)
            key = budget.store.pending()[0]
            if change == "stamp":
                monkeypatch.setattr(runner, "input_stamps", lambda *_: {"changed": (2,)})
            elif change == "protocol":
                arguments["verify_protocol"] = refuse
                evaluator.verify_protocol = refuse
            elif change == "deadline":
                arguments["deadline"].remaining = refuse
            if change != "unstarted":
                budget.store.begin(key)
            await evaluator.evaluate(key, 3 if change == "worker" else 1)
        assert not constructions and not sent
    finally:
        if evaluator is not None:
            await evaluator.close()
        budget.seal()
        budget.store.close()


@pytest.mark.parametrize("complete_bank", [False, True])
async def test_bridge_distinguishes_cancelled_preparation_from_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete_bank: bool
) -> None:
    arguments, _sent, _constructions = inputs(tmp_path, monkeypatch)
    budget = arguments["budget"]

    async def cancelled(**kwargs: Any) -> dict[str, Any]:
        if complete_bank:
            kwargs["formation_bank_path"].write_bytes(b'{"completed":"retained"}')
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "evaluate_fixture", cancelled)
    evaluator = FrozenSlotEvaluator(**arguments)
    try:
        key = budget.store.pending()[0]
        budget.store.begin(key)
        with pytest.raises(CollectionFailureError) as failure:
            await evaluator.evaluate(key, 1)
        assert failure.value.stage == ("answer" if complete_bank else "formation")
        assert isinstance(failure.value.cause, asyncio.CancelledError)
    finally:
        await evaluator.close()
        budget.seal()
        budget.store.close()
