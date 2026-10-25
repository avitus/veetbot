"""Guarded scratch storage for production-shaped synthetic recall experiments."""

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_core.adapters.determinism import FixedClock, elapsed_time
from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import Base
from agent_core.config import DeploymentMode, Settings


def validate_evaluation_database(settings: Settings, *, authorized: bool) -> None:
    url = make_url(settings.database_url)
    if (
        not authorized
        or settings.deployment_mode is DeploymentMode.PRODUCTION
        or url.drivername != "postgresql+asyncpg"
        or re.fullmatch(r"veetbot_m33_eval_[a-z0-9_]{1,32}", url.database or "") is None
    ):
        raise ValueError(
            "PostgreSQL recall evaluation requires explicit scratch authorization and a "
            "development database named veetbot_m33_eval_<name>"
        )


def validate_probe_database(settings: Settings) -> None:
    """Owner recall probes run only against a clone named for the purpose.

    A probe replay writes sessions, runs, traces and usage events, so it never
    runs against production; the clone's name says what it is for, and the
    frozen Milestone 33 scratch databases keep their own name and guard.
    """

    url = make_url(settings.database_url)
    if (
        settings.deployment_mode is DeploymentMode.PRODUCTION
        or url.drivername != "postgresql+asyncpg"
        or re.fullmatch(r"veetbot_recall_probe_[a-z0-9_]{1,32}", url.database or "") is None
    ):
        raise ValueError(
            "Owner recall probes require a development clone named "
            "veetbot_recall_probe_<name>; never production"
        )


def confirmation_databases(
    settings: Settings, *, additional: str | None, authorized: bool
) -> tuple[Settings, ...]:
    """One coordinator, at most two explicitly authorized databases on the same host."""
    validate_evaluation_database(settings, authorized=authorized)
    if additional is None:
        return (settings,)
    url = make_url(settings.database_url)
    if additional == url.database:
        raise ValueError("Confirmation workers require distinct scratch databases")
    second = replace(
        settings, database_url=url.set(database=additional).render_as_string(hide_password=False)
    )
    validate_evaluation_database(second, authorized=authorized)
    return settings, second


@asynccontextmanager
async def evaluation_database(
    settings: Settings, *, storage: Literal["memory", "postgres"], authorized: bool
) -> AsyncIterator[None]:
    if storage == "memory":
        yield
        return
    validate_evaluation_database(settings, authorized=authorized)
    engine = create_engine(settings.database_url)
    primary_error: BaseException | None = None
    try:
        # Hold one session lock for the entire case, not just the reset. A second
        # evaluator must not erase this case while its worker is still running.
        async with engine.connect() as connection:
            locked = await connection.scalar(text("SELECT pg_try_advisory_lock(3300192)"))
            if not locked:
                raise ValueError("Recall scratch database already has an active experiment")
            try:
                table_names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
                await connection.execute(
                    text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE")
                )
                await connection.commit()
                yield
            except BaseException as error:
                primary_error = error
                raise
            finally:
                try:
                    await _release_scratch_lock(connection)
                except asyncio.CancelledError as error:
                    primary_error = error
                    raise
                except Exception:
                    if primary_error is None:
                        raise
    except asyncio.CancelledError as error:
        primary_error = error
        raise
    except Exception as error:
        if primary_error is None or primary_error is error:
            primary_error = error
            raise
        raise primary_error from None
    finally:
        try:
            await engine.dispose()
        except Exception:
            if primary_error is None:
                raise


async def _release_scratch_lock(connection: AsyncConnection) -> None:
    """Attempt unlock independently and discard a connection with failed cleanup."""
    failure: Exception | asyncio.CancelledError | None = None
    try:
        await connection.rollback()
    except (Exception, asyncio.CancelledError) as error:
        failure = error
    try:
        await connection.execute(text("SELECT pg_advisory_unlock(3300192)"))
    except asyncio.CancelledError as error:
        failure = error
    except Exception as error:
        failure = failure or error
    if failure is not None:
        with suppress(Exception):
            await connection.invalidate()
        raise failure


class EvaluationClock(FixedClock):
    """Author history at fixed instants, then measure the worker with elapsed time."""

    def __init__(self, current: datetime) -> None:
        super().__init__(current)
        self._started: float | None = None

    def start_live(self) -> None:
        if self._started is not None:
            raise ValueError("The evaluation clock is already live")
        self._started = elapsed_time()

    def now(self) -> datetime:
        return super().now() + timedelta(
            seconds=0 if self._started is None else elapsed_time() - self._started
        )

    def advance(self, delta: timedelta) -> None:
        if self._started is not None:
            raise ValueError("A live evaluation clock cannot be advanced manually")
        super().advance(delta)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
