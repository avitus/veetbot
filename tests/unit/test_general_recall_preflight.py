"""Capture uses a fixed probe and stops before history or provider work."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_core.domain.messages import ResolvedModel
from agent_core.evals import general_recall_confirmation as confirmation
from agent_core.evals import general_recall_execution as execution
from agent_core.evals.general_recall_experiment import Experiment
from tests.contract.support import NOW
from tests.contract.test_local_semantic_preparation import LOCAL
from tests.integration.m2_support import memory_settings
from tests.unit.test_general_recall_confirmation import capture


async def test_preflight_captures_all_variants_without_opening_corpus_or_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []
    principal = capture().principal.principal()

    async def probe(**kwargs: Any) -> dict[str, Any]:
        case = kwargs["case"]
        seen.append((case.id, kwargs["pipeline"], kwargs["path"], kwargs["channel"]))
        assert case.id == "configuration-preflight" and case.split == "development"
        assert not case.evidence
        assert kwargs["reader"].budget.calls == 0
        value = capture().variants[0].configuration
        value.update({key: kwargs[key] for key in ("pipeline", "path", "channel")})
        kwargs["validate_composition"](value)
        pytest.fail("The capture callback must stop execution")

    monkeypatch.setattr(execution, "evaluate_fixture", probe)
    options = Experiment(
        corpus=tmp_path / "unopened.jsonl",
        output=tmp_path / "unused",
        storage="postgres",
        production_reader=True,
    )
    captured = await confirmation.capture_runtime_configurations(
        options,
        settings=memory_settings(),
        principal=principal,
        resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
        local=LOCAL,
        maximum_cost_usd=Decimal("10"),
    )
    assert len(captured.variants) == len(seen) == 12
    assert len(set(seen)) == 12
    assert captured.maximum_cost_usd == 10
    assert not options.corpus.exists() and not options.output.exists()
