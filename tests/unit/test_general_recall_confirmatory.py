"""Confirmation refusal precedes provider construction and held-out parsing."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_core.evals import general_recall_confirmatory as runner
from tests.integration.m2_support import memory_settings


def options(tmp_path: Path, **updates: Any) -> runner.ConfirmationOptions:
    return runner.ConfirmationOptions(
        **{
            "corpus": tmp_path / "unopened-holdout.jsonl",
            "output": tmp_path / "result",
            "independence_review": tmp_path / "missing-review.json",
            "baseline_report": tmp_path / "baseline.json",
            "build_ref": "a" * 40,
            "p95_latency_ms": 6000,
            "maximum_case_usd": 1,
            "storage": "postgres",
            "disposable_database": True,
            **updates,
        }
    )


def test_explicit_owner_approved_cap_does_not_raise_the_existing_default(tmp_path: Path) -> None:
    assert options(tmp_path).maximum_cost_usd == 100
    assert options(tmp_path, maximum_cost_usd=290).maximum_cost_usd == 290
    assert options(tmp_path, maximum_cost_usd=300).maximum_cost_usd == 300
    with pytest.raises(ValidationError):
        options(tmp_path, maximum_cost_usd=301)


@pytest.mark.parametrize("changed", [False, True])
async def test_preflight_captures_actual_dependencies_before_parsing_holdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    import hashlib
    from types import SimpleNamespace

    from agent_core.domain.messages import ResolvedModel
    from agent_core.domain.semantic_reader import reader_identity
    from agent_core.evals import general_recall_capture, general_recall_corpus, memory_distillation
    from agent_core.evals.general_recall_collection import scenario_manifest
    from agent_core.evals.general_recall_publication import IndependenceReview
    from agent_core.memory import (
        general_recall_evidence,
        recall_release_binding,
        reconsolidation_evidence,
    )
    from agent_core.memory.semantic_deployment import IndexConfiguration, configuration_digest
    from agent_core.model.registry import StaticModelRouter
    from tests.contract.support import NOW
    from tests.contract.test_local_semantic_preparation import CONFIG, LOCAL
    from tests.unit.test_general_recall_confirmation import capture
    from tests.unit.test_general_recall_corpus import fixture
    from tests.unit.test_general_recall_release import fixture as release_fixture

    selected = options(tmp_path)
    base = fixture()
    cases = tuple(
        base.model_copy(
            update={
                **scenario.model_dump(
                    include={"id", "family", "split", "scenario_lineage", "query_lineage"}
                ),
                "history": (base.history[0].model_copy(update={"content": scenario.id}),),
            }
        )
        for scenario in release_fixture().scenarios
    )
    scenarios = scenario_manifest(cases)
    review = IndependenceReview(
        version="recall-independence-review@1",
        reviewer="Fabricated unit reviewer",
        reviewed_at=NOW,
        decision="accepted",
        corpus_sha256=general_recall_corpus.corpus_digest(cases),
        scenario_manifest_sha256=hashlib.sha256(
            b"".join(s.model_dump_json().encode() + b"\n" for s in scenarios)
        ).hexdigest(),
        reviewed_scenarios=tuple(case.id for case in cases),
    )
    selected.independence_review.write_text(review.model_dump_json())
    selected.baseline_report.write_text('{"kind":"fabricated unit baseline"}')
    stages: list[str] = []
    schema = {"version": "fabricated-schema"}
    resolved = ResolvedModel(
        provider="fake",
        model="scripted",
        credential_ref="fake",
        policy_name="fake-balanced",
        resolved_at=NOW,
    )
    profile = IndexConfiguration(
        embedding=CONFIG,
        local=LOCAL,
        model_directory=tmp_path,
        reader_sha256=reader_identity(
            resolved, reasoning_effort=None, reasoning_summary=True, registry_version=None
        ),
    )
    captured = capture()
    target: dict[str, Any] = {
        "configuration": {
            "principal": captured.principal.model_dump(mode="json"),
            "schema_sha256": configuration_digest(schema),
            "memory": {"retrieval": {"source_recovery_enabled": False}},
            "context": {"classes": {"in_turn_recall": {"max_tokens": 2000}}},
        },
        "formation": {},
        "reader": {
            "catalog": {
                "chat_default": {"model_policy": "fake-balanced"},
                "chat_options": [
                    {
                        "model_policy": "fake-balanced",
                        "display_name": "Fake",
                        "provider": "fake",
                        "model": "scripted",
                    }
                ],
                "memory_default": {"model_policy": "fake-balanced"},
                "memory_options": [
                    {
                        "model_policy": "fake-balanced",
                        "display_name": "Fake",
                        "provider": "fake",
                        "model": "scripted",
                    }
                ],
            }
        },
    }
    for variant in captured.variants:
        variant.configuration.update(
            composition=target,
            retrieval=target["configuration"]["memory"]["retrieval"],
            embedding=CONFIG.model_dump(mode="json"),
            source_recovery=False,
            production_reader=True,
            reader_runtime={
                "reader_sha256": profile.reader_sha256,
                "reasoning_effort": None,
                "reasoning_summary": True,
                "registry_version": None,
            },
        )

    async def deployment(*_: Any, **__: Any) -> dict[str, Any]:
        stages.append("deployment")
        return {
            "indexing": profile.model_dump(mode="json"),
            "composition": target,
            "dependencies": dict.fromkeys(
                (
                    "configuration_sha256",
                    "embedding_configuration_sha256",
                    "formation_configuration_sha256",
                    "reader_configuration_sha256",
                ),
                "a" * 64,
            ),
        }

    async def schema_capture(*_: Any) -> dict[str, Any]:
        stages.append("schema")
        return schema

    async def runtime(*_: Any, **__: Any) -> Any:
        stages.append("runtime")
        return captured

    async def resolve(*_: Any, **__: Any) -> ResolvedModel:
        return resolved

    def corpus(*_: Any, **__: Any) -> Any:
        assert stages == ["committed", "deployment", "schema", "controls", "runtime"]
        stages.append("holdout")
        return cases[:-1] if changed else cases

    monkeypatch.setattr(
        memory_distillation, "require_committed_tree", lambda *_: stages.append("committed")
    )
    monkeypatch.setattr(general_recall_capture, "capture_deployment", deployment)
    monkeypatch.setattr(general_recall_capture, "capture_schema", schema_capture)
    monkeypatch.setattr(runner, "capture_runtime_configurations", runtime)

    def controls(_: Path) -> dict[str, str]:
        stages.append("controls")
        return {"fixed": "a" * 64}

    monkeypatch.setattr(runner, "capture_frozen_controls", controls)
    monkeypatch.setattr(StaticModelRouter, "resolve", resolve)
    monkeypatch.setattr(general_recall_corpus, "load_corpus", corpus)
    monkeypatch.setattr(reconsolidation_evidence, "implementation_digest", lambda _: "b" * 64)
    monkeypatch.setattr(general_recall_evidence, "scorer_digest", lambda _: "c" * 64)
    evaluated = SimpleNamespace(digest="e" * 64, model_dump=lambda **_: {})
    monkeypatch.setattr(recall_release_binding, "capture_inputs", lambda *_: evaluated)
    if changed:
        with pytest.raises(ValueError, match="Corpus differs"):
            await runner.prepare_confirmation(
                tmp_path, selected, memory_settings(), memory_settings()
            )
    else:
        prepared = await runner.prepare_confirmation(
            tmp_path, selected, memory_settings(), memory_settings()
        )
        assert len(prepared.cases) == 400
        assert prepared.confirmation.identity.execution_manifest_sha256 == captured.digest
        assert prepared.preregistration.registered_at >= captured.captured_at
        assert prepared.confirmation.manifest.maximum_cost_usd == 100
        assert stages[-1] == "holdout"
    assert not selected.output.exists()


@pytest.mark.parametrize("failure", ["disabled", "existing_output", "unreviewed"])
async def test_confirmation_refuses_before_holdout_or_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from agent_core.evals import general_recall_composition, general_recall_corpus

    selected = options(tmp_path)
    if failure == "existing_output":
        selected.output.mkdir()

    def forbidden(*_: Any, **__: Any) -> Any:
        pytest.fail("Refused confirmation must not parse holdout or construct providers")

    monkeypatch.setattr(general_recall_composition, "provider_adapters", forbidden)
    monkeypatch.setattr(general_recall_corpus, "load_corpus", forbidden)
    with pytest.raises(ValueError, match=r"LIVE|output|review"):
        await runner.run_confirmation(
            tmp_path,
            selected,
            memory_settings(),
            memory_settings(),
            live_enabled=failure != "disabled",
        )


@pytest.mark.parametrize("stop", ["none", "budget", "controls", "registry"])
async def test_one_shared_budget_and_immutable_rows_survive_completion_or_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    import json
    from types import SimpleNamespace

    from agent_core.domain.errors import BudgetExceededError
    from agent_core.domain.messages import ResolvedModel
    from agent_core.evals import (
        general_recall_capture,
        general_recall_composition,
        general_recall_execution,
    )
    from agent_core.memory import recall_release_binding
    from agent_core.model.registry import ProviderRegistry
    from tests.contract.support import NOW
    from tests.contract.test_local_semantic_preparation import LOCAL
    from tests.unit.test_general_recall_confirmation import binding, capture
    from tests.unit.test_general_recall_corpus import fixture
    from tests.unit.test_general_recall_release import fixture as release_fixture

    selected = options(tmp_path)
    confirmed = binding(capture())
    prepared = runner.PreparedConfirmation(
        confirmed,
        release_fixture().preregistration,
        (fixture(),),
        LOCAL,
        tmp_path,
        ResolvedModel(
            provider="fake",
            model="scripted",
            credential_ref="fake",
            policy_name="fake-balanced",
            resolved_at=NOW,
        ),
        memory_settings(),
        {"deployment": {}, "schema": {}, "frozen_controls": {}},
    )

    async def prepare(*_: Any) -> runner.PreparedConfirmation:
        return prepared

    monkeypatch.setattr(runner, "prepare_confirmation", prepare)
    monkeypatch.setattr(recall_release_binding, "input_stamps", lambda _: {})
    monkeypatch.setattr(
        recall_release_binding,
        "capture_inputs",
        lambda *_: SimpleNamespace(digest=confirmed.identity.evaluated_inputs_sha256),
    )
    closed: list[str] = []

    class Resource:
        name = "fake"

        async def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(
        general_recall_composition, "provider_adapters", lambda *_: {"fake": Resource()}
    )
    monkeypatch.setattr(
        general_recall_composition, "local_embedding_provider", lambda *_: Resource()
    )
    budgets: list[Any] = []

    async def deployment_capture(*_: Any, **__: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(general_recall_capture, "capture_deployment", deployment_capture)
    monkeypatch.setattr(general_recall_capture, "capture_schema", deployment_capture)
    monkeypatch.setattr(
        runner,
        "capture_frozen_controls",
        lambda _: {"changed": "a" * 64} if stop == "controls" and budgets else {},
    )

    async def evaluate(**kwargs: Any) -> dict[str, Any]:
        budget = kwargs["reader"].budget
        budgets.append(budget)
        assert kwargs["confirmation"] == confirmed
        if stop == "budget" and len(budgets) == 4:
            budget.held = Decimal("0.25")
            budget.failed = True
            raise BudgetExceededError("cost", "Synthetic budget interruption")
        # No fabricated usage is presented to the real journal verifier. These
        # rows isolate orchestration and cannot pass publication or collection.
        return {
            "scenario_id": kwargs["case"].id,
            "pipeline": kwargs["pipeline"],
            "path": kwargs["path"],
            "channel": kwargs["channel"],
            **confirmed.observation_fields(),
        }

    monkeypatch.setattr(general_recall_execution, "evaluate_fixture", evaluate)
    if stop == "registry":

        def unavailable_registry(*_: Any, **__: Any) -> None:
            raise ValueError("Synthetic registry setup failure")

        monkeypatch.setattr(ProviderRegistry, "load", unavailable_registry)
    if stop != "none":
        with pytest.raises(BudgetExceededError if stop == "budget" else ValueError):
            await runner.run_confirmation(
                tmp_path, selected, memory_settings(), memory_settings(), live_enabled=True
            )
    else:
        await runner.run_confirmation(
            tmp_path, selected, memory_settings(), memory_settings(), live_enabled=True
        )
    result = json.loads((selected.output / "run.json").read_text())
    assert result["state"] == ("failed" if stop != "none" else "completed")
    if stop == "registry":
        assert result["error_class"] == "ValueError" and result["finished_at"]
        assert result["completed"] == result["provider_calls"] == 0
        assert result["actual_cost_usd"] == result["held_cost_usd"] == "0"
        assert not budgets and not closed
        assert not (selected.output / "collection.json").exists()
        return
    assert result["completed"] == (3 if stop == "budget" else 12)
    assert result["activation_evidence"] is False
    assert result["held_cost_usd"] == ("0.25" if stop == "budget" else "0")
    assert len({id(budget) for budget in budgets}) == 1
    assert budgets[0].maximum == Decimal("100")
    assert len(closed) == 2
    rows = [
        json.loads(row)
        for path in (selected.output / "observations").glob("*.jsonl")
        for row in path.read_text().splitlines()
    ]
    assert len(rows) == (3 if stop == "budget" else 12)
    assert all(
        row["identity_sha256"] == confirmed.observation_fields()["identity_sha256"] for row in rows
    )
    assert (selected.output / "collection.json").exists() == (stop == "none")
    with pytest.raises(ValueError, match="output already exists"):
        await runner.run_confirmation(
            tmp_path, selected, memory_settings(), memory_settings(), live_enabled=True
        )
