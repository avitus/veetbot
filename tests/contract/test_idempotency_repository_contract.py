from datetime import timedelta

from agent_core.adapters.persistence.memory import InMemoryIdempotencyRepository
from agent_core.domain.persistence import IdempotencyRecord
from tests.contract.support import NOW, PRINCIPAL_ID, RUN_ID, TENANT


async def test_idempotency_repository_returns_the_original_run() -> None:
    repository = InMemoryIdempotencyRepository()
    record = IdempotencyRecord(
        key="request-1",
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        request_hash="sha256:one",
        run_id=RUN_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    assert await repository.create(record) == record
    assert await repository.create(record) == record
    assert await repository.get(record.key, TENANT, PRINCIPAL_ID) == record
