from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelCompletedEvent,
    ModelFailedEvent,
    ModelRequest,
    ModelTransientError,
    ResolvedModel,
    ScriptedTurn,
    UserMessage,
)
from tests.contract.support import NOW, RUN_ID


async def test_model_provider_stream_has_contiguous_sequence_and_one_terminal() -> None:
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="done")]), FixedClock(NOW)
    )
    request = ModelRequest(model_policy="fake", conversation=[UserMessage(content=[])], tools=[])
    attempt = ModelAttempt(
        attempt_id=RUN_ID,
        run_id=RUN_ID,
        step_number=1,
        attempt_number=1,
        started_at=NOW,
    )
    resolved = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    events = [event async for event in provider.stream(request, resolved, attempt)]
    assert [event.sequence for event in events] == list(range(len(events)))
    assert sum(isinstance(event, ModelCompletedEvent) for event in events) == 1
    assert provider.attempts == [attempt]
    await provider.close()


def test_repeat_last_uses_the_latest_context_matching_turn() -> None:
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(text="fallback"),
                ScriptedTurn(text="gated", context_contains="required marker"),
            ],
            on_exhausted="repeat_last",
        ),
        FixedClock(NOW),
    )
    provider._index = 2
    request = ModelRequest(model_policy="fake", conversation=[UserMessage(content=[])], tools=[])

    assert provider._next_turn(request).text == "fallback"


def test_failed_event_round_trip_preserves_error_subtype_fields() -> None:
    event = ModelFailedEvent(
        attempt_id=RUN_ID,
        run_id=RUN_ID,
        step_number=1,
        sequence=0,
        error=ModelTransientError(
            provider="fake",
            model="scripted",
            attempt_id=RUN_ID,
            message="retry",
            stream_had_output=True,
        ),
    )
    restored = ModelFailedEvent.model_validate(event.model_dump(mode="json"))
    assert isinstance(restored.error, ModelTransientError)
    assert restored.error.stream_had_output is True
