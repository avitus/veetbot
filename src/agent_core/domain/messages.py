"""Provider-neutral conversation and model protocol types."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.policies import TrustLevel


class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str


class ImageReferencePart(BaseModel):
    kind: Literal["image"] = "image"
    artifact_id: UUID
    media_type: str
    detail: str = "auto"


class FileReferencePart(BaseModel):
    kind: Literal["file"] = "file"
    artifact_id: UUID
    media_type: str
    filename: str | None = None


type ContentPart = TextPart | ImageReferencePart | FileReferencePart


class SystemMessage(BaseModel):
    kind: Literal["system"] = "system"
    content: list[ContentPart]
    trust: TrustLevel = TrustLevel.PLATFORM


class UserMessage(BaseModel):
    kind: Literal["user"] = "user"
    content: list[ContentPart]
    trust: TrustLevel = TrustLevel.USER
    principal_id: str | None = None


class AssistantMessage(BaseModel):
    kind: Literal["assistant"] = "assistant"
    content: list[ContentPart]
    item_index: int = 0
    trust: TrustLevel = TrustLevel.EXTERNAL_UNTRUSTED


class ToolCallItem(BaseModel):
    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    item_index: int
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    parse_error: str | None = None


class ToolResultItem(BaseModel):
    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    content: list[ContentPart]
    is_error: bool = False
    trust: TrustLevel = TrustLevel.INTERNAL_TOOL


class ProviderReasoningItem(BaseModel):
    kind: Literal["provider_reasoning"] = "provider_reasoning"
    item_index: int
    provider: str
    provider_payload: dict[str, Any]
    token_count: int | None = None
    trust_level: TrustLevel = TrustLevel.PLATFORM


type ConversationItem = (
    SystemMessage
    | UserMessage
    | AssistantMessage
    | ToolCallItem
    | ToolResultItem
    | ProviderReasoningItem
)


class PendingToolCall(BaseModel):
    call_id: str
    item_index: int
    name: str
    raw_arguments: str
    parse_error: str | None = None


class CostSource(StrEnum):
    PROVIDER_COST_API = "provider_cost_api"
    GENERATION_USAGE = "generation_usage"
    MODEL_CATALOG = "model_catalog"
    DOCS_SNAPSHOT = "docs_snapshot"
    CONFIG_OVERRIDE = "config_override"


class ReasoningSupport(StrEnum):
    NONE = "none"
    NATIVE = "native"
    IN_BAND = "in_band"


class Capability(StrEnum):
    NATIVE_TOOL_CALLING = "native_tool_calling"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    IMAGES = "images"
    AUDIO = "audio"
    FILES = "files"
    REASONING = "reasoning"
    PROVIDER_MANAGED_STATE = "provider_managed_state"
    EXPLICIT_CACHE_CONTROL = "explicit_cache_control"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"


type CapabilitySet = frozenset[Capability]


class ModelUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cost: Decimal = Decimal("0")
    cost_source: CostSource = CostSource.CONFIG_OVERRIDE
    provider: str = "fake"
    model: str = "scripted"


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    CONTENT_FILTER = "content_filter"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"


class ProviderMetadata(BaseModel):
    """The closed, content-free metadata vocabulary shared by all adapters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_api: Literal["responses", "messages", "chat_completions"]
    response_id: str | None = None
    request_id: str | None = None
    resolved_model: str | None = None
    previous_response_id: str | None = None
    cache_breakpoints_sent: int = Field(default=0, ge=0)
    cache_breakpoints_dropped: int = Field(default=0, ge=0)


class ModelTurn(BaseModel):
    assistant_messages: list[AssistantMessage] = Field(default_factory=list)
    tool_calls: list[ToolCallItem] = Field(default_factory=list)
    provider_reasoning_items: list[ProviderReasoningItem] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    stop_reason: StopReason
    provider_metadata: ProviderMetadata | None = None


class ModelError(BaseModel):
    kind: Literal["transient", "permanent", "protocol"]
    provider: str
    model: str
    attempt_id: UUID
    message: str
    provider_code: str | None = None
    http_status: int | None = None


class ModelTransientError(ModelError):
    kind: Literal["transient"] = "transient"
    retry_after: timedelta | None = None
    stream_had_output: bool = False


class ModelPermanentError(ModelError):
    kind: Literal["permanent"] = "permanent"


class ModelProtocolError(ModelError):
    kind: Literal["protocol"] = "protocol"
    detail: str


type ModelFailure = Annotated[
    ModelTransientError | ModelPermanentError | ModelProtocolError,
    Field(discriminator="kind"),
]


class ModelAttempt(BaseModel):
    attempt_id: UUID
    run_id: UUID
    step_number: int
    attempt_number: int
    started_at: datetime


class CacheBreakpoint(BaseModel):
    boundary: str
    min_tokens: int = 1024
    ttl: str = "default"


class CacheHints(BaseModel):
    breakpoints: list[CacheBreakpoint] = Field(default_factory=list)


class ModelRequest(BaseModel):
    model_policy: str
    conversation: list[ConversationItem]
    tools: list[Any]
    response_schema: dict[str, Any] | None = None
    temperature: float | None = None
    maximum_output_tokens: int | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    cache_hints: CacheHints | None = None
    timeout_seconds: float = 600.0
    stream_idle_seconds: float = 60.0


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    native_tool_calling: bool = True
    parallel_tool_calls: bool = True
    images: bool = False
    audio: bool = False
    files: bool = False
    reasoning: ReasoningSupport = ReasoningSupport.NONE
    provider_managed_state: bool = False
    explicit_cache_control: bool = False
    structured_output: bool = True
    streaming: bool = True

    def enabled(self) -> CapabilitySet:
        values: set[Capability] = set()
        for capability in Capability:
            if capability is Capability.REASONING:
                if self.reasoning is not ReasoningSupport.NONE:
                    values.add(capability)
                continue
            if bool(getattr(self, capability.value)):
                values.add(capability)
        return frozenset(values)


class ModelLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_window_tokens: int = Field(default=32768, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    default_output_reserve: int = Field(default=4096, gt=0)
    max_cache_breakpoints: int = Field(default=0, ge=0)
    max_tool_count: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_composed_limits(self) -> ModelLimits:
        if self.default_output_reserve > self.max_output_tokens:
            raise ValueError("default output reserve exceeds maximum output tokens")
        if self.max_output_tokens > self.context_window_tokens:
            raise ValueError("maximum output tokens exceed context window")
        return self


class ModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    cached_input_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    cache_write_per_mtok: Decimal | None = Field(default=None, ge=0)
    output_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    reasoning_per_mtok: Decimal | None = Field(default=None, ge=0)
    reasoning_priced_separately: bool = False
    source: CostSource = CostSource.MODEL_CATALOG
    effective_at: datetime | None = None


class ResolvedModel(BaseModel):
    provider: str
    model: str
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    limits: ModelLimits = Field(default_factory=ModelLimits)
    pricing: ModelPricing = Field(default_factory=ModelPricing)
    credential_ref: str = "fake"
    policy_name: str = "balanced"
    resolved_at: datetime


class ProviderPin(BaseModel):
    run_id: UUID
    provider: str
    model: str
    registry_version: str
    pinned_at: datetime


class ModelEventBase(BaseModel):
    attempt_id: UUID
    run_id: UUID
    step_number: int
    sequence: int


class TextDeltaEvent(ModelEventBase):
    kind: Literal["text_delta"] = "text_delta"
    item_index: int
    text: str


class ReasoningDeltaEvent(ModelEventBase):
    kind: Literal["reasoning_delta"] = "reasoning_delta"
    item_index: int
    text: str
    is_summary: bool


class ToolCallDeltaEvent(ModelEventBase):
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    item_index: int
    call_id: str | None
    name: str | None
    arguments_delta: str


class UsageEvent(ModelEventBase):
    kind: Literal["usage"] = "usage"
    usage: ModelUsage
    is_final: Literal[False] = False


class ModelCompletedEvent(ModelEventBase):
    kind: Literal["completed"] = "completed"
    turn: ModelTurn
    stop_reason: StopReason
    stop_sequence: str | None = None


class ModelFailedEvent(ModelEventBase):
    kind: Literal["failed"] = "failed"
    error: ModelFailure
    partial_turn: ModelTurn | None = None


type ModelEvent = (
    TextDeltaEvent
    | ReasoningDeltaEvent
    | ToolCallDeltaEvent
    | UsageEvent
    | ModelCompletedEvent
    | ModelFailedEvent
)


class ScriptedToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] | str
    call_id: str | None = None


class ScriptedTurn(BaseModel):
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)
    stop_reason: StopReason = StopReason.END_TURN
    usage: ModelUsage | None = None
    fail_with: ModelFailure | None = None
    delay_ms: int = 0


class FakeModelScript(BaseModel):
    turns: list[ScriptedTurn]
    on_exhausted: Literal["error", "repeat_last"] = "error"
