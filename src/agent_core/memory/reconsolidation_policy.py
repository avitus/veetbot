"""Local per-content classification and explicit provider residency admission."""

import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from agent_core.domain.agents import Principal
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation_inputs import EgressSubject, ReconsolidationEgressDecision
from agent_core.memory.reconsolidation_inputs import EgressPolicy

POLICY_VERSION = "reconsolidation-egress@1"
# Classification examines every complete content value, including raw excerpts.
# Memory classification is a floor, never a label assigned to its whole episode.
_PRIVATE = re.compile(
    r"(?:[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b\d[\d ()+-]{7,}\d\b|"
    r"\b(?:health|medical|diagnos\w*|symptom\w*|therapy|disease|disabil\w*|"
    r"sexual\w*|religio\w*|muslim|christian|jewish|gay|lesbian|bisexual|transgender|politic\w*|ethnic\w*|racial|pregnan\w*|"
    r"depress\w*|autis\w*|adhd|salary|income|bank|account number|address|"
    r"social security|passport|confidential|private|secret)\b)",
    re.I,
)


def privacy_digest(residency_provider: str | None) -> str:
    return hashlib.sha256(
        json.dumps([POLICY_VERSION, residency_provider], separators=(",", ":")).encode()
    ).hexdigest()


def local_egress_policy(residency_provider: str | None) -> EgressPolicy:
    """No configured residency permission means no provider export."""

    def assess(
        principal: Principal, model: ResolvedModel, subject: EgressSubject, now: datetime
    ) -> ReconsolidationEgressDecision:
        sensitivity = subject.sensitivity_floor or Sensitivity.INTERNAL
        if contains_secret_material(subject.text) or contains_injection_pattern(subject.text):
            sensitivity = Sensitivity.RESTRICTED
        elif _PRIVATE.search(subject.text):
            sensitivity = max(
                (sensitivity, Sensitivity.SENSITIVE), key=SENSITIVITY_ORDER.__getitem__
            )
        residency: Literal["allowed", "unknown"] = (
            "allowed" if residency_provider == model.provider else "unknown"
        )
        known = bool(subject.text.strip()) and len(subject.text.encode("utf-8")) <= 65536
        return ReconsolidationEgressDecision(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            provider=model.provider,
            model=model.model,
            policy_version=POLICY_VERSION,
            assessed_at=now,
            permitted=known
            and residency == "allowed"
            and sensitivity in {Sensitivity.PUBLIC, Sensitivity.INTERNAL},
            sensitivity=sensitivity if known else None,
            residency=residency,
        )

    return assess
