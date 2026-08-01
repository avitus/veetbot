from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelCompletedEvent,
    ModelRequest,
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
