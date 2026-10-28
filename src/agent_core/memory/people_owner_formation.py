"""Current owner-claim normalization outside the frozen distillation implementation."""

import re

from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import BeliefType, MemoryCandidate, MemoryClaimKind, MemoryDerivation
from agent_core.memory.distillation import _DistilledCandidate
from agent_core.memory.people_context_distillation import ContextualPeopleCandidateExtractor


class OwnerPeopleCandidateExtractor(ContextualPeopleCandidateExtractor):
    """Retain People evidence while classifying direct possessions as owner attributes."""

    @staticmethod
    def _normalize_candidate(
        candidate: _DistilledCandidate, *, by_sequence: dict[int, EventEnvelope], scope: str
    ) -> MemoryCandidate:
        result = ContextualPeopleCandidateExtractor._normalize_candidate(
            candidate, by_sequence=by_sequence, scope=scope
        )
        if (
            result.claim_kind is MemoryClaimKind.RESOURCE
            and result.derivation is MemoryDerivation.DIRECT
            and re.match(
                r"^User\s+(?:(?:no\s+longer\s+)?(?:has|owns|drives|uses)|"
                r"does\s+not\s+(?:have|own|drive|use))\s+",
                result.statement,
                re.I,
            )
        ):
            result = result.model_copy(update={"belief_type": BeliefType.USER_MODEL_ATTR})
        return result
