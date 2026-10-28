"""Owner kinship repair (ADR-0259) against PostgreSQL."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.bootstrap import build
from agent_core.domain.events import NewEvent
from tests.contract.people_kinship_cases import kinship_repair_case
from tests.integration.test_memory_postgres_m9 import _settings


@pytest.mark.parametrize(
    ("message", "restatements", "ended"),
    [
        ("Erin is my wife. We married in 2005.", (), False),
        ("Erin is my wife. We married in 2005.", ("Andy confirmed that Erin is his wife.",), False),
        ("Erin is my wife; we divorced last year.", (), True),
    ],
)
async def test_kinship_repair_records_the_owners_stated_tie_postgres(
    tmp_path: Path, message: str, restatements: tuple[str, ...], ended: bool
) -> None:
    async with build(
        settings=replace(_settings(tmp_path), people_enabled=True), storage="postgres"
    ) as app:
        session_id = await app.sessions.create()
        async with app.uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": message},
                )
            )
        await kinship_repair_case(
            app.uow_factory,
            app.clock,
            app.principal,
            session_id,
            event.sequence,
            restatements=restatements,
            ended=ended,
        )
