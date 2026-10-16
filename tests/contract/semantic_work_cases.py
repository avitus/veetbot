"""Shared durable indexing, lease fencing and accounting scenarios."""

from datetime import timedelta
from decimal import Decimal

import pytest

from agent_core.domain.errors import ConflictError
from agent_core.domain.semantic_memory import SemanticBounds
from agent_core.domain.semantic_work import EmbeddingConfig
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.semantic_memory_cases import SPACE
from tests.contract.support import NOW, principal

CONFIG = EmbeddingConfig(
    space=SPACE,
    usd_per_million_tokens=Decimal("1"),
    pricing_revision="test@1",
    egress_revision="test@1",
    residency_provider="fixture",
    request_usd=Decimal(".1"),
    daily_usd=Decimal(".15"),
)
BOUNDS = SemanticBounds(creation=40, change=40)


async def lease_and_cursor(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None, "eligible semantic inventory must acquire a durable lease"
        assert await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW) is None
        await uow.semantic_work.finish(principal(), job.lease_token, 16, 16, True, NOW)
    async with factory() as uow:
        resumed = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert resumed is not None and resumed.full_cursor == resumed.change_cursor == 16
        assert resumed.lease_token != job.lease_token
        with pytest.raises(ConflictError):
            await uow.semantic_work.current(principal(), job.lease_token, NOW)
        with pytest.raises(ConflictError):
            await uow.semantic_work.current(
                principal().model_copy(update={"principal_id": "other"}), resumed.lease_token, NOW
            )


async def durable_budget(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None
        spend = await uow.semantic_work.reserve(
            principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".1"), 16, 16, NOW
        )
        assert spend is not None
        assert (
            await uow.semantic_work.reserve(
                principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".1"), 16, 16, NOW
            )
            is None
        ), "a reservation cannot authorize a duplicate send"
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), NOW.date()) == Decimal(".1")
        await uow.semantic_work.settle(principal(), job.lease_token, spend.id, None, NOW)
        await uow.semantic_work.finish(principal(), job.lease_token, 16, 16, False, NOW)
    async with factory() as uow:
        retry = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert retry is not None and retry.full_cursor == 0
        assert (
            await uow.semantic_work.reserve(
                principal(), retry.lease_token, CONFIG, "b" * 64, Decimal(".1"), 16, 16, NOW
            )
            is None
        )


async def settlement_and_rollback(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None
    with pytest.raises(RuntimeError):
        async with factory() as uow:
            assert (
                await uow.semantic_work.reserve(
                    principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".1"), 16, 16, NOW
                )
                is not None
            )
            raise RuntimeError("rollback")
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), NOW.date()) == 0
        spend = await uow.semantic_work.reserve(
            principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".1"), 16, 16, NOW
        )
        assert spend is not None
        receipt = await uow.semantic_work.settle(
            principal(), job.lease_token, spend.id, Decimal(".02"), NOW
        )
        assert receipt.charge == Decimal(".02")
        assert (
            await uow.semantic_work.settle(
                principal(), job.lease_token, spend.id, Decimal(".02"), NOW
            )
            == receipt
        )
        with pytest.raises(ConflictError):
            await uow.semantic_work.settle(
                principal(), job.lease_token, spend.id, Decimal("0"), NOW
            )
        assert await uow.semantic_work.spent(principal(), NOW.date()) == Decimal(".02")


async def recovery_and_finite_retries(factory: UnitOfWorkFactory) -> None:
    for attempt in range(3):
        now = NOW + timedelta(minutes=3 * attempt)
        async with factory() as uow:
            job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, now)
            assert job is not None and job.full_cursor == 0
            assert (
                await uow.semantic_work.reserve(
                    principal(),
                    job.lease_token,
                    CONFIG,
                    str(attempt) * 64,
                    Decimal(".01"),
                    16,
                    16,
                    now,
                )
                is not None
            )
    assert job is not None
    async with factory() as uow:
        resumed = await uow.semantic_work.claim(
            principal(), CONFIG, BOUNDS, NOW + timedelta(minutes=10)
        )
        assert resumed is not None and resumed.full_cursor == 16 and resumed.failed_pages == 1
        assert await uow.semantic_work.spent(principal(), NOW.date()) == Decimal(".03")
        with pytest.raises(ConflictError):
            await uow.semantic_work.finish(
                principal(), job.lease_token, 16, 16, True, NOW + timedelta(minutes=10)
            )


async def config_and_day_do_not_reset_spend(factory: UnitOfWorkFactory) -> None:
    now = NOW.replace(hour=23, minute=59, second=45)
    async with factory() as uow:
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, now)
        assert job is not None
        spend = await uow.semantic_work.reserve(
            principal(),
            job.lease_token,
            CONFIG,
            "a" * 64,
            Decimal(".1"),
            16,
            16,
            now + timedelta(seconds=20),
        )
        assert spend is not None and spend.day == now.date()
        await uow.semantic_work.finish(
            principal(), job.lease_token, 16, 16, True, now + timedelta(seconds=20)
        )
        assert await uow.semantic_work.spent(principal(), now.date()) == Decimal(".1")
    async with factory() as uow:
        changed = CONFIG.model_copy(update={"pricing_revision": "test@2"})
        job = await uow.semantic_work.claim(
            principal(), changed, BOUNDS, now + timedelta(seconds=25)
        )
        assert job is not None
        # A fresh day's allowance is available, but the old charge stays in its original bucket.
        assert await uow.semantic_work.spent(principal(), job.day) == 0
        assert await uow.semantic_work.spent(principal(), now.date()) == Decimal(".1")


SCENARIOS = [
    lease_and_cursor,
    durable_budget,
    settlement_and_rollback,
    recovery_and_finite_retries,
    config_and_day_do_not_reset_spend,
]


async def money_quantum_cannot_disappear(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None
        spend = await uow.semantic_work.reserve(
            principal(), job.lease_token, CONFIG, "a" * 64, Decimal("0.000000001"), 16, 16, NOW
        )
        assert spend is not None and spend.maximum_usd == Decimal("0.00000001")
        receipt = await uow.semantic_work.settle(
            principal(), job.lease_token, spend.id, Decimal("0.000000001"), NOW
        )
        assert receipt.charge == Decimal("0.00000001")


SCENARIOS.append(money_quantum_cannot_disappear)


async def generations_keep_change_watermark(factory: UnitOfWorkFactory) -> None:
    for cursor in (16, 32, 40):
        async with factory() as uow:
            job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
            assert job is not None
            await uow.semantic_work.finish(principal(), job.lease_token, cursor, cursor, True, NOW)
    async with factory() as uow:
        job = await uow.semantic_work.claim(
            principal(), CONFIG, SemanticBounds(creation=42, change=45), NOW
        )
        assert job is not None and job.full_cursor == 0 and job.change_cursor == 40


SCENARIOS.append(generations_keep_change_watermark)


async def legacy_configuration_keeps_reservations_and_cursors(factory: UnitOfWorkFactory) -> None:
    class LegacyConfig(EmbeddingConfig):
        @property
        def key(self) -> str:
            return "92ed35aa3e222cbda9f3e68f342e70c75e65161a5255b698326ffa654fc91764"

    legacy = LegacyConfig.model_validate(CONFIG.model_dump())
    async with factory() as uow:
        job = await uow.semantic_work.claim(principal(), legacy, BOUNDS, NOW)
        assert job is not None
    async with factory() as uow:
        spend = await uow.semantic_work.reserve(
            principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".01"), 16, 16, NOW
        )
        assert spend is not None
        await uow.semantic_work.settle(principal(), job.lease_token, spend.id, Decimal(".005"), NOW)
        await uow.semantic_work.finish(principal(), job.lease_token, 16, 16, True, NOW)
    async with factory() as uow:
        resumed = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert resumed is not None
        assert resumed.generation == job.generation
        assert resumed.full_cursor == resumed.change_cursor == 16
        assert await uow.semantic_work.spent(principal(), NOW.date()) == Decimal(".005")


SCENARIOS.append(legacy_configuration_keeps_reservations_and_cursors)


async def changing_configuration_keeps_daily_charge(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None
        assert await uow.semantic_work.reserve(
            principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".1"), 16, 16, NOW
        )
        await uow.semantic_work.finish(principal(), job.lease_token, 16, 16, False, NOW)
    async with factory() as uow:
        changed = CONFIG.model_copy(update={"pricing_revision": "new"})
        job = await uow.semantic_work.claim(principal(), changed, BOUNDS, NOW)
        assert job is not None
        assert (
            await uow.semantic_work.reserve(
                principal(), job.lease_token, changed, "b" * 64, Decimal(".1"), 16, 16, NOW
            )
            is None
        )
        assert await uow.semantic_work.spent(principal(), NOW.date()) == Decimal(".1")


SCENARIOS.append(changing_configuration_keeps_daily_charge)
