from dataclasses import replace

import pytest

from agent_core.config import DeploymentMode
from agent_core.evals.general_recall_database import (
    confirmation_databases,
    validate_evaluation_database,
)
from tests.gates.test_runtime_tool_budget import _settings


@pytest.mark.parametrize(
    "change", ["unconfirmed", "ordinary_database", "production", "other_driver"]
)
def test_postgres_evaluation_requires_an_explicit_named_scratch_database(change: str) -> None:
    settings = replace(
        _settings(), database_url="postgresql+asyncpg://localhost/veetbot_m33_eval_unit"
    )
    if change == "ordinary_database":
        settings = replace(settings, database_url="postgresql+asyncpg://localhost/agent")
    elif change == "production":
        settings = replace(settings, deployment_mode=DeploymentMode.PRODUCTION)
    elif change == "other_driver":
        settings = replace(settings, database_url="sqlite:///veetbot_m33_eval_unit")
    with pytest.raises(ValueError, match="scratch"):
        validate_evaluation_database(settings, authorized=change != "unconfirmed")


def test_explicit_postgres_scratch_database_is_accepted() -> None:
    validate_evaluation_database(
        replace(_settings(), database_url="postgresql+asyncpg://localhost/veetbot_m33_eval_unit"),
        authorized=True,
    )


@pytest.mark.parametrize("additional", ["veetbot_m33_eval_unit", "agent", "invalid/path"])
def test_parallel_database_cannot_share_storage_or_escape_the_scratch_guard(
    additional: str,
) -> None:
    settings = replace(
        _settings(), database_url="postgresql+asyncpg://localhost/veetbot_m33_eval_unit"
    )
    with pytest.raises(ValueError):
        confirmation_databases(settings, additional=additional, authorized=True)


def test_parallel_database_preserves_configuration_and_still_requires_authorization() -> None:
    settings = replace(
        _settings(), database_url="postgresql+asyncpg://localhost/veetbot_m33_eval_unit"
    )
    with pytest.raises(ValueError, match="authorization"):
        confirmation_databases(settings, additional="veetbot_m33_eval_second", authorized=False)
    first, second = confirmation_databases(
        settings, additional="veetbot_m33_eval_second", authorized=True
    )
    assert first == settings
    assert second == replace(
        settings, database_url="postgresql+asyncpg://localhost/veetbot_m33_eval_second"
    )


async def test_worker_clock_uses_elapsed_time_without_advancing_during_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from agent_core.evals import general_recall_database as database
    from tests.unit.test_general_recall_corpus import AT

    ticks = [100.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(database, "elapsed_time", lambda: ticks[0])
    monkeypatch.setattr("agent_core.evals.general_recall_database.asyncio.sleep", sleep)
    clock = database.EvaluationClock(AT)
    clock.advance(timedelta(days=1))
    clock.start_live()
    ticks[0] += 12
    assert clock.now() == AT + timedelta(days=1, seconds=12)
    await clock.sleep(30)
    assert slept == [30] and clock.now() == AT + timedelta(days=1, seconds=12)
    with pytest.raises(ValueError):
        clock.advance(timedelta(days=1))
    with pytest.raises(ValueError):
        clock.start_live()
