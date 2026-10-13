"""Query embedding reservations share durable indexing budgets."""

from decimal import Decimal

from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.semantic_work_cases import BOUNDS, CONFIG
from tests.contract.support import NOW, principal


async def query_shares_index_budget(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        receipt = await uow.semantic_work.reserve_query(
            principal(), CONFIG, "a" * 64, Decimal(".1"), NOW
        )
        assert receipt is not None, "an eligible query must reserve its send before provider egress"
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), NOW.date()) == Decimal(".1")
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None
        assert (
            await uow.semantic_work.reserve(
                principal(), job.lease_token, CONFIG, "b" * 64, Decimal(".1"), 16, 16, NOW
            )
            is None
        ), "indexing cannot spend a second copy of the owner's query budget"


SCENARIOS = [query_shares_index_budget]


async def query_lease_recovery_and_owner_fence(factory: UnitOfWorkFactory) -> None:
    from datetime import timedelta

    import pytest

    from agent_core.domain.errors import ConflictError

    async with factory() as uow:
        receipt = await uow.semantic_work.reserve_query(
            principal(), CONFIG, "a" * 64, Decimal(".04"), NOW
        )
        assert receipt is not None
    async with factory() as uow:
        assert await uow.semantic_work.query_current(principal(), receipt.id, NOW) == receipt
        assert (
            await uow.semantic_work.reserve_query(
                principal(), CONFIG, "b" * 64, Decimal(".04"), NOW
            )
            is None
        )
        other = principal().model_copy(update={"principal_id": "other"})
        with pytest.raises(ConflictError):
            await uow.semantic_work.query_current(other, receipt.id, NOW)
        with pytest.raises(ConflictError):
            await uow.semantic_work.settle_query(other, receipt.id, Decimal("0"), NOW)
    later = NOW + timedelta(seconds=15)
    async with factory() as uow:
        with pytest.raises(ConflictError):
            await uow.semantic_work.settle_query(principal(), receipt.id, Decimal("0"), later)
        assert (
            await uow.semantic_work.reserve_query(
                principal(), CONFIG, "b" * 64, Decimal(".04"), later
            )
            is not None
        )
        assert await uow.semantic_work.spent(principal(), NOW.date()) == Decimal(".08")


async def query_settlement_rollback_and_day(factory: UnitOfWorkFactory) -> None:
    from datetime import timedelta

    import pytest

    from agent_core.domain.errors import ConflictError

    now = NOW.replace(hour=23, minute=59, second=55)
    with pytest.raises(RuntimeError):
        async with factory() as uow:
            assert (
                await uow.semantic_work.reserve_query(
                    principal(), CONFIG, "a" * 64, Decimal(".1"), now
                )
                is not None
            )
            raise RuntimeError("rollback")
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), now.date()) == 0
        receipt = await uow.semantic_work.reserve_query(
            principal(), CONFIG, "a" * 64, Decimal(".1"), now
        )
        assert receipt is not None
    later = now + timedelta(seconds=7)
    async with factory() as uow:
        for actual in (Decimal("NaN"), Decimal("-.1"), Decimal(".11")):
            with pytest.raises(ConflictError):
                await uow.semantic_work.settle_query(principal(), receipt.id, actual, later)
        settled = await uow.semantic_work.settle_query(
            principal(), receipt.id, Decimal(".000000001"), later
        )
        assert settled.charge == Decimal(".00000001")
        assert (
            await uow.semantic_work.settle_query(
                principal(), receipt.id, Decimal(".000000001"), later
            )
            == settled
        )
        with pytest.raises(ConflictError):
            await uow.semantic_work.query_current(principal(), receipt.id, later)
        with pytest.raises(ConflictError):
            await uow.semantic_work.settle_query(principal(), receipt.id, None, later)
        assert await uow.semantic_work.spent(principal(), now.date()) == settled.charge
        assert await uow.semantic_work.spent(principal(), later.date()) == 0


async def index_blocks_query_budget_and_cannot_be_settled_as_query(
    factory: UnitOfWorkFactory,
) -> None:
    import pytest

    from agent_core.domain.errors import ConflictError

    async with factory() as uow:
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None
        receipt = await uow.semantic_work.reserve(
            principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".1"), 16, 16, NOW
        )
        assert receipt is not None
        with pytest.raises(ConflictError):
            await uow.semantic_work.settle_query(principal(), receipt.id, Decimal("0"), NOW)
    async with factory() as uow:
        changed = CONFIG.model_copy(update={"pricing_revision": "changed"})
        assert (
            await uow.semantic_work.reserve_query(
                principal(), changed, "b" * 64, Decimal(".1"), NOW
            )
            is None
        )
        assert await uow.semantic_work.current(principal(), job.lease_token, NOW) is not None
        for maximum in (Decimal("NaN"), Decimal("0"), Decimal("-.1"), Decimal(".11")):
            assert (
                await uow.semantic_work.reserve_query(principal(), CONFIG, "b" * 64, maximum, NOW)
                is None
            )


SCENARIOS.extend(
    [
        query_lease_recovery_and_owner_fence,
        query_settlement_rollback_and_day,
        index_blocks_query_budget_and_cannot_be_settled_as_query,
    ]
)


async def query_service_uses_committed_admission(factory: UnitOfWorkFactory) -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.domain.memory import Sensitivity
    from agent_core.memory.semantic_queries import SemanticQueryContext, SemanticQueryPreparation
    from tests.contract.memory_fixtures import recall_query
    from tests.contract.semantic_indexing_cases import INDEX_CONFIG, Provider

    provider = Provider(factory)
    context = SemanticQueryContext(
        policy_version="policy@1",
        retrieval_policy="retrieval@1",
        surface_id="owner-chat",
        task_sha256="a" * 64,
        sensitivity_floor=Sensitivity.INTERNAL,
    )
    lookup = SemanticQueryPreparation(
        factory,
        FixedClock(NOW),
        principal(),
        provider,
        INDEX_CONFIG,
        admitted=lambda: True,
        context=lambda: context,
    )
    query = recall_query(text="A quiet place to work").model_copy(
        update={"candidate_selection": "relevance"}
    )
    assert lookup.lookup(query) is None
    assert (await lookup.prepare([query])).prepared == 1
    assert lookup.lookup(query) is not None
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), NOW.date()) == INDEX_CONFIG.price(2)


SCENARIOS.append(query_service_uses_committed_admission)
