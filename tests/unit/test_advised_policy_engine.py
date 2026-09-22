"""The advisory layer can only escalate, runs on allow paths only, and abstains on failure."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.policies import (
    ActionKind,
    AdvisoryVerdict,
    AdvisoryVerdictType,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecision,
    PolicyDecisionRank,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.policy.advised import ADVISORY_ESCALATED, AdvisedPolicyEngine, advisable
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, run

POLICY_VERSION = "default@000000000000+h00000000"
RANK = {
    PolicyDecisionType.ALLOW: PolicyDecisionRank.ALLOW,
    PolicyDecisionType.ALLOW_WITH_MODIFICATIONS: PolicyDecisionRank.ALLOW_WITH_MODIFICATIONS,
    PolicyDecisionType.REQUIRE_APPROVAL: PolicyDecisionRank.REQUIRE_APPROVAL,
    PolicyDecisionType.DENY: PolicyDecisionRank.DENY,
}


def web_action(
    *,
    name: str = "web.search",
    effect: SideEffectClass = SideEffectClass.NETWORK_READ,
    target: str = "web_provider",
    arguments: dict[str, object] | None = None,
    origin: TrustLevel = TrustLevel.USER,
) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=71),
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name=name,
        version="1.0.0",
        summary=f"Run {name} with validated arguments.",
        side_effect=effect,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(),
        arguments=arguments or {"query": "weather in lisbon"},
        normalized_arguments_hash="hash",
        argument_trust={},
        origin_trust=origin,
        target=ExecutionTarget(kind=target, isolated=False, network_enabled=True),
        evaluated_at=NOW,
    )


class ScriptedEngine:
    def __init__(
        self, decision: PolicyDecisionType, *, modified: dict[str, object] | None = None
    ) -> None:
        self._decision = PolicyDecision(
            decision=decision,
            reason_code="policy.matrix.network_read",
            explanation="The default profile evaluated network_read.",
            modified_arguments=modified,
            policy_version=POLICY_VERSION,
        )

    async def evaluate(self, action: object, principal: object, run: object) -> PolicyDecision:
        del action, principal, run
        return self._decision


class ScriptedAdvisor:
    def __init__(
        self,
        verdict: AdvisoryVerdictType = AdvisoryVerdictType.ABSTAIN,
        *,
        failure: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self._verdict = verdict
        self._failure = failure
        self._delay = delay
        self.calls: list[ProposedAction] = []

    async def advise(self, action: ProposedAction) -> AdvisoryVerdict:
        self.calls.append(action)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._failure is not None:
            raise self._failure
        signals = () if self._verdict is AdvisoryVerdictType.ABSTAIN else ("personal_data",)
        return AdvisoryVerdict(verdict=self._verdict, signals=signals, advisor_version="scripted@1")


class RecordingObserver:
    def __init__(self) -> None:
        self.consultations: list[dict[str, object]] = []
        self.abstentions: list[dict[str, object]] = []

    def consulted(self, **fields: object) -> None:
        self.consultations.append(fields)

    def abstained(self, **fields: object) -> None:
        self.abstentions.append(fields)


def engine(
    decision: PolicyDecisionType,
    advisor: ScriptedAdvisor,
    *,
    enforce: bool = True,
    observer: RecordingObserver | None = None,
    **options: object,
) -> AdvisedPolicyEngine:
    return AdvisedPolicyEngine(
        ScriptedEngine(decision, modified={"q": "narrowed"} if "modif" in decision.value else None),
        advisor,
        enforce=enforce,
        clock=FixedClock(NOW),
        observer=observer,
        **options,  # type: ignore[arg-type]
    )


async def test_an_escalation_on_a_plain_allow_requires_approval_without_content() -> None:
    advisor = ScriptedAdvisor(AdvisoryVerdictType.REQUIRE_APPROVAL)
    decision = await engine(PolicyDecisionType.ALLOW, advisor).evaluate(
        web_action(arguments={"query": "jane doe home address 12 elm street"}), principal(), run()
    )

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
    assert decision.reason_code == ADVISORY_ESCALATED == "policy.advisory.escalated"
    assert decision.modified_arguments is None
    assert decision.policy_version == POLICY_VERSION
    assert "personal_data" in decision.explanation and "scripted@1" in decision.explanation
    assert "jane" not in decision.explanation.lower()
    assert len(advisor.calls) == 1


@pytest.mark.parametrize("deterministic", list(PolicyDecisionType))
@pytest.mark.parametrize("verdict", list(AdvisoryVerdictType))
async def test_the_composite_never_lowers_a_rank(
    deterministic: PolicyDecisionType, verdict: AdvisoryVerdictType
) -> None:
    baseline = await ScriptedEngine(
        deterministic, modified={"q": "narrowed"} if "modif" in deterministic.value else None
    ).evaluate(None, None, None)
    decision = await engine(deterministic, ScriptedAdvisor(verdict)).evaluate(
        web_action(), principal(), run()
    )

    assert RANK[decision.decision] >= RANK[deterministic]
    assert decision.policy_version == POLICY_VERSION
    # Only the deterministic layer may produce modifications.
    assert decision.modified_arguments in (None, baseline.modified_arguments)
    if decision.decision is deterministic:
        assert decision == baseline


@pytest.mark.parametrize(
    "deterministic",
    [
        PolicyDecisionType.ALLOW_WITH_MODIFICATIONS,
        PolicyDecisionType.REQUIRE_APPROVAL,
        PolicyDecisionType.DENY,
    ],
)
async def test_the_advisor_is_never_called_off_the_plain_allow_path(
    deterministic: PolicyDecisionType,
) -> None:
    advisor = ScriptedAdvisor(AdvisoryVerdictType.DENY)
    baseline = await engine(deterministic, ScriptedAdvisor()).evaluate(
        web_action(), principal(), run()
    )
    decision = await engine(deterministic, advisor).evaluate(web_action(), principal(), run())

    assert advisor.calls == []
    assert decision == baseline


@pytest.mark.parametrize(
    "action",
    [
        web_action(effect=SideEffectClass.NONE, target="in_process"),
        web_action(effect=SideEffectClass.WORKSPACE_READ, target="in_process"),
        web_action(effect=SideEffectClass.WORKSPACE_WRITE, target="in_process"),
        web_action(effect=SideEffectClass.CODE_EXECUTION, target="sandbox"),
        web_action(name="gmail_read.search_threads", target="mcp"),
        web_action(
            name="device.sms.send", effect=SideEffectClass.EXTERNAL_MESSAGE, target="device"
        ),
    ],
    ids=["none", "workspace-read", "workspace-write", "code", "mcp-read", "sms"],
)
async def test_actions_outside_the_consulted_class_are_not_advised(action: ProposedAction) -> None:
    advisor = ScriptedAdvisor(AdvisoryVerdictType.REQUIRE_APPROVAL)
    decision = await engine(PolicyDecisionType.ALLOW, advisor).evaluate(action, principal(), run())

    assert advisable(action) is False
    assert advisor.calls == []
    assert decision.decision is PolicyDecisionType.ALLOW


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("web.search", "web_provider"),
        ("web.fetch", "web_provider"),
        ("browser.navigate", "browser_provider"),
    ],
)
def test_web_and_browser_network_reads_are_the_consulted_class(name: str, target: str) -> None:
    assert advisable(web_action(name=name, target=target)) is True


class _CodedError(RuntimeError):
    """Stands in for a provider error: the composite knows no provider type."""

    def __init__(self, reason_code: object) -> None:
        super().__init__("synthetic")
        self.reason_code = reason_code


@pytest.mark.parametrize(
    ("reason_code", "cause"),
    [
        ("judgment.payment_required", "judgment.payment_required"),
        # Anything that is not a short code could carry content, so the class name stands in.
        ("the owner's query was: synthetic private text", "_CodedError"),
        (402, "_CodedError"),
    ],
    ids=["code", "prose", "not-a-string"],
)
async def test_an_abstention_is_counted_under_the_failure_reason_code(
    reason_code: object, cause: str
) -> None:
    observer = RecordingObserver()
    await engine(
        PolicyDecisionType.ALLOW,
        ScriptedAdvisor(failure=_CodedError(reason_code)),
        observer=observer,
    ).evaluate(web_action(), principal(), run())

    assert observer.abstentions == [{"tool": "web.search", "cause": cause}]


@pytest.mark.parametrize(
    ("advisor", "cause"),
    [
        (ScriptedAdvisor(failure=RuntimeError("judgment.provider_unavailable")), "RuntimeError"),
        (ScriptedAdvisor(AdvisoryVerdictType.REQUIRE_APPROVAL, delay=5.0), "timeout"),
    ],
    ids=["error", "timeout"],
)
async def test_an_unavailable_advisor_abstains_byte_identically(
    advisor: ScriptedAdvisor, cause: str
) -> None:
    observer = RecordingObserver()
    baseline = await ScriptedEngine(PolicyDecisionType.ALLOW).evaluate(None, None, None)
    decision = await engine(
        PolicyDecisionType.ALLOW, advisor, observer=observer, timeout_seconds=0.05
    ).evaluate(web_action(), principal(), run())

    assert decision == baseline
    assert decision.model_dump_json() == baseline.model_dump_json()
    assert observer.abstentions == [{"tool": "web.search", "cause": cause}]
    assert observer.consultations == []


async def test_observe_mode_records_the_verdict_and_changes_no_decision() -> None:
    observer = RecordingObserver()
    advisor = ScriptedAdvisor(AdvisoryVerdictType.REQUIRE_APPROVAL)
    baseline = await ScriptedEngine(PolicyDecisionType.ALLOW).evaluate(None, None, None)
    decision = await engine(
        PolicyDecisionType.ALLOW, advisor, enforce=False, observer=observer
    ).evaluate(web_action(), principal(), run())

    assert decision.model_dump_json() == baseline.model_dump_json()
    assert len(advisor.calls) == 1
    (consultation,) = observer.consultations
    assert consultation["verdict"] == "require_approval"
    assert consultation["enforced"] is False
    assert consultation["signals"] == ("personal_data",)
    assert consultation["tool"] == "web.search"


async def test_cancellation_is_not_swallowed_as_an_abstention() -> None:
    advisor = ScriptedAdvisor(failure=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await engine(PolicyDecisionType.ALLOW, advisor).evaluate(web_action(), principal(), run())
