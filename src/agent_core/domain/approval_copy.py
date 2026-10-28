"""What an approval card says (ADR-0260).

The headline names the kind of action and the reason names what made the
platform ask. Both come from closed tables keyed on the tool's side-effect
class and the policy decision, never from model text, so a page that steered
the model cannot also write the explanation the owner reads.
"""

from __future__ import annotations

from agent_core.domain.policies import PolicyDecision, SideEffectClass
from agent_core.domain.tools import ToolSpec

# The advisory layer's reason code (policy/advised.py); the domain cannot import it.
ADVISORY_ESCALATED = "policy.advisory.escalated"
FALLBACK_REASON = "Veetbot's safety rules ask for your approval on this action."
ADVISORY_REASON = "A second safety check flagged this request, so Veetbot asks first."

# The headline for a tool that does not describe its own approvals.
_ACTION_PHRASES: dict[SideEffectClass, str] = {
    SideEffectClass.EXTERNAL_MESSAGE: "Send a message",
    SideEffectClass.EXTERNAL_WRITE: "Make a change outside Veetbot",
    SideEffectClass.EXTERNAL_DELETE: "Delete something outside Veetbot",
    SideEffectClass.FINANCIAL: "Spend money",
    SideEffectClass.PUBLICATION: "Publish something",
    SideEffectClass.CODE_EXECUTION: "Run code in Veetbot's sandbox",
    SideEffectClass.WORKSPACE_WRITE: "Write a file in this chat's workspace",
}

# Classes whose own matrix row asks: the class itself is the reason.
_CLASS_REASONS: dict[SideEffectClass, str] = {
    SideEffectClass.EXTERNAL_MESSAGE: "Veetbot asks before sending a message for you.",
    SideEffectClass.EXTERNAL_WRITE: "Veetbot asks before changing anything outside Veetbot.",
    SideEffectClass.EXTERNAL_DELETE: "Veetbot asks before deleting anything outside Veetbot.",
    SideEffectClass.FINANCIAL: "Veetbot asks before spending money.",
    SideEffectClass.PUBLICATION: "Veetbot asks before publishing anything.",
}

# What the trust overlay held back, for a class it can escalate.
_ESCALATED_ACTIONS: dict[SideEffectClass, str] = {
    SideEffectClass.CODE_EXECUTION: "running code",
    SideEffectClass.WORKSPACE_WRITE: "writing files",
}


def generic_action_summary(spec: ToolSpec) -> str:
    phrase = _ACTION_PHRASES.get(spec.side_effect)
    return f"Use {spec.name}" if phrase is None else f"{phrase} with {spec.name}"


def approval_reason(decision: PolicyDecision, side_effect: SideEffectClass) -> str:
    """One sentence on why the platform asked, never a policy code."""
    if decision.reason_code == ADVISORY_ESCALATED:
        return ADVISORY_REASON
    if decision.untrusted_escalation:
        action = _ESCALATED_ACTIONS.get(side_effect, "taking this action")
        return (
            "This chat has read web pages or other outside content, "
            f"so Veetbot asks before {action}."
        )
    return _CLASS_REASONS.get(side_effect, FALLBACK_REASON)


def legacy_approval_reason(reason_code: str) -> str:
    """The reason for an approval stored before it kept its own sentence."""
    if reason_code == ADVISORY_ESCALATED:
        return ADVISORY_REASON
    prefix = "policy.matrix."
    if reason_code.startswith(prefix):
        try:
            side_effect = SideEffectClass(reason_code.removeprefix(prefix))
        except ValueError:
            return FALLBACK_REASON
        return _CLASS_REASONS.get(side_effect, FALLBACK_REASON)
    return FALLBACK_REASON
