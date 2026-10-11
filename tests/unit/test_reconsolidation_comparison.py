"""Publication recomputes scores and keeps every failed case in its denominator."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest

from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest, ResolvedModel
from agent_core.evals.memory_reconsolidation import Answer, Observation, load_corpus
from agent_core.evals.memory_reconsolidation_comparison import ARMS, ComparisonRow, quality_failures
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_execution_cases import TrackedFactory

ROOT = Path(__file__).resolve().parents[2]


def ideal_rows() -> tuple[ComparisonRow, ...]:
    corpus = load_corpus(ROOT)
    result = []
    for arm in ARMS:
        for repeat in (1, 2, 3):
            for case in corpus.cases:
                result.append(
                    ComparisonRow(
                        arm=arm,
                        repeat=repeat,
                        observation=Observation(
                            case_id=case.id,
                            surviving_ids=case.required_survivors,
                            merges=case.duplicate_classes if arm != "original_only" else (),
                            hypotheses=case.hypotheses if arm == "connections" else (),
                            answers=tuple(
                                Answer(probe_id=p.id, text=p.answer.values[0]) for p in case.probes
                            )
                            if arm == "connections"
                            else (),
                        ),
                    )
                )
    return tuple(result)


def test_complete_comparison_uses_frozen_scorer() -> None:
    assert quality_failures(ROOT, ideal_rows()) == ()


def test_comparison_refuses_missing_duplicate_failed_or_unsafe_rows() -> None:
    rows = ideal_rows()
    assert quality_failures(ROOT, rows[:-1])
    assert quality_failures(ROOT, (*rows[:-1], rows[0]))
    assert quality_failures(
        ROOT, (rows[0].model_copy(update={"failure": "runtime_failure"}), *rows[1:])
    )
    assert quality_failures(ROOT, (rows[0].model_copy(update={"boundary_failures": 1}), *rows[1:]))
    bad = rows[-1].observation.model_copy(update={"surviving_ids": ()})
    assert quality_failures(ROOT, (*rows[:-1], rows[-1].model_copy(update={"observation": bad})))


def test_no_hypotheses_and_no_answer_lift_cannot_pass() -> None:
    rows = tuple(
        r.model_copy(
            update={
                "observation": r.observation.model_copy(update={"hypotheses": (), "answers": ()})
            }
        )
        for r in ideal_rows()
    )
    failures = quality_failures(ROOT, rows)
    assert any("hypothesis_quality" in reason for reason in failures)
    assert any("answer_coverage_lift" in reason for reason in failures)


async def test_runtime_collects_real_traces_without_labels_or_gold_answers() -> None:
    from agent_core.domain.messages import AssistantMessage, TextPart
    from agent_core.evals.memory_reconsolidation_control import runtime_case
    from agent_core.evals.memory_reconsolidation_runtime import (
        MeasuredProvider,
        collect_comparison_case,
    )
    from tests.contract.reconsolidation_admission_cases import model
    from tests.contract.reconsolidation_execution_cases import ExecutionProvider
    from tests.contract.support import memory_uow_factory

    _, factory = await memory_uow_factory()

    class Answers(ExecutionProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            if not request.metadata:
                self.requests.append(request)
                from agent_core.domain.messages import (
                    ModelCompletedEvent,
                    ModelTurn,
                    ModelUsage,
                    StopReason,
                )

                yield ModelCompletedEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=0,
                    stop_reason=StopReason.END_TURN,
                    turn=ModelTurn(
                        assistant_messages=[
                            AssistantMessage(item_index=0, content=[TextPart(text="unknown")])
                        ],
                        usage=ModelUsage(
                            input_tokens=100,
                            output_tokens=10,
                            provider=resolved.provider,
                            model=resolved.model,
                        ),
                        stop_reason=StopReason.END_TURN,
                    ),
                )
            else:
                async for event in super().stream(request, resolved, attempt):
                    yield event

    provider = Answers(TrackedFactory(cast(Factory, factory)))
    measured = MeasuredProvider(provider)
    case = load_corpus(ROOT).cases[0]
    runtime = runtime_case(case)
    assert set(type(runtime).model_fields) == {"id", "seeds", "probes"}
    for arm in ARMS:
        row = await collect_comparison_case(runtime, arm, 1, model=model(), provider=measured)
        assert row.failure is None
        assert len(row.observation.answers) == len(case.probes)
        assert len(row.trace_sha256) == len(case.probes)
        assert set(row.observation.surviving_ids) == set(case.required_survivors)
        assert not row.observation.hypotheses and not row.observation.merges
    assert measured.cost > 0


async def test_failed_provider_calls_remain_charged_and_counted() -> None:
    from uuid import UUID

    from agent_core.domain.messages import ModelAttempt, ModelRequest
    from agent_core.evals.memory_reconsolidation_runtime import MeasuredProvider
    from tests.contract.reconsolidation_admission_cases import model
    from tests.contract.support import NOW

    class FailedProvider:
        name = "openai"

        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            raise RuntimeError("transport failed")
            yield

        async def close(self) -> None:
            pass

    measured = MeasuredProvider(FailedProvider())
    request = ModelRequest(
        model_policy="balanced",
        tools=[],
        conversation=[],
        maximum_output_tokens=100,
        maximum_provider_attempts=1,
    )
    attempt = ModelAttempt(
        attempt_id=UUID(int=1), run_id=UUID(int=2), step_number=1, attempt_number=1, started_at=NOW
    )
    with pytest.raises(RuntimeError):
        async for _ in measured.stream(request, model(), attempt):
            pass
    assert measured.calls == measured.unknown_calls == 1
    assert measured.cost > 0


def test_answer_control_requires_complete_measured_census() -> None:
    from agent_core.evals.memory_reconsolidation_report import record
    from tests.contract.reconsolidation_admission_cases import model

    with pytest.raises(ValueError, match="census"):
        record(ROOT, ideal_rows()[:2], model(), purpose="answer_control", unknown_usage_calls=0)


def test_label_only_rows_cannot_become_measurement_evidence() -> None:
    from agent_core.evals.memory_reconsolidation_report import record
    from tests.contract.reconsolidation_admission_cases import model

    report = record(
        ROOT, ideal_rows(), model(), purpose="three_arm_comparison", unknown_usage_calls=0
    )
    assert "unmeasured_rows" in report.failures


async def test_noop_arms_send_identical_answer_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider wording variance in unchanged arms must not be called lost recall."""
    from agent_core.evals.memory_reconsolidation import Seed
    from agent_core.evals.memory_reconsolidation_control import (
        CONTROL_AT,
        RuntimeCase,
        RuntimeProbe,
    )
    from agent_core.evals.memory_reconsolidation_runtime import (
        MeasuredProvider,
        collect_comparison_case,
    )
    from tests.contract import reconsolidation_execution_cases as execution
    from tests.contract.reconsolidation_admission_cases import model
    from tests.contract.support import memory_uow_factory

    monkeypatch.setattr(execution, "reply", lambda request: "green")
    _, factory = await memory_uow_factory()
    provider = execution.ExecutionProvider(TrackedFactory(cast(Factory, factory)))
    case = RuntimeCase(
        id="fabricated-single-fact",
        seeds=(
            Seed(
                id="paint",
                statement="The owner paints model boats green.",
                subject="model boats",
                session="painting",
                event=1,
                evidence_at=CONTROL_AT,
            ),
        ),
        probes=(RuntimeProbe(id="color", question="What color are the owner's model boats?"),),
    )
    measured = MeasuredProvider(provider)
    rows = [
        await collect_comparison_case(case, arm, 1, model=model(), provider=measured)
        for arm in ARMS
    ]
    assert all(row.failure is None and row.provider_calls == 1 for row in rows)
    assert all(row.observation == rows[0].observation for row in rows)
    assert len(provider.requests) == 3
    assert all(request == provider.requests[0] for request in provider.requests)
