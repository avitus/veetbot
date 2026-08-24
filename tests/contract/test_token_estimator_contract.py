from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.messages import TextPart, UserMessage
from agent_core.tools.calculator import CalculatorTool


def test_token_estimator_is_stable_and_reconciles_conservatively() -> None:
    estimator = ConservativeTokenEstimator()
    items = [UserMessage(content=[TextPart(text="stable payload")])]

    first = estimator.estimate(items, "fake:scripted")
    second = estimator.estimate(items, "fake:scripted")
    estimator.reconcile("fake:scripted", first, first * 2)
    reconciled = estimator.estimate(items, "fake:scripted")
    adjusted = estimator.estimate(
        [UserMessage(content=[TextPart(text="different payload")])],
        "fake:scripted",
    )

    assert first == second
    assert reconciled == first * 2
    assert adjusted >= first
    assert estimator.error_ratio("fake:scripted") == -0.5


def test_tool_estimate_counts_the_model_visible_contract_only() -> None:
    estimator = ConservativeTokenEstimator()
    model_id = "fake:scripted"
    visible = CalculatorTool.spec
    internal_metadata_changed = visible.model_copy(
        update={
            "output_schema": {
                "type": "object",
                "description": "internal-only" * 10_000,
            },
            "timeout_seconds": visible.timeout_seconds + 1,
            "maximum_output_bytes": visible.maximum_output_bytes + 1,
        },
        deep=True,
    )
    visible_contract_changed = visible.model_copy(
        update={"description": visible.description + " Additional model-visible guidance."},
        deep=True,
    )

    baseline = estimator.estimate_tools([visible], model_id)

    assert estimator.estimate_tools([internal_metadata_changed], model_id) == baseline
    assert estimator.estimate_tools([visible_contract_changed], model_id) > baseline
