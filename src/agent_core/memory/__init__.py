"""Builtin long-term memory formation and retrieval."""

from agent_core.memory.formation import DeterministicCandidateExtractor
from agent_core.memory.model_extraction import ModelAssistedCandidateExtractor
from agent_core.memory.provider_extraction import ProviderAssistedCandidateExtractor

SHIPPED_MEMORY_CANDIDATE_EXTRACTORS = (
    DeterministicCandidateExtractor,
    ModelAssistedCandidateExtractor,
    ProviderAssistedCandidateExtractor,
)
"""Authoritative census of extractor implementations shipped by this package."""

__all__ = ["SHIPPED_MEMORY_CANDIDATE_EXTRACTORS"]
