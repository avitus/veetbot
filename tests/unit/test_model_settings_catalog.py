"""What the owner may choose, and what applies when nothing valid is stored (ADR-0118)."""

from datetime import UTC, datetime

import pytest

from agent_core.domain.messages import ReasoningEffort
from agent_core.domain.model_settings import (
    ChatModelOption,
    MemoryModelOption,
    ModelChoice,
    ModelSettings,
    ModelSettingsCatalog,
    chat_reasoning_effort,
)

EFFORTS = tuple(ReasoningEffort)
NOW = datetime(2026, 9, 23, tzinfo=UTC)


def catalog() -> ModelSettingsCatalog:
    return ModelSettingsCatalog(
        chat_default=ModelChoice(model_policy="astra", reasoning_effort=ReasoningEffort.HIGH),
        chat_options=(
            ChatModelOption(
                model_policy="astra",
                display_name="GPT-6 Astra",
                provider="openai",
                model="gpt-6-astra",
                reasoning_efforts=EFFORTS,
            ),
            ChatModelOption(
                model_policy="local",
                display_name="Qwen3 8B (local)",
                provider="chat_completions",
                model="qwen3:8b",
                reasoning_efforts=(),
            ),
        ),
        memory_default=ModelChoice(model_policy="balanced"),
        memory_options=(
            MemoryModelOption(
                model_policy="balanced",
                display_name="GPT-5.6 Sol",
                provider="openai",
                model="gpt-5.6-sol",
                reasoning_effort=None,
            ),
            MemoryModelOption(
                model_policy="astra",
                display_name="GPT-6 Astra",
                provider="openai",
                model="gpt-6-astra",
                reasoning_effort=ReasoningEffort.MEDIUM,
            ),
        ),
    )


def stored(chat: ModelChoice, memory: ModelChoice) -> ModelSettings:
    return ModelSettings(
        tenant_id="local",
        principal_id="owner",
        version=1,
        chat=chat,
        memory=memory,
        created_at=NOW,
    )


def test_a_chat_choice_is_offered_only_with_an_effort_its_model_accepts() -> None:
    offered = catalog()

    assert offered.offers_chat(
        ModelChoice(model_policy="astra", reasoning_effort=ReasoningEffort.MAX)
    )
    assert offered.offers_chat(ModelChoice(model_policy="local"))
    assert not offered.offers_chat(ModelChoice(model_policy="astra"))
    assert not offered.offers_chat(
        ModelChoice(model_policy="local", reasoning_effort=ReasoningEffort.LOW)
    )
    assert not offered.offers_chat(
        ModelChoice(model_policy="fable", reasoning_effort=ReasoningEffort.HIGH)
    )


def test_a_memory_choice_must_match_one_evaluated_tuple_exactly() -> None:
    offered = catalog()

    assert offered.offers_memory(ModelChoice(model_policy="balanced"))
    assert offered.offers_memory(
        ModelChoice(model_policy="astra", reasoning_effort=ReasoningEffort.MEDIUM)
    )
    assert not offered.offers_memory(
        ModelChoice(model_policy="astra", reasoning_effort=ReasoningEffort.HIGH)
    )
    assert not offered.offers_memory(
        ModelChoice(model_policy="balanced", reasoning_effort=ReasoningEffort.LOW)
    )


def test_nothing_stored_or_a_withdrawn_choice_falls_back_to_the_deployment_default() -> None:
    offered = catalog()
    withdrawn = stored(
        ModelChoice(model_policy="fable", reasoning_effort=ReasoningEffort.HIGH),
        ModelChoice(model_policy="astra", reasoning_effort=ReasoningEffort.HIGH),
    )
    kept = stored(
        ModelChoice(model_policy="astra", reasoning_effort=ReasoningEffort.LOW),
        ModelChoice(model_policy="astra", reasoning_effort=ReasoningEffort.MEDIUM),
    )

    assert offered.effective_chat(None) == offered.chat_default
    assert offered.effective_memory(None) == offered.memory_default
    assert offered.effective_chat(withdrawn) == offered.chat_default
    assert offered.effective_memory(withdrawn) == offered.memory_default
    assert offered.effective_chat(kept) == kept.chat
    assert offered.effective_memory(kept) == kept.memory


def test_the_defaults_must_themselves_be_offered() -> None:
    shape = catalog().model_dump()

    with pytest.raises(ValueError, match="chat default"):
        ModelSettingsCatalog.model_validate({**shape, "chat_default": {"model_policy": "fable"}})
    with pytest.raises(ValueError, match="memory default"):
        ModelSettingsCatalog.model_validate(
            {**shape, "memory_default": {"model_policy": "astra", "reasoning_effort": "low"}}
        )


def test_the_chat_effort_is_sent_only_to_a_model_that_accepts_it() -> None:
    choice = ModelChoice(model_policy="astra", reasoning_effort=ReasoningEffort.XHIGH)

    assert chat_reasoning_effort(choice, EFFORTS) is ReasoningEffort.XHIGH
    assert chat_reasoning_effort(choice, ()) is None
    assert chat_reasoning_effort(ModelChoice(model_policy="astra"), EFFORTS) is None


def test_a_chat_model_variant_is_its_own_stable_agent() -> None:
    from uuid import UUID

    from agent_core.domain.agents import AgentSpec, chat_model_variant
    from agent_core.domain.runs import RunLimits

    base = AgentSpec(
        id=UUID("8ad3e17d-449f-5ec8-a807-4e14f2b3a716"),
        version="1.0.0+habc",
        name="Agent",
        instructions="Help.",
        model_policy="astra",
        enabled_tools=["math.calculate"],
        policy_profile="default",
        limits=RunLimits(),
    )

    fable = chat_model_variant(base, "fable")

    assert chat_model_variant(base, "astra") == base
    assert fable.model_policy == "fable"
    assert fable.id != base.id
    assert fable == chat_model_variant(base, "fable")
    assert fable.version.startswith("1.0.0+h")
    assert chat_model_variant(
        base.model_copy(update={"instructions": "New."}), "fable"
    ).version != (fable.version)
    assert fable.model_dump(exclude={"id", "version", "model_policy"}) == base.model_dump(
        exclude={"id", "version", "model_policy"}
    )
