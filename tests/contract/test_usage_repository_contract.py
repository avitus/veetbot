from decimal import Decimal
from uuid import UUID

from agent_core.adapters.persistence.memory import InMemoryUsageRepository
from agent_core.domain.messages import CostSource, ModelUsage
from agent_core.domain.persistence import ModelCallRecord
from tests.contract.support import NOW, RUN_ID, SESSION_ID, TENANT, memory_stack


async def test_usage_repository_deduplicates_attempts_and_rolls_up() -> None:
    _clock, _sessions, runs, _events = await memory_stack()
    repository = InMemoryUsageRepository(runs)
    call = ModelCallRecord(
        attempt_id=UUID(int=91),
        run_id=RUN_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT,
        step_number=1,
        attempt_number=1,
        provider="fake",
        model="scripted",
        model_policy="fake",
        registry_version="catalog@1",
        prefix_sha256="abc",
        usage=ModelUsage(input_tokens=3, output_tokens=2),
        cost=Decimal("0"),
        cost_source=CostSource.CONFIG_OVERRIDE,
        started_at=NOW,
        finished_at=NOW,
    )
    await repository.record_attempt(call)
    await repository.record_attempt(call)
    assert (await repository.run_usage(RUN_ID)).model_calls == 1
