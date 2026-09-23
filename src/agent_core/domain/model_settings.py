"""The owner's model settings (ADR-0119).

One versioned document per principal names the chat model new app chats use,
the reasoning effort every agent run sends, and the model and effort memory
formation uses. The document stores choices only; which choices are offered,
and what an absent document means, is decided by the composition that serves
them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.messages import ReasoningEffort

MODEL_POLICY_PATTERN = r"^[a-z][a-z0-9_-]*$"
# The effort a chat sends until the owner chooses another (ADR-0119).
DEFAULT_CHAT_REASONING_EFFORT = ReasoningEffort.HIGH


class ModelChoice(BaseModel):
    """A model policy and the reasoning effort sent with it.

    A null effort sends none, so the provider's own default applies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_policy: str = Field(pattern=MODEL_POLICY_PATTERN, max_length=64)
    reasoning_effort: ReasoningEffort | None = None


class ModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    chat: ModelChoice
    memory: ModelChoice
    created_at: datetime


class ChatModelOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_policy: str
    display_name: str
    provider: str
    model: str
    reasoning_efforts: tuple[ReasoningEffort, ...] = ()


class MemoryModelOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_policy: str
    display_name: str
    provider: str
    model: str
    reasoning_effort: ReasoningEffort | None = None


class ModelSettingsCatalog(BaseModel):
    """The choices a composition offers and the defaults it applies.

    A chat choice names an offered model and one effort that model accepts,
    or no effort when it accepts none. A memory choice must equal one offered
    tuple exactly, because each tuple stands for evaluated evidence. A stored
    choice that is no longer offered is ignored in favour of the default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chat_default: ModelChoice
    chat_options: tuple[ChatModelOption, ...]
    memory_default: ModelChoice
    memory_options: tuple[MemoryModelOption, ...]

    @model_validator(mode="after")
    def _defaults_are_offered(self) -> ModelSettingsCatalog:
        if not self.offers_chat(self.chat_default):
            raise ValueError("the chat default must be an offered chat choice")
        if not self.offers_memory(self.memory_default):
            raise ValueError("the memory default must be an offered memory choice")
        return self

    def chat_option(self, model_policy: str) -> ChatModelOption | None:
        return next(
            (option for option in self.chat_options if option.model_policy == model_policy),
            None,
        )

    def offers_chat(self, choice: ModelChoice) -> bool:
        option = self.chat_option(choice.model_policy)
        if option is None:
            return False
        if choice.reasoning_effort is None:
            return not option.reasoning_efforts
        return choice.reasoning_effort in option.reasoning_efforts

    def offers_memory(self, choice: ModelChoice) -> bool:
        return any(
            option.model_policy == choice.model_policy
            and option.reasoning_effort == choice.reasoning_effort
            for option in self.memory_options
        )

    def effective_chat(self, stored: ModelSettings | None) -> ModelChoice:
        if stored is not None and self.offers_chat(stored.chat):
            return stored.chat
        return self.chat_default

    def effective_memory(self, stored: ModelSettings | None) -> ModelChoice:
        if stored is not None and self.offers_memory(stored.memory):
            return stored.memory
        return self.memory_default


def chat_reasoning_effort(
    choice: ModelChoice, accepted: tuple[ReasoningEffort, ...]
) -> ReasoningEffort | None:
    """The effort a run sends: the chosen one, when the run's model accepts it.

    A session keeps the model it was created with, so a run may be on a model
    other than the one now chosen for new chats. That model receives the
    chosen effort when it accepts it and otherwise its provider default.
    """

    if choice.reasoning_effort in accepted:
        return choice.reasoning_effort
    return None


class ChatModelOptionView(ChatModelOption):
    default_reasoning_effort: ReasoningEffort | None = None


class ModelSettingsView(BaseModel):
    """What the owner has in effect and every choice on offer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=0)
    chat: ModelChoice
    memory: ModelChoice
    chat_options: tuple[ChatModelOptionView, ...]
    memory_options: tuple[MemoryModelOption, ...]

    @classmethod
    def build(
        cls, catalog: ModelSettingsCatalog, stored: ModelSettings | None
    ) -> ModelSettingsView:
        return cls(
            version=0 if stored is None else stored.version,
            chat=catalog.effective_chat(stored),
            memory=catalog.effective_memory(stored),
            chat_options=tuple(
                ChatModelOptionView(
                    **option.model_dump(),
                    default_reasoning_effort=(
                        DEFAULT_CHAT_REASONING_EFFORT
                        if DEFAULT_CHAT_REASONING_EFFORT in option.reasoning_efforts
                        else (option.reasoning_efforts[0] if option.reasoning_efforts else None)
                    ),
                )
                for option in catalog.chat_options
            ),
            memory_options=catalog.memory_options,
        )
