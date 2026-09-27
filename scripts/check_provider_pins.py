"""Refuse a release that cannot resume an active run's exact provider pin."""

from __future__ import annotations

import asyncio
import os
import sys
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_core.adapters.determinism import SystemClock
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.config import PACKAGE_ROOT, load_settings
from agent_core.domain.errors import ProviderPinUnavailableError
from agent_core.domain.messages import ProviderPin
from agent_core.model.registry import ProviderRegistry, StaticModelRouter


async def incompatible_provider_pins(
    router: StaticModelRouter, rows: list[tuple[UUID, object]]
) -> list[UUID]:
    failures: list[UUID] = []
    for run_id, value in rows:
        try:
            pin = ProviderPin.model_validate(value)
            if pin.run_id != run_id:
                failures.append(run_id)
                continue
            await router.resolve_pinned(pin)
        except (ValidationError, ProviderPinUnavailableError):
            failures.append(run_id)
    return failures


async def inspect_provider_pins(database_url: str) -> list[tuple[UUID, object]]:
    engine = create_async_engine(database_url, connect_args={"timeout": 10})
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            # A deploy must see every tenant or fail, never silently accept a
            # tenant-filtered inventory. This uses the migration role.
            await connection.execute(text("SET LOCAL row_security = off"))
            rows = await connection.execute(
                text(
                    "SELECT id, provider_pin FROM runs "
                    "WHERE status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') "
                    "AND provider_pin IS NOT NULL ORDER BY id"
                )
            )
            return [(row.id, row.provider_pin) for row in rows]
    finally:
        await engine.dispose()


async def _run() -> int:
    try:
        settings = load_settings(os.environ)
        router = StaticModelRouter(
            ProviderRegistry.load(
                PACKAGE_ROOT / "models",
                adapters=ADAPTER_DEFINITIONS,
                overlay_root=settings.config_dir,
            ),
            SystemClock(),
        )
        async with asyncio.timeout(30):
            failures = await incompatible_provider_pins(
                router, await inspect_provider_pins(settings.database_url)
            )
    except Exception as exc:
        # Configuration and database exceptions can contain credentials.
        print(
            f"FAIL: provider pin preflight could not complete ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    if failures:
        print(
            "FAIL: active runs require an unavailable provider registry; "
            "finish or cancel these runs on the current release before deploying: "
            + ", ".join(str(run_id) for run_id in failures),
            file=sys.stderr,
        )
        return 1
    print("OK: every active provider pin is available in the staged registry")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
