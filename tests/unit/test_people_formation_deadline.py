"""Formation's total deadline must not cancel a progressing provider prematurely."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.memory import ProviderExtractionFailure
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelEvent,
    ModelRequest,
    ResolvedModel,
    ScriptedTurn,
)
from agent_core.memory.distillation import (
    NemoriAssistedCandidateExtractor,
    PeopleAssistedCandidateExtractor,
)
from tests.contract.support import ids, memory_uow_factory, principal


class ProgressingProvider(FakeModelProvider):
    delay: float = 0.75
    cancelled: bool = False

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        try:
            async for event in super().stream(request, resolved, attempt):
                await asyncio.sleep(self.delay)
                yield event
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def extractor(
    cls: type[NemoriAssistedCandidateExtractor],
    *,
    timeout: float | None = None,
) -> tuple[NemoriAssistedCandidateExtractor, ProgressingProvider]:
    clock, factory = await memory_uow_factory()
    provider = ProgressingProvider(FakeModelScript(turns=[ScriptedTurn(text="{}")]), clock)
    return cls(
        provider=provider,
        resolved_model=ResolvedModel(provider="fake", model="scripted", resolved_at=clock.now()),
        uow_factory=factory,
        clock=clock,
        ids=ids(),
        timeout_seconds=timeout,
    ), provider


async def call(
    instance: NemoriAssistedCandidateExtractor,
) -> tuple[str | None, ProviderExtractionFailure | None, dict[str, int | str | None]]:
    return await instance._call(
        stage="prediction_error_distillation",
        prompt="Synthetic source for an operational deadline regression.",
        response_schema={"type": "object", "properties": {}, "additionalProperties": False},
        principal=principal(),
        run_id=None,
    )


def scaled_deadline(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    original = asyncio.timeout
    observed: list[float] = []

    def timeout(seconds: float) -> asyncio.Timeout:
        observed.append(seconds)
        return original(seconds / 100)

    monkeypatch.setattr(asyncio, "timeout", timeout)
    return observed


async def test_people_stream_can_finish_after_transport_interval_without_request_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = scaled_deadline(monkeypatch)
    repaired, provider = await extractor(PeopleAssistedCandidateExtractor)
    raw, failure, metric = await call(repaired)
    assert raw == "{}" and failure is None
    assert not provider.cancelled and metric["outcome"] == "success"
    assert limits == [600.0]
    # Transport settings and every model-visible field stay at their original values.
    control, original = await extractor(PeopleAssistedCandidateExtractor, timeout=120.0)
    original.delay = 0
    await call(control)
    assert provider.requests[0].model_dump() == original.requests[0].model_dump()
    assert provider.requests[0].timeout_seconds == 120.0
    assert provider.requests[0].stream_idle_seconds == 10.0


async def test_frozen_nemori_control_keeps_original_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = scaled_deadline(monkeypatch)
    control, provider = await extractor(NemoriAssistedCandidateExtractor)
    raw, failure, _ = await call(control)
    assert raw is None and failure is not None and provider.cancelled
    assert failure.error_class == "TimeoutError" and limits == [120.0]


async def test_explicit_override_is_exact_and_local_deadline_has_distinct_audit_origin() -> None:
    instance, provider = await extractor(PeopleAssistedCandidateExtractor, timeout=0.01)
    raw, failure, metric = await call(instance)
    assert raw is None and failure is not None and provider.cancelled
    assert failure.error_class == "TimeoutError"
    assert metric["timeout_origin"] == "stage_deadline"
    assert metric["stage_deadline_seconds"] == "0.01"


async def test_external_cancellation_is_not_converted_to_fallback() -> None:
    instance, provider = await extractor(PeopleAssistedCandidateExtractor)
    task = asyncio.create_task(call(instance))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled


async def test_default_people_deadline_is_finite(monkeypatch: pytest.MonkeyPatch) -> None:
    original = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: original(seconds / 100_000))
    instance, provider = await extractor(PeopleAssistedCandidateExtractor)
    raw, failure, metric = await call(instance)
    assert raw is None and failure is not None and provider.cancelled
    assert metric["stage_deadline_seconds"] == "600.0"
    assert metric["timeout_origin"] == "stage_deadline"
    assert len(provider.requests) == 1
