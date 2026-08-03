"""Provider-neutral conversation and model protocol types."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

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


class ModelUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
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


class ModelTurn(BaseModel):
    assistant_messages: list[AssistantMessage] = Field(default_factory=list)
    tool_calls: list[ToolCallItem] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    stop_reason: StopReason
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


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
    native_tool_calling: bool = True
    parallel_tool_calls: bool = True
    images: bool = False
    audio: bool = False
    files: bool = False
    provider_managed_state: bool = False
    explicit_cache_control: bool = False
    structured_output: bool = True
    streaming: bool = True


class ResolvedModel(BaseModel):
    provider: str
    model: str
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    credential_ref: str = "fake"
    policy_name: str = "balanced"
    resolved_at: datetime


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
    error: ModelError
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
    fail_with: ModelError | None = None
    delay_ms: int = 0


class FakeModelScript(BaseModel):
    turns: list[ScriptedTurn]
    on_exhausted: Literal["error", "repeat_last"] = "error"
