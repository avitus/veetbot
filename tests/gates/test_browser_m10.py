"""Milestone 10 hard gates for authenticated browser automation."""

from pathlib import Path

from agent_core.domain.browser import BrowserAuthenticationStatus
from tests.contract import test_browser_authentication_repository_contract as auth_repository
from tests.contract import test_browser_grant_repository_contract as grant_repository
from tests.contract import test_browser_profile_repository_contract as profile_repository
from tests.contract import test_browser_provider_contract as provider_contract
from tests.contract import test_browser_public_api_contract as public_api_contract
from tests.contract import test_encrypted_profile_store_contract as encrypted_store
from tests.contract import (
    test_hosted_profile_lifecycle_service_contract as lifecycle_service,
)
from tests.contract import test_hosted_profile_session_service_contract as session_service
from tests.contract import test_profile_service_configuration_contract as service_configuration
from tests.contract import test_profile_service_http_contract as service_http
from tests.unit import test_browser_composition as composition_contract
from tests.unit import test_browser_grants as grant_authority
from tests.unit import test_browser_management as management_contract
from tests.unit import test_browser_playwright as playwright_contract
from tests.unit import test_browser_policy as policy_contract
from tests.unit import test_browser_tools as tool_contract
from tests.unit import test_hosted_browser_runtime as hosted_runtime
from tests.unit import test_persistence_schema as persistence_schema
from tests.unit import test_toolchain as toolchain_contract
from tests.unit.test_browser_composition import (
    test_exact_standing_browser_grant_authorizes_without_interactive_approval as exact_grant_pipeline,  # noqa: E501
)
from tests.unit.test_browser_composition import (
    test_revoked_standing_browser_grant_falls_back_to_interactive_approval as revoked_grant_pipeline,  # noqa: E501
)
from tests.unit.test_hosted_browser_provider import (
    test_hosted_provider_acquires_exact_execution_scope_and_rotates_between_runs as hosted_lease_contract,  # noqa: E501
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
    await (
        management_contract.test_profile_authentication_is_durable_secret_free_and_runtime_decided()
    )
    await (
        public_api_contract.test_public_profile_authentication_and_grant_creation_are_secret_free()
    )
    await hosted_runtime.test_hosted_runtime_rejects_oversized_interactive_frames()
    persistence_schema.test_browser_authentications_schema_is_secret_free()


async def test_standing_grant() -> None:
    await grant_repository.test_in_memory_browser_grant_repository_contract()
    grant_repository.test_browser_grant_refuses_ambient_or_unbounded_authority()
    await management_contract.test_standing_grant_creation_pins_profile_agent_policy_and_approver()
    await grant_authority.test_exact_routine_action_can_replace_one_approval()
    await grant_authority.test_expired_revoked_mismatched_or_excluded_grant_fails_closed()
    await grant_authority.test_policy_allow_or_deny_is_never_overridden()
    await exact_grant_pipeline()
    await revoked_grant_pipeline()
    persistence_schema.test_browser_grants_schema_contains_exact_authority_without_material()
