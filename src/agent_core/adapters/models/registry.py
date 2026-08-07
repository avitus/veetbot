"""Registered adapter APIs and capability ceilings, without SDK imports."""

from agent_core.domain.messages import ModelCapabilities, ReasoningSupport
from agent_core.model.registry import AdapterDefinition

OPENAI_CAPABILITY_CEILING = ModelCapabilities(
    native_tool_calling=True,
    parallel_tool_calls=True,
    images=True,
    audio=False,
    files=True,
    reasoning=ReasoningSupport.NATIVE,
    provider_managed_state=True,
    explicit_cache_control=False,
    structured_output=True,
    streaming=True,
)

ANTHROPIC_CAPABILITY_CEILING = ModelCapabilities(
    native_tool_calling=True,
    parallel_tool_calls=True,
    images=True,
    audio=False,
    files=True,
    reasoning=ReasoningSupport.NATIVE,
    provider_managed_state=True,
    explicit_cache_control=True,
    structured_output=True,
    streaming=True,
)

CHAT_COMPLETIONS_CAPABILITY_CEILING = ModelCapabilities(
    native_tool_calling=False,
    parallel_tool_calls=False,
    images=False,
    audio=False,
    files=False,
    reasoning=ReasoningSupport.IN_BAND,
    provider_managed_state=False,
    explicit_cache_control=False,
    structured_output=False,
    streaming=True,
)

ADAPTER_DEFINITIONS = {
    "openai": AdapterDefinition(
        apis=frozenset({"responses"}),
        ceiling=OPENAI_CAPABILITY_CEILING,
    ),
    "anthropic": AdapterDefinition(
        apis=frozenset({"messages"}),
        ceiling=ANTHROPIC_CAPABILITY_CEILING,
    ),
    "chat_completions": AdapterDefinition(
        apis=frozenset({"chat_completions"}),
        ceiling=CHAT_COMPLETIONS_CAPABILITY_CEILING,
    ),
}
