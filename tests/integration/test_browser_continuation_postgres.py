"""Durable authentication continuation uses the actual PostgreSQL checkpoint/queue."""

from dataclasses import replace

import pytest

from agent_core.bootstrap import build
from tests.contract.test_run_repository_contract import (
    waiting_run_limits_contract,
    waiting_runs_contract,
)
from tests.integration.m2_support import database_settings
from tests.unit import test_browser_offers, test_browser_recovery
from tests.unit.test_browser_composition import session_bound_hosted_settings


async def test_postgres_waiting_browser_runs_are_scoped_and_paginated() -> None:
    async with build(settings=database_settings(), storage="postgres") as app:
        await waiting_runs_contract(app.uow_factory)


async def test_postgres_a_waiting_run_takes_new_limits_and_no_other_run_does() -> None:
    async with build(settings=database_settings(), storage="postgres") as app:
        await waiting_run_limits_contract(app.uow_factory)


@pytest.mark.parametrize(
    "change", ["none", "recorded_ready", "cancelled", "revoked", "newer_ceremony"]
)
async def test_postgres_verified_continuation_is_durable_and_fenced(change: str) -> None:
    await test_browser_recovery.test_verified_browser_authentication_resumes_once(
        change, settings=database_settings(), storage="postgres"
    )


async def test_postgres_an_offer_answers_its_waiting_source_run() -> None:
    """ADR-0251 owner decision 3 in one PostgreSQL transaction."""
    await test_browser_offers.test_accepting_while_the_source_run_asks_answers_it_in_that_run(
        settings=replace(
            session_bound_hosted_settings(), database_url=database_settings().database_url
        ),
        storage="postgres",
    )
