"""The release inventory reads durable runs across tenants and pause states."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.adapters.persistence.database import create_engine
from agent_core.bootstrap import build
from agent_core.config import PACKAGE_ROOT
from agent_core.domain.runs import RunStatus
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from scripts.check_provider_pins import incompatible_provider_pins, inspect_provider_pins
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("status", list(RunStatus))
@pytest.mark.parametrize("pinned", [False, True])
async def test_deployment_inventory_covers_every_active_state_and_ignores_terminal_runs(
    status: RunStatus, pinned: bool
) -> None:
    settings = database_settings()
    router = StaticModelRouter(
        ProviderRegistry.load(PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS),
        FixedClock(NOW),
    )
    async with build(settings=settings, storage="postgres") as app:
        run_id = await app.runs.submit("synthetic pending run")
        if pinned:
            pin = router.pin(run_id, await router.resolve("astra", tenant_id="local"))
            pin.registry_version = "previous-release"
            async with app.uow_factory() as uow:
                await uow.runs.set_provider_pin(run_id, pin)
    engine = create_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            # Move it outside the configured tenant: deployment must not use
            # the normal application-scoped repository to inventory pins.
            await connection.execute(
                text(
                    "UPDATE runs SET status = :status, tenant_id = 'another-tenant' WHERE id = :id"
                ),
                {"status": status.value, "id": run_id},
            )
        rows = await inspect_provider_pins(settings.database_url)
        expected = pinned and status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
        assert [row[0] for row in rows] == ([run_id] if expected else [])
        assert await incompatible_provider_pins(router, rows) == ([run_id] if expected else [])
    finally:
        await engine.dispose()


async def test_tenant_filtered_database_role_cannot_report_false_compatibility() -> None:
    settings = database_settings()
    engine = create_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE ROLE pin_preflight_reader LOGIN"))
            await connection.execute(text("GRANT SELECT ON runs TO pin_preflight_reader"))
            # Runs normally use repository scoping. Exercise the guard against
            # a database policy that would otherwise silently hide all rows.
            await connection.execute(text("ALTER TABLE runs ENABLE ROW LEVEL SECURITY"))
        restricted_url = make_url(settings.database_url).set(username="pin_preflight_reader")
        with pytest.raises(DBAPIError, match="row-level security"):
            await inspect_provider_pins(restricted_url.render_as_string(hide_password=False))
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE runs DISABLE ROW LEVEL SECURITY"))
            await connection.execute(text("REVOKE SELECT ON runs FROM pin_preflight_reader"))
            await connection.execute(text("DROP ROLE pin_preflight_reader"))
        await engine.dispose()
