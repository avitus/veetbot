"""Milestone 29 hard gates for the typed-judgment port (ADR-0110)."""

import pytest

from agent_core.adapters.judgment import SHIPPED_JUDGMENT_PROVIDERS
from tests.contract import test_judgment_provider_contract as provider_contract
from tests.unit import test_config as config_contract
from tests.unit import test_judgment_composition as composition_contract


async def test_fixed_egress() -> None:
    """One hard-coded endpoint, no redirect, and a credential resolved per call and kept nowhere."""

    await provider_contract.test_typesafe_sends_the_exact_wire_request()
    await provider_contract.test_typesafe_omits_absent_noul_criteria()
    await provider_contract.test_typesafe_follows_no_redirect()
    await provider_contract.test_typesafe_resolves_the_credential_on_every_call_and_keeps_none()


async def test_default_off(caplog: pytest.LogCaptureFixture) -> None:
    """Off until selected; a credential alone enables nothing; a missing key degrades."""

    config_contract.test_judgment_provider_is_disabled_until_selected()
    config_contract.test_judgment_provider_selects_typesafe()
    config_contract.test_unknown_judgment_provider_fails_at_load()
    await composition_contract.test_no_judgment_provider_is_composed_until_the_selector_names_one()
    await composition_contract.test_the_typesafe_selector_composes_the_typesafe_provider()
    await composition_contract.test_a_selector_without_a_credential_warns_and_never_refuses_startup(
        caplog
    )
    await provider_contract.test_typesafe_fails_without_dialing_when_the_credential_is_missing()


async def test_typed_failure() -> None:
    """Six reason codes and nothing else; bounded retries; no body, state, or key in a failure."""

    for status, reason, retryable, calls in provider_contract.STATUS_CASES:
        await provider_contract.test_typesafe_classifies_failure_by_status_alone(
            status, reason, retryable, calls
        )
    for failure in provider_contract.TRANSPORT_FAILURES.values():
        await provider_contract.test_typesafe_maps_timeouts_and_transport_failures_to_unavailable(
            failure
        )
    await provider_contract.test_typesafe_retries_transient_failures_on_the_injected_clock()
    await provider_contract.test_typesafe_bounds_the_whole_call_in_time()
    for unusable in provider_contract.UNUSABLE_RESPONSES.values():
        await provider_contract.test_typesafe_refuses_an_unusable_response_without_retrying(
            unusable
        )
    await provider_contract.test_typesafe_fails_without_dialing_when_the_request_is_too_large()
    for leaking in provider_contract.LEAK_RESPONSES.values():
        await provider_contract.test_typesafe_failures_carry_no_body_state_or_credential(leaking)


async def test_priced_contract() -> None:
    """Local pricing at the pinned price, and one contract over the production census."""

    await provider_contract.test_typesafe_prices_usage_locally_from_the_returned_input_tokens()
    await provider_contract.test_typesafe_replaces_a_hostile_model_name_with_the_requested_alias()
    provider_contract.test_shipped_provider_census_is_owned_by_the_production_package()
    for implementation in SHIPPED_JUDGMENT_PROVIDERS:
        await provider_contract.test_choice_contract(implementation)
        await provider_contract.test_every_question_kind_is_answered_in_one_call(implementation)
        for answers in provider_contract.MISMATCHED_RESULTS.values():
            await provider_contract.test_a_result_that_does_not_answer_the_request_is_invalid(
                implementation, answers
            )
    await provider_contract.test_score_beyond_its_last_level_is_invalid_on_both_subjects()
