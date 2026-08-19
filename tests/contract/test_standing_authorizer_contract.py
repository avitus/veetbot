"""Shared standing-authorization fail-closed contract."""

from tests.unit import test_browser_grants as exact_authority_contract
from tests.unit.test_browser_composition import (
    test_exact_standing_browser_grant_authorizes_without_interactive_approval as exact_pipeline,
)
from tests.unit.test_browser_composition import (
    test_revoked_standing_browser_grant_falls_back_to_interactive_approval as revoked_pipeline,
)


async def test_standing_authorizer_contract() -> None:
    """Exact routine authority can replace approval; mismatch and revocation cannot."""

    await exact_authority_contract.test_exact_routine_action_can_replace_one_approval()
    await exact_authority_contract.test_expired_revoked_mismatched_or_excluded_grant_fails_closed()
    await exact_authority_contract.test_policy_allow_or_deny_is_never_overridden()
    await exact_pipeline()
    await revoked_pipeline()
