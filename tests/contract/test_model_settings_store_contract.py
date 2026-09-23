"""Owner model-settings store contract (ADR-0118)."""

from datetime import timedelta

import pytest

from agent_core.adapters.persistence.memory import InMemoryModelSettingsStore
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import ReasoningEffort
from agent_core.domain.model_settings import ModelChoice, ModelSettings
from agent_core.ports.model_settings import ModelSettingsStore
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT, principal


def settings(
    *,
    version: int = 1,
    chat_policy: str = "astra",
    principal_id: str = PRINCIPAL_ID,
) -> ModelSettings:
    return ModelSettings(
        tenant_id=TENANT,
        principal_id=principal_id,
        version=version,
        chat=ModelChoice(model_policy=chat_policy, reasoning_effort=ReasoningEffort.HIGH),
        memory=ModelChoice(model_policy="balanced", reasoning_effort=None),
        created_at=NOW + timedelta(seconds=version),
    )


def subjects() -> list[ModelSettingsStore]:
    return [InMemoryModelSettingsStore()]


@pytest.mark.parametrize("store", subjects())
async def test_unsaved_settings_read_as_absent_and_the_head_wins(
    store: ModelSettingsStore,
) -> None:
    assert await store.current(principal()) is None

    await store.append_version(settings(version=1), expected_version=0)
    await store.append_version(settings(version=2, chat_policy="fable"), expected_version=1)

    head = await store.current(principal())
    assert head is not None
    assert (head.version, head.chat.model_policy) == (2, "fable")


@pytest.mark.parametrize("store", subjects())
async def test_a_write_must_name_the_current_head(store: ModelSettingsStore) -> None:
    await store.append_version(settings(version=1), expected_version=0)

    with pytest.raises(ConflictError):
        await store.append_version(settings(version=2), expected_version=0)
    with pytest.raises(ConflictError):
        await store.append_version(settings(version=3), expected_version=1)


@pytest.mark.parametrize("store", subjects())
async def test_settings_never_cross_principals(store: ModelSettingsStore) -> None:
    await store.append_version(settings(version=1), expected_version=0)

    other = principal().model_copy(update={"principal_id": "other"})
    assert await store.current(other) is None
    await store.append_version(settings(version=1, principal_id="other"), expected_version=0)
    mine = await store.current(principal())
    assert mine is not None and mine.principal_id == PRINCIPAL_ID


def test_a_choice_rejects_a_malformed_policy_name() -> None:
    with pytest.raises(ValueError):
        ModelChoice(model_policy="Not A Policy")
