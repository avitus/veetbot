import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.messages import (
    AssistantMessage,
    FakeModelScript,
    ImageReferencePart,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ModelUsage,
    ResolvedModel,
    ScriptedTurn,
    StopReason,
)
from agent_core.evals.general_recall_formation_bank import FormationBank
from tests.unit.test_general_recall_corpus import AT


async def invoke(bank: FormationBank, text: str = "same") -> list[ModelCompletedEvent]:
    from agent_core.domain.messages import TextPart, UserMessage

    return [
        event
        async for event in bank.stream(
            ModelRequest(
                model_policy="balanced",
                conversation=[UserMessage(content=[TextPart(text=text)])],
                tools=[],
                maximum_output_tokens=100,
            ),
            ResolvedModel(provider="fake", model="scripted", resolved_at=AT),
            ModelAttempt(
                attempt_id=UUID(int=1),
                run_id=UUID(int=2),
                step_number=1,
                attempt_number=1,
                started_at=AT,
            ),
        )
        if isinstance(event, ModelCompletedEvent)
    ]


async def test_paired_formation_reuses_exact_provider_output_without_another_send(
    tmp_path: Path,
) -> None:
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="First"), ScriptedTurn(text="Different")]),
        FixedClock(AT),
    )
    path = tmp_path / "bank.json"
    first = FormationBank(provider, path, "1" * 64)
    expected = await invoke(first)
    first.finish("2" * 64)
    second = FormationBank(provider, path, "1" * 64)
    actual = await invoke(second)
    second.finish("2" * 64)
    assert len(provider.requests) == 1, "A retrieval comparison must not reform its memories"
    assert actual[0].turn.assistant_messages == expected[0].turn.assistant_messages
    assert actual[0].turn.usage == expected[0].turn.usage
    assert actual[0].sequence == 0
    assert actual[0].turn.provider_metadata is None
    assert actual[0].turn.provider_reasoning_items == []


@pytest.mark.parametrize("change", ["identity", "request", "state", "missing_call", "extra_call"])
async def test_formation_replay_refuses_a_changed_or_incomplete_comparison(
    tmp_path: Path, change: str
) -> None:
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="First") for _ in range(4)]), FixedClock(AT)
    )
    path = tmp_path / "bank.json"
    first = FormationBank(provider, path, "1" * 64)
    await invoke(first)
    first.finish("2" * 64)
    with pytest.raises(ValueError, match="formation bank"):
        second = FormationBank(provider, path, ("3" if change == "identity" else "1") * 64)
        if change != "missing_call":
            await invoke(second, "changed" if change == "request" else "same")
        if change == "extra_call":
            await invoke(second)
        second.finish(("3" if change == "state" else "2") * 64)
    assert len(provider.requests) == 1


async def test_bank_omits_reasoning_and_provider_metadata(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text='{"facts":[]}',
                    reasoning="Private reasoning must not be retained",
                    provider_reasoning_payload={"opaque": "Private provider continuation"},
                )
            ]
        ),
        FixedClock(AT),
    )
    path = tmp_path / "bank.json"
    bank = FormationBank(provider, path, "1" * 64)
    await invoke(bank)
    bank.finish("2" * 64)
    assert "Private" not in path.read_text()
    assert not path.with_suffix(".json.pending.jsonl").exists()


async def test_unfinished_recording_cannot_be_replaced(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="First")]), FixedClock(AT)
    )
    path = tmp_path / "bank.json"
    bank = FormationBank(provider, path, "1" * 64)
    await invoke(bank)
    with pytest.raises(FileExistsError):
        FormationBank(provider, path, "1" * 64)
    assert len(provider.requests) == 1


async def test_changed_bank_bytes_cannot_certify_an_already_loaded_replay(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="First")]), FixedClock(AT)
    )
    path = tmp_path / "bank.json"
    first = FormationBank(provider, path, "1" * 64)
    await invoke(first)
    first.finish("2" * 64)
    second = FormationBank(provider, path, "1" * 64)
    await invoke(second)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="formation bank"):
        second.finish("2" * 64)


async def test_known_token_limit_replays_exact_refusal_usage_and_following_request(
    tmp_path: Path,
) -> None:
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text="Partial text must not become a recorded completion",
                    reasoning="Private reasoning",
                    provider_reasoning_payload={"opaque": "Private state"},
                    stop_reason=StopReason.MAX_TOKENS,
                    usage=ModelUsage(cost=Decimal("0.42"), output_tokens=100),
                ),
                ScriptedTurn(text='{"facts":[]}'),
            ]
        ),
        FixedClock(AT),
    )
    path = tmp_path / "bank.json"
    first = FormationBank(provider, path, "1" * 64)
    with pytest.raises(ValueError, match="ordinary structured completion"):
        await invoke(first)
    await invoke(first, "following stage")
    first.finish("2" * 64)
    original = path.read_bytes()
    encoded = json.loads(original)
    assert encoded["version"] == "paired-formation-bank@2"
    assert encoded["calls"][0]["kind"] == "token_limit_refusal"
    assert "completion" not in encoded["calls"][0]
    assert "Partial text" not in original.decode() and "Private" not in original.decode()
    second = FormationBank(provider, path, "1" * 64)
    with pytest.raises(ValueError, match="ordinary structured completion"):
        await invoke(second)
    await invoke(second, "following stage")
    second.finish("2" * 64)
    assert len(provider.requests) == 2
    assert first.usages == second.usages
    assert second.usages[0].cost == Decimal("0.42")
    assert path.read_bytes() == original


async def test_token_limit_refusal_cannot_be_relabelled_as_a_legacy_bank(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(stop_reason=StopReason.MAX_TOKENS)]), FixedClock(AT)
    )
    path = tmp_path / "bank.json"
    bank = FormationBank(provider, path, "1" * 64)
    with pytest.raises(ValueError):
        await invoke(bank)
    bank.finish("2" * 64)
    raw = json.loads(path.read_bytes())
    raw.pop("version")
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        FormationBank(provider, path, "1" * 64)


@pytest.mark.parametrize("damage", ["stop_reason", "retry", "image", "mismatch"])
async def test_other_unsupported_terminals_still_cannot_seal(tmp_path: Path, damage: str) -> None:
    class Unsupported(FakeModelProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            async for event in super().stream(request, resolved, attempt):
                if isinstance(event, ModelCompletedEvent):
                    if damage == "stop_reason":
                        event = event.model_copy(update={"stop_reason": StopReason.CONTENT_FILTER})
                    elif damage == "retry":
                        event = event.model_copy(update={"internal_retry_count": 1})
                    elif damage == "mismatch":
                        event = event.model_copy(
                            update={
                                "turn": event.turn.model_copy(
                                    update={"stop_reason": StopReason.END_TURN}
                                )
                            }
                        )
                    else:
                        event = event.model_copy(
                            update={
                                "turn": event.turn.model_copy(
                                    update={
                                        "assistant_messages": [
                                            AssistantMessage(
                                                content=[
                                                    ImageReferencePart(
                                                        artifact_id=UUID(int=1),
                                                        media_type="image/png",
                                                    )
                                                ]
                                            )
                                        ]
                                    }
                                )
                            }
                        )
                yield event

    provider = Unsupported(
        FakeModelScript(turns=[ScriptedTurn(stop_reason=StopReason.MAX_TOKENS)]), FixedClock(AT)
    )
    path = tmp_path / "bank.json"
    bank = FormationBank(provider, path, "1" * 64)
    with pytest.raises(ValueError):
        await invoke(bank)
    with pytest.raises(ValueError, match="failed"):
        bank.finish("2" * 64)
    assert not path.exists() and path.with_suffix(".json.pending.jsonl").exists()


async def test_legacy_bank_still_replays_without_rewriting_its_bytes(tmp_path: Path) -> None:
    provider = FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text="Same")]), FixedClock(AT))
    path = tmp_path / "bank.json"
    first = FormationBank(provider, path, "1" * 64)
    await invoke(first)
    first.finish("2" * 64)
    value = json.loads(path.read_bytes())
    value.pop("version")
    original = json.dumps(value).encode()
    path.write_bytes(original)
    replay = FormationBank(provider, path, "1" * 64)
    await invoke(replay)
    replay.finish("2" * 64)
    assert path.read_bytes() == original and len(provider.requests) == 1


async def test_actual_formation_stage_repeats_the_same_audited_fallback(tmp_path: Path) -> None:
    from agent_core.memory.distillation import PeopleAssistedCandidateExtractor
    from tests.contract.support import ids, memory_uow_factory
    from tests.unit.test_people_formation_deadline import call

    clock, factory = await memory_uow_factory()
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="Partial", stop_reason=StopReason.MAX_TOKENS)]),
        clock,
    )
    results = []
    for _ in range(2):
        bank = FormationBank(provider, tmp_path / "bank.json", "1" * 64)
        extractor = PeopleAssistedCandidateExtractor(
            provider=bank,
            resolved_model=ResolvedModel(
                provider="fake", model="scripted", resolved_at=clock.now()
            ),
            uow_factory=factory,
            clock=clock,
            ids=ids(),
        )
        result, failure, metrics = await call(extractor)
        assert result is None and failure is not None
        assert failure.error_class == "ValueError" and metrics["outcome"] == "provider_failure"
        results.append(failure)
        bank.finish("2" * 64)
    assert results[0] == results[1] and len(provider.requests) == 1
