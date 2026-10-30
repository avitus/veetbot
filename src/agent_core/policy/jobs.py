"""The jobs publication rule has its own policy identity (ADR-0268)."""

from agent_core.domain.agents import Principal
from agent_core.domain.policies import LoadedRuleset, PolicyDecision, ProposedAction
from agent_core.domain.runs import Run
from agent_core.policy.loader import load_ruleset_documents
from agent_core.ports.policies import PolicyEngine


def job_publication_ruleset(base: LoadedRuleset) -> LoadedRuleset:
    """Keep unrelated evaluated policy inputs byte-for-byte unchanged."""
    tool_rules = {
        rule.tool_name: rule.model_dump(mode="json", exclude={"tool_name"}, exclude_none=True)
        for rule in base.tool_rules
    }
    # An operator's explicit job rule takes precedence over this MVP default.
    tool_rules.setdefault("job.manage", {"decision": "allow"})
    tool_rules.setdefault("cockpit.install", {"decision": "require_approval"})
    profile = {
        "schema_version": 1,
        "name": base.profile_name + "-jobs",
        "default_effect": base.default_effect.value,
        "rules": {
            rule.side_effect.value: rule.model_dump(
                mode="json", exclude={"side_effect"}, exclude_none=True
            )
            for rule in base.rules
        },
        "tool_rules": tool_rules,
        "unknown_tool": {"decision": base.unknown_tool_decision.value},
        "trust_overlay": {
            "external_untrusted_requires_approval": base.external_untrusted_requires_approval
        },
        "approval_expiry_seconds": {
            risk.value: seconds for risk, seconds in base.approval_expiry_seconds
        },
        "self_approval": {"enabled": base.self_approval_enabled},
        "advisory": {"enabled": base.advisory_enabled},
    }
    return load_ruleset_documents(
        profile,
        {"schema_version": 1, "rules": [rule.model_dump(mode="json") for rule in base.hardline]},
    )


class JobPolicyEngine:
    def __init__(self, base: PolicyEngine, publication: PolicyEngine) -> None:
        self.base = base
        self.publication = publication

    async def evaluate(
        self, action: ProposedAction, principal: Principal, run: Run
    ) -> PolicyDecision:
        """Use the jobs profile for job.manage and preserve other policy identities."""
        engine = self.publication if action.name in {"job.manage", "cockpit.install"} else self.base
        return await engine.evaluate(action, principal, run)
