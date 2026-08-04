from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.messages import TextPart, UserMessage


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
