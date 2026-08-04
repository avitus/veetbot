from datetime import timedelta

from agent_core.adapters.persistence.memory import InMemoryExportConsentRepository
from agent_core.domain.trajectory import ExportConsent
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT


async def test_export_consent_grant_and_withdraw_are_durable_state() -> None:
    repository = InMemoryExportConsentRepository()
    grant = ExportConsent(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        granted_at=NOW,
    )
    assert (await repository.grant(grant)).active
    assert (
        await repository.grant(grant.model_copy(update={"granted_at": NOW + timedelta(seconds=1)}))
        == grant
    )
    assert (await repository.get_for_update(TENANT, PRINCIPAL_ID)) == grant
    withdrawn = await repository.withdraw(TENANT, PRINCIPAL_ID, NOW + timedelta(seconds=1))
    assert not withdrawn.active
    assert await repository.withdraw(TENANT, PRINCIPAL_ID, NOW + timedelta(seconds=2)) == withdrawn
    assert await repository.get(TENANT, PRINCIPAL_ID) == withdrawn
