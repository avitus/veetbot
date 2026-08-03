"""Milestone 3 failed-attempt pricing and metadata-closure gates."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.messages import (
    CostSource,
    FakeModelScript,
    ModelPricing,
    ModelTransientError,
    ModelTurn,
    ModelUsage,
    ProviderMetadata,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import FailureReason, RunLimits
from agent_core.model.cost import price_usage
from tests.contract.support import NOW

ROOT = Path(__file__).resolve().parents[2]


SETTINGS = Settings(
    database_url="postgresql+asyncpg://localhost/unused",
    deployment_mode=DeploymentMode.DEVELOPMENT,
    auth_mode=AuthMode.DEV,
    auth_token=None,
    sandbox=SandboxMechanism.FAKE,
    config_dir=None,
    credentials=MappingProxyType({}),
    interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
)


def transient() -> ModelTransientError:
    return ModelTransientError(
        provider="fake",
        model="scripted",
        attempt_id=UUID(int=0),
        message="scripted transient failure",
        stream_had_output=False,
    )


async def test_failed_attempt_costs_accumulate_until_budget_exceeded() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                fail_with=transient(),
                usage=ModelUsage(cost=Decimal("0.03")),
            ),
            ScriptedTurn(
                fail_with=transient(),
                usage=ModelUsage(cost=Decimal("0.03")),
            ),
        ]
    )
    async with build(
        settings=SETTINGS,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("0.05"),
        ),
    ) as composition:
        run_id = await composition.runs.submit("retry until the cost ceiling")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            rollup = await uow.usage.run_usage(run_id)
    assert run.failure is not None
    assert run.failure.reason is FailureReason.BUDGET_EXCEEDED
    assert run.model_call_count == 2
    assert run.usage.cost == Decimal("0.06")
    assert rollup.cost == Decimal("0.06")


def test_cost_calculation_uses_all_five_token_classes_exactly() -> None:
    usage = ModelUsage(
        input_tokens=1_000_000,
        cached_input_tokens=100_000,
        cache_write_input_tokens=200_000,
        output_tokens=500_000,
        reasoning_tokens=100_000,
    )
    pricing = ModelPricing(
        input_per_mtok=Decimal("5"),
        cached_input_per_mtok=Decimal("0.5"),
        cache_write_per_mtok=Decimal("6.25"),
        output_per_mtok=Decimal("25"),
        reasoning_per_mtok=Decimal("30"),
        reasoning_priced_separately=True,
        source=CostSource.DOCS_SNAPSHOT,
    )
    priced = price_usage(usage, pricing)
    assert priced.cost == Decimal("17.8")
    assert priced.cost_source is CostSource.DOCS_SNAPSHOT


def test_cost_calculation_preserves_explicit_zero_rates() -> None:
    usage = ModelUsage(
        input_tokens=10,
        cache_write_input_tokens=10,
        output_tokens=10,
        reasoning_tokens=10,
    )
    pricing = ModelPricing(
        input_per_mtok=Decimal("7"),
        cache_write_per_mtok=Decimal("0"),
        output_per_mtok=Decimal("11"),
        reasoning_per_mtok=Decimal("0"),
        reasoning_priced_separately=True,
    )
    assert price_usage(usage, pricing).cost == Decimal("0")


@pytest.mark.parametrize(
    "usage",
    [
        ModelUsage(input_tokens=2, cached_input_tokens=2, cache_write_input_tokens=1),
        ModelUsage(output_tokens=2, reasoning_tokens=3),
    ],
)
def test_cost_calculation_rejects_inconsistent_provider_usage(usage: ModelUsage) -> None:
    with pytest.raises(ValueError, match="exceed"):
        price_usage(
            usage,
            ModelPricing(reasoning_priced_separately=True),
        )


def test_provider_metadata_is_closed_and_has_only_two_readers() -> None:
    assert set(ProviderMetadata.model_fields) == {
        "provider_api",
        "response_id",
        "request_id",
        "resolved_model",
        "previous_response_id",
        "cache_breakpoints_sent",
        "cache_breakpoints_dropped",
    }
    with pytest.raises(ValidationError):
        ModelTurn.model_validate(
            {
                "stop_reason": StopReason.END_TURN,
                "provider_metadata": {
                    "provider_api": "responses",
                    "undeclared": "value",
                },
            }
        )

    readers: list[tuple[str, str]] = []
    for relative in (
        "src/agent_core/adapters/persistence/mappers.py",
        "src/agent_core/observability/models.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if any(
                isinstance(nested, ast.Call)
                and isinstance(nested.func, ast.Attribute)
                and nested.func.attr == "model_dump"
                and isinstance(nested.func.value, ast.Name)
                and nested.func.value.id == "metadata"
                for nested in ast.walk(node)
            ):
                readers.append((relative, node.name))
    assert readers == [
        (
            "src/agent_core/adapters/persistence/mappers.py",
            "flatten_provider_metadata",
        ),
        ("src/agent_core/observability/models.py", "span_provider_attributes"),
    ]
