from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelCompletedEvent,
    ModelRequest,
    ResolvedModel,
    ScriptedTurn,
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
