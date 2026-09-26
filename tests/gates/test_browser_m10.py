"""Milestone 10 hard gates for authenticated browser automation."""

from pathlib import Path
from uuid import UUID

from agent_core.domain.browser import BrowserAuthenticationStatus
from tests.contract import test_browser_authentication_repository_contract as auth_repository
from tests.contract import test_browser_grant_repository_contract as grant_repository
from tests.contract import test_browser_profile_repository_contract as profile_repository
from tests.contract import test_browser_provider_contract as provider_contract
from tests.contract import test_browser_public_api_contract as public_api_contract
from tests.contract import test_browser_task_grant_api_contract as task_grant_api
from tests.contract import test_browser_task_grant_repository_contract as task_grant_repository
from tests.contract import test_encrypted_profile_store_contract as encrypted_store
from tests.contract import (
    test_hosted_profile_lifecycle_service_contract as lifecycle_service,
)
from tests.contract import test_hosted_profile_session_service_contract as session_service
from tests.contract import test_profile_service_configuration_contract as service_configuration
from tests.contract import test_profile_service_http_contract as service_http
from tests.integration import test_browser_task_grant_pipeline as task_grant_pipeline
from tests.unit import test_browser_act_approval_view as approval_view
from tests.unit import test_browser_action_classification as classification
from tests.unit import test_browser_composition as composition_contract
from tests.unit import test_browser_dispatch_constraint as dispatch_constraint
from tests.unit import test_browser_grants as grant_authority
from tests.unit import test_browser_management as management_contract
from tests.unit import test_browser_playwright as playwright_contract
from tests.unit import test_browser_policy as policy_contract
from tests.unit import test_browser_task_grant_authorizer as task_grant_authority
from tests.unit import test_browser_task_grant_domain as task_grant_domain
from tests.unit import test_browser_tools as tool_contract
from tests.unit import test_hosted_browser_runtime as hosted_runtime
from tests.unit import test_maintenance_task_grant_sweep as task_grant_sweep
from tests.unit import test_persistence_schema as persistence_schema
from tests.unit import test_toolchain as toolchain_contract
from tests.unit.test_browser_composition import (
    test_exact_standing_browser_grant_authorizes_without_interactive_approval as exact_grant_pipeline,  # noqa: E501
)
from tests.unit.test_browser_composition import (
    test_revoked_standing_browser_grant_falls_back_to_interactive_approval as revoked_grant_pipeline,  # noqa: E501
)
from tests.unit.test_hosted_browser_provider import (
    test_hosted_provider_keeps_one_lease_per_run_attempt_and_rotates_between_attempts as hosted_lease_contract,  # noqa: E501
)


async def test_provider_contract() -> None:
    await provider_contract.test_browser_provider_navigation_and_observation_contract()
    await provider_contract.test_playwright_adapter_satisfies_browser_provider_contract()


async def test_default_off_composition() -> None:
    await composition_contract.test_browser_capabilities_are_absent_without_bound_provider()


async def test_origin_isolation() -> None:
    await playwright_contract.test_playwright_provider_starts_with_scrubbed_egress_policy()
    await playwright_contract.test_playwright_provider_rejects_out_of_policy_origin_before_start()
    await tool_contract.test_navigate_rejects_disallowed_url_before_provider_dispatch()
    await tool_contract.test_navigate_rejects_public_url_outside_bound_origin_policy()


async def test_observation_trust() -> None:
    await composition_contract.test_browser_navigation_persists_policy_checked_untrusted_result()
    await tool_contract.test_navigate_bounds_multibyte_element_names_within_tool_ceiling()


async def test_action_authority() -> None:
    policy_contract.test_browser_act_registration_requires_conservative_write_classification()
    await (
        composition_contract.test_browser_action_waits_for_approval_then_records_effect_watermark()
    )


async def test_revision_binding() -> None:
    await playwright_contract.test_playwright_provider_dispatches_revision_bound_action()
    await tool_contract.test_browser_act_rejects_mismatched_action_fields_before_watermark()


async def test_uncertain_write() -> None:
    await composition_contract.test_browser_action_ambiguous_dispatch_is_persisted_as_uncertain()
    await tool_contract.test_browser_act_normalizes_stale_and_ambiguous_failures()


async def test_profile_lifecycle(tmp_path: Path) -> None:
    await profile_repository.test_profile_repository_scopes_create_get_and_list()
    await profile_repository.test_profile_repository_rejects_duplicate_and_stale_writes()
    await profile_repository.test_profile_repository_binds_only_the_reserved_generation()
    await profile_repository.test_profile_repository_requires_revocation_before_idempotent_delete()
    await encrypted_store.test_encrypted_store_round_trips_across_restart_without_plaintext(
        tmp_path / "encrypted-roundtrip"
    )
    await encrypted_store.test_encrypted_store_revocation_fences_load_and_survives_restart(
        tmp_path / "encrypted-revocation"
    )
    await encrypted_store.test_encrypted_store_delete_is_scoped_durable_and_idempotent(
        tmp_path / "encrypted-delete"
    )
    await encrypted_store.test_encrypted_store_rotation_is_restartable_and_drops_old_key_dependency(
        tmp_path / "encrypted-rotation"
    )
    await lifecycle_service.test_lifecycle_service_provision_is_durable_and_scope_idempotent(
        tmp_path / "lifecycle-provision"
    )
    await lifecycle_service.test_lifecycle_service_revoke_delete_and_rotation_are_restart_safe(
        tmp_path / "lifecycle-revoke"
    )
    await session_service.test_hosted_session_lease_is_scoped_exclusive_and_seals_server_side(
        tmp_path / "session-lease"
    )
    await session_service.test_revocation_fences_and_closes_live_lease(
        tmp_path / "session-revocation"
    )
    await hosted_lease_contract()
    persistence_schema.test_browser_profiles_schema_contains_metadata_only()
    toolchain_contract.test_production_environment_preserves_process_boundaries()
    toolchain_contract.test_production_compose_preserves_browser_profile_isolation()
    toolchain_contract.test_browser_profile_dockerfile_preserves_process_isolation()
    toolchain_contract.test_systemd_units_preserve_role_boundaries()
    toolchain_contract.test_release_script_preserves_release_boundaries()
    toolchain_contract.test_nginx_configuration_preserves_public_process_boundaries()


async def test_authentication_boundary(tmp_path: Path) -> None:
    await auth_repository.test_in_memory_browser_authentication_repository_contract()
    auth_repository.test_authentication_record_cannot_contain_launch_or_browser_material()
    service_configuration.test_profile_service_loads_only_private_file_mounted_material(
        tmp_path / "service-config"
    )
    for index, status in enumerate(
        (
            BrowserAuthenticationStatus.READY,
            BrowserAuthenticationStatus.NEEDS_USER,
            BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
        )
    ):
        ceremony_contract = (
            session_service.assert_authentication_ceremony_is_direct_single_use_and_runtime_decided
        )
        await ceremony_contract(
            tmp_path / f"ceremony-{index}",
            runtime_status=status,
        )
    await session_service.test_authentication_scope_mismatch_and_caller_asserted_success_are_absent(
        tmp_path / "ceremony-scope"
    )
    await (
        service_http.test_authentication_surface_binds_fragment_capability_before_interaction_body(
            tmp_path / "ceremony-http"
        )
    )
    # ADR-0128: a device ceremony's session reaches only the isolated service,
    # once, through its capability; the service filters, verifies and decides.
    await session_service.assert_device_handoff_is_single_use_scope_filtered_and_service_decided(
        tmp_path / "device-handoff"
    )
    await service_http.test_device_handoff_authenticates_before_buffering_and_logs_nothing(
        tmp_path / "device-http"
    )
    await public_api_contract.test_device_ceremony_begin_passes_mode_and_returns_launch_once()
    await (
        management_contract.test_profile_authentication_is_durable_secret_free_and_runtime_decided()
    )
    await (
        public_api_contract.test_public_profile_authentication_and_grant_creation_are_secret_free()
    )
    await hosted_runtime.test_hosted_runtime_rejects_oversized_interactive_frames()
    persistence_schema.test_browser_authentications_schema_is_secret_free()


async def test_standing_grant(tmp_path: Path) -> None:
    await grant_repository.test_in_memory_browser_grant_repository_contract()
    grant_repository.test_browser_grant_refuses_ambient_or_unbounded_authority()
    await management_contract.test_standing_grant_creation_pins_profile_agent_policy_and_approver()
    await grant_authority.test_exact_routine_action_can_replace_one_approval()
    await grant_authority.test_expired_revoked_mismatched_or_excluded_grant_fails_closed()
    await grant_authority.test_policy_allow_or_deny_is_never_overridden()
    await grant_authority.test_standing_grant_sends_a_routine_constraint()
    await grant_authority.test_hidden_label_source_is_not_routine_for_a_standing_grant()
    await exact_grant_pipeline()
    await revoked_grant_pipeline()
    persistence_schema.test_browser_grants_schema_contains_exact_authority_without_material()
    await _task_grants(tmp_path)


async def _task_grants(tmp_path: Path) -> None:
    """ADR-0129: owner-scoped, session-bound, capped, prefix-confined, and
    rechecked against the live page."""

    # The scope, the grant's shape and its closed coverage rules.
    task_grant_domain.test_constraint_fields_must_match_the_grant_kind()
    task_grant_domain.test_scope_setting_refuses_multi_segment_and_duplicate_entries()
    task_grant_domain.test_scope_contains_only_its_origin_and_prefix()
    task_grant_domain.test_grant_window_caps_and_end_pairing_are_validated()
    classification.test_scope_setting_refuses_a_sensitive_segment()
    classification.test_the_old_hard_exclusions_are_named_consequences()
    classification.test_every_label_source_is_classified_separately()
    classification.test_task_grant_coverage_names_the_first_failing_rule()
    classification.test_dispatch_constraints_check_expiry_and_origins_before_coverage()
    # Created only from an approval card that offered it, inside a configured scope.
    await approval_view.test_the_offer_copies_the_configured_scope()
    for case in approval_view.OFFERLESS_CASES:
        await approval_view.test_each_offer_rule_withholds_the_offer(case)
    await task_grant_api.test_approve_for_task_approves_once_and_creates_the_grant()
    await task_grant_api.test_task_grant_routes_exist_only_with_the_flag()
    # Session-bound and capped, with uses that cannot race past the caps.
    await task_grant_repository.test_one_active_grant_per_session()
    await task_grant_repository.test_concurrent_uses_stop_at_the_action_cap()
    await task_grant_repository.test_typed_characters_never_pass_the_grant_budget()
    await task_grant_repository.test_no_use_after_expiry_revocation_or_in_another_session()
    await (
        task_grant_authority.test_a_covered_click_is_allowed_with_the_task_constraint_and_its_view()
    )
    await task_grant_authority.test_an_ineligible_run_is_refused(
        {"parent_run_id": UUID(int=0xC1)}, task_grant_authority.BROWSER_TURN, None
    )
    await task_grant_authority.test_a_changed_pin_ends_the_grant(
        {"generation": 4}, "agent-v1", {}, "profile_changed"
    )
    await task_grant_authority.test_the_composite_asks_the_standing_grant_first_and_keeps_the_specific_denial()  # noqa: E501
    await task_grant_sweep.test_an_expired_task_grant_is_ended_once_with_an_event()
    persistence_schema.test_browser_task_grants_schema_holds_scope_pins_and_counters_only()
    # The isolated service rechecks every grant-authorized act before dispatch.
    await dispatch_constraint.test_expired_constraint_is_refused_before_the_runtime_acts(
        tmp_path / "constraint-expired"
    )
    await dispatch_constraint.test_refusal_leaves_the_sequence_unchanged(
        tmp_path / "constraint-sequence"
    )
    await dispatch_constraint.test_constraint_on_a_foreign_lease_or_wrong_credential_is_refused_and_dispatches_nothing(  # noqa: E501
        tmp_path / "constraint-foreign"
    )
    # The whole pipeline, from the approval card to the live-page recheck.
    pipeline = tmp_path / "pipeline"
    await task_grant_pipeline.assert_allowed_task_authorizes_the_next_acts(pipeline / "allowed")
    await task_grant_pipeline.assert_the_two_hundred_and_first_act_asks(pipeline / "cap")
    await task_grant_pipeline.assert_text_past_the_grant_budget_asks(pipeline / "text")
    for ending in ("revoked", "expired"):
        await task_grant_pipeline.assert_a_revoked_or_expired_grant_asks(pipeline / ending, ending)
    await task_grant_pipeline.assert_another_or_scheduled_session_asks(pipeline / "sessions")
    await task_grant_pipeline.assert_another_tool_in_the_turn_asks(pipeline / "turn")
    await task_grant_pipeline.assert_a_destructive_button_asks(pipeline / "destructive")
    await task_grant_pipeline.assert_a_sign_in_ends_the_grant(pipeline / "sign-in")
    await task_grant_pipeline.assert_a_credential_shaped_type_is_denied_not_granted(
        pipeline / "credential"
    )
    await task_grant_pipeline.assert_a_standing_grant_answers_first(pipeline / "standing")
    await task_grant_pipeline.assert_a_runtime_refusal_keeps_the_lease_and_forces_an_observe(
        pipeline / "refusal"
    )
