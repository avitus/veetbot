"""PostgreSQL lexical recall computes each belief's lexeme vector once.

A scheduled instruction of a few kilobytes forms hundreds of recall terms. When
every per-term match repeated `to_tsvector` over the belief text, one in-turn
recall took nine seconds against 1,058 beliefs and the Friday X follow
recommendation ran out of time before its answer (2026-10-23). The vector is
row data, so its evaluation count must not grow with the number of terms.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from agent_core.adapters.persistence.memory_repositories import PostgresMemoryStore
from agent_core.domain.memory import RecallProfile, RecallQuery

NOW = datetime(2026, 10, 23, 17, 3, 40, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _CapturingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> list[Any]:
        self.statements.append(statement)
        return []

    async def scalars(self, statement: Any) -> Any:
        self.statements.append(statement)

        class _Rows:
            def all(self) -> list[Any]:
                return []

        return _Rows()


def _vector_evaluations(text: str, *, historical: bool) -> int:
    query = RecallQuery(
        tenant_id="veetbot",
        principal_id="owner",
        current_scope="general",
        text=text,
        profile=RecallProfile.TASK,
        candidate_selection="relevance",
        budget_tokens=2_000,
        max_items=20,
        min_score=0.12,
        known_at=NOW if historical else None,
    )
    session = _CapturingSession()
    asyncio.run(PostgresMemoryStore(session, _Clock()).query(query))  # type: ignore[arg-type]
    (statement,) = [item for item in session.statements if isinstance(item, Select)]
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    compiled = str(statement.compile(dialect=dialect))
    return compiled.count("to_tsvector(")


@pytest.mark.parametrize("historical", [False, True])
def test_lexeme_vector_evaluations_do_not_grow_with_query_terms(historical: bool) -> None:
    short = _vector_evaluations("agents tooling", historical=historical)
    long = _vector_evaluations(
        " ".join(f"term{index}" for index in range(200)), historical=historical
    )

    assert long == short
    # The outer selection and its two candidate arms each read the vector once.
    assert short <= 3
