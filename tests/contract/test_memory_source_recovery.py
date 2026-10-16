import pytest

from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import formation_stack
from tests.contract.source_recovery_cases import (  # noqa: F401
    test_attribution_change_fences_source_before_atom_formation,
    test_change_to_one_fact_fences_shared_source_for_every_other_fact,
    test_deleting_fact_erases_earlier_source_quotes_under_another_fact,
    test_erasing_all_attribution_fences_sources_without_formed_atoms,
    test_new_private_owner_source_is_bounded_and_identified,
    test_real_recovered_quote_is_restricted_and_erased_with_later_fact,
    test_rolled_back_correction_does_not_fence_committed_source,
    test_source_access_requires_private_view_and_restricted_clearance,
    test_source_fence_cannot_modify_another_owners_message,
    test_source_withholds_closed_sibling_even_without_a_change_write,
    test_unknown_or_hazardous_sources_never_receive_admission,
)


@pytest.fixture
async def source_factory() -> UnitOfWorkFactory:
    _, factory, _, _ = await formation_stack()
    return factory
