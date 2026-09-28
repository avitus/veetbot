import pytest

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


def test_owner_attachments_are_counted_as_what_the_adapter_may_send() -> None:
    """ADR-0120: a reference serializes small, so the file itself is added."""

    from uuid import UUID

    from agent_core.domain.messages import FileReferencePart, ToolResultItem
    from agent_core.model import attachments

    estimator = ConservativeTokenEstimator()
    pdf = FileReferencePart(
        artifact_id=UUID(int=1),
        media_type="application/pdf",
        filename="a.pdf",
        size_bytes=10_000,
        page_count=5,
    )
    text_only = [UserMessage(content=[TextPart(text="read this")])]
    with_pdf = [UserMessage(content=[TextPart(text="read this"), pdf])]
    in_tool_result = [ToolResultItem(call_id="c", content=[pdf])]
    base = estimator.estimate(text_only, "fake:scripted")
    assert estimator.estimate(with_pdf, "fake:scripted") >= (
        base + 5 * attachments.PDF_TOKENS_PER_PAGE + attachments.LABEL_TOKENS
    )
    # A reference outside an owner message is only ever a one-line marker.
    assert estimator.estimate(in_tool_result, "fake:scripted") < 5 * attachments.PDF_TOKENS_PER_PAGE


def test_reconciliation_only_ever_raises_estimates_and_only_for_its_model() -> None:
    """Over-estimates are the safe direction: observed savings never shrink the budget."""

    estimator = ConservativeTokenEstimator()
    items = [UserMessage(content=[TextPart(text="a payload that stays the same")])]
    base = estimator.estimate(items, "fake:scripted")

    estimator.reconcile("fake:scripted", base, base // 4)
    assert estimator.estimate(items, "fake:scripted") == base
    assert estimator.error_ratio("fake:scripted") == pytest.approx((base - base // 4) / (base // 4))

    estimator.reconcile("fake:other", base, base * 3)
    assert estimator.estimate(items, "fake:scripted") == base
    assert estimator.estimate(items, "fake:other") == base * 3


def test_reconciliation_accumulates_and_rejects_impossible_observations() -> None:
    estimator = ConservativeTokenEstimator()
    assert estimator.error_ratio("fake:scripted") is None
    estimator.reconcile("fake:scripted", 100, 0)
    assert estimator.error_ratio("fake:scripted") is None
    estimator.reconcile("fake:scripted", 100, 300)
    # Cumulative: 200 estimated against 300 used, so the factor is 1.5, not 3.
    base = ConservativeTokenEstimator().estimate_text("x" * 30, "fake:scripted")
    assert base == 14
    assert estimator.estimate_text("x" * 30, "fake:scripted") == 21
    for estimated, actual in ((0, 10), (-1, 10), (10, -1)):
        with pytest.raises(ValueError, match="positive estimates and nonnegative use"):
            estimator.reconcile("fake:scripted", estimated, actual)


def test_memo_eviction_never_changes_an_estimate() -> None:
    with pytest.raises(ValueError, match="memo capacity must be positive"):
        ConservativeTokenEstimator(memo_capacity=0)
    bounded = ConservativeTokenEstimator(memo_capacity=2)
    unbounded = ConservativeTokenEstimator()
    texts = [f"payload number {index} " * (index + 1) for index in range(6)]

    first = [bounded.estimate_text(text, "fake:scripted") for text in texts]
    again = [bounded.estimate_text(text, "fake:scripted") for text in reversed(texts)]

    assert first == [unbounded.estimate_text(text, "fake:scripted") for text in texts]
    assert again == list(reversed(first))
    assert len(bounded._memo) == 2
