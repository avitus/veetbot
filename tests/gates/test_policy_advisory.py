"""Milestone 30 hard gates for the advisory approval layer (ADR-0111)."""

from pathlib import Path

import pytest

from agent_core.domain.policies import AdvisoryVerdictType, PolicyDecisionType, SideEffectClass
from agent_core.policy.judgment_advisor import SHIPPED_POLICY_ADVISORS
from tests.contract import test_policy_advisor_contract as advisor_contract
from tests.unit import test_advised_policy_engine as composite
from tests.unit import test_judgment_policy_advisor as advisor
from tests.unit import test_policy_advisory_composition as composition
from tests.unit import test_policy_loader_advisory_flag as loader
from tests.unit import test_tool_pipeline_recovery_policy as recovery


async def test_advisory_monotonic() -> None:
    """Through the composite, no verdict lowers a rank, adds modifications, or moves the version."""

    for deterministic in PolicyDecisionType:
        for verdict in AdvisoryVerdictType:
            await composite.test_the_composite_never_lowers_a_rank(deterministic, verdict)
    await composite.test_an_escalation_on_a_plain_allow_requires_approval_without_content()
    advisor_contract.test_an_advisor_cannot_say_allow()


async def test_advisory_allow_path_once(tmp_path: Path) -> None:
    """Allow paths and the consulted class only; never at revalidation or for an existing row."""

    for deterministic in (
        PolicyDecisionType.ALLOW_WITH_MODIFICATIONS,
        PolicyDecisionType.REQUIRE_APPROVAL,
        PolicyDecisionType.DENY,
    ):
        await composite.test_the_advisor_is_never_called_off_the_plain_allow_path(deterministic)
    for effect, target, name in (
        ("none", "in_process", "web.search"),
        ("workspace_write", "in_process", "web.search"),
        ("code_execution", "sandbox", "web.search"),
        ("network_read", "mcp", "gmail_read.search_threads"),
    ):
        action = composite.web_action(name=name, effect=SideEffectClass(effect), target=target)
        await composite.test_actions_outside_the_consulted_class_are_not_advised(action)
    await composition.test_an_enforced_escalation_is_approved_once_and_asks_the_vendor_once(
        tmp_path
    )
    await recovery.test_an_existing_invocation_is_never_put_to_the_primary_policy_again()
    await recovery.test_an_escalation_recorded_before_a_crash_survives_it()
    await recovery.test_a_persisted_decision_under_another_policy_version_does_not_bind()
    await recovery.test_one_engine_behaves_exactly_as_before_with_no_extra_lookup()


async def test_advisory_abstains(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A timeout, an error, and a missing provider each leave the deterministic decision intact."""

    await composite.test_an_unavailable_advisor_abstains_byte_identically(
        composite.ScriptedAdvisor(failure=RuntimeError("judgment.provider_unavailable")),
        "RuntimeError",
    )
    await composite.test_an_unavailable_advisor_abstains_byte_identically(
        composite.ScriptedAdvisor(AdvisoryVerdictType.REQUIRE_APPROVAL, delay=5.0), "timeout"
    )
    await composite.test_cancellation_is_not_swallowed_as_an_abstention()
    await advisor.test_a_provider_failure_propagates_for_the_composite_to_abstain_on()
    await (
        composition.test_the_layer_without_a_provider_uses_the_deterministic_engine_and_warns_once(
            tmp_path, caplog, lambda path: composition._environment(path, enforce=True)
        )
    )


async def test_advisory_blind_redacted() -> None:
    """No rule, profile, identifier, or decision reaches a request; arguments are masked."""

    await advisor.test_the_state_is_redacted_delimited_and_carries_no_identifier_or_rule()
    await advisor.test_instructions_and_criteria_are_platform_authored()
    await advisor.test_a_truncated_or_oversize_argument_escalates_without_a_request()
    advisor.test_the_advisor_module_imports_neither_the_loader_nor_the_hardline_module()
    for implementation in SHIPPED_POLICY_ADVISORS:
        advisor_contract.test_an_advisor_receives_the_action_and_nothing_else(implementation)


async def test_advisory_default_off(tmp_path: Path) -> None:
    """Off makes no request, observing changes no decision, and nothing shipped denies."""

    loader.test_the_shipped_profile_keeps_the_advisory_layer_off_and_its_version_unmoved()
    loader.test_an_enabled_profile_loads_as_enforcing_and_changes_the_policy_version()
    await composition.test_the_layer_is_off_by_default_and_makes_no_judgment_request(
        tmp_path / "off"
    )
    await composition.test_observe_mode_consults_the_advisor_and_changes_no_decision(
        tmp_path / "observe"
    )
    await composite.test_observe_mode_records_the_verdict_and_changes_no_decision()
    await advisor.test_no_shipped_verdict_is_a_denial_over_a_probability_grid()
    for implementation in SHIPPED_POLICY_ADVISORS:
        await advisor_contract.test_no_shipped_advisor_denies(implementation)
