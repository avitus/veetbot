"""Milestone 17 memory read API against a real PostgreSQL store.

The gates in `tests/gates/test_memory_read_api_m17.py` observe the read
surface over the in-memory adapter, and the browse contract suite fixes the
behavior there. This file adds a real-composition HTTP browse journey plus
the three things only a live store can answer: full-text search through
`to_tsvector`, keyset pagination over the gapped cluster-wide sequence that
supplies real store positions, and the ceiling predicate over all four
sensitivity values. Cross-adapter browse parity lives beside the rest of it in
`tests/integration/test_memory_postgres_m9.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from agent_core.api import create_app
from agent_core.application.public_services import PublicMemoryService
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    Sensitivity,
)
from agent_core.domain.views import MemoryView, Page
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.integration.m2_support import database_settings

SENSITIVITIES = (
    Sensitivity.PUBLIC,
    Sensitivity.INTERNAL,
    Sensitivity.SENSITIVE,
    Sensitivity.RESTRICTED,
)


def _settings(tmp_path: Path) -> Settings:
    return replace(
        database_settings(),
        artifact_root=tmp_path / "memory-read-artifacts",
        memory_api_enabled=True,
    )


def _reader(composition: Composition) -> Principal:
    return Principal(
        tenant_id=composition.principal.tenant_id,
        principal_id=composition.principal.principal_id,
        roles={"user"},
        scopes=set(PLATFORM_SCOPES) | {"memory.read"},
    )


def _service(composition: Composition) -> PublicMemoryService:
    return PublicMemoryService(uow_factory=composition.uow_factory)


@asynccontextmanager
async def _http_client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    """Exercise the production router and composition over the live store."""

    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _list(
    service: PublicMemoryService,
    principal: Principal,
    *,
    ceiling: Sensitivity = Sensitivity.RESTRICTED,
    statuses: list[MemoryStatus] | None = None,
    belief_types: list[BeliefType] | None = None,
    subject: str | None = None,
    session_id: UUID | None = None,
    text: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page[MemoryView]:
    return await service.list(
        principal,
        ceiling=ceiling,
        statuses=statuses,
        belief_types=belief_types,
        subject=subject,
        session_id=session_id,
        text=text,
        limit=limit,
        cursor=cursor,
    )


async def _write(
    composition: Composition,
    session_id: UUID,
    *,
    subject: str,
    statement: str,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    belief_type: BeliefType = BeliefType.FACT,
) -> MemoryRecord:
    """Insert one belief, drawing its position from the real store sequence."""

    now = composition.clock.now()
    async with composition.uow_factory() as uow:
        record = MemoryRecord.model_validate(
            {
                "id": uuid4(),
                "tenant_id": composition.principal.tenant_id,
                "principal_id": composition.principal.principal_id,
                "scope": "integration",
                "subject": subject,
                "statement": statement,
                "source_session_id": session_id,
                "source_event_ids": [1],
                "confidence": 0.9,
                "sensitivity": sensitivity,
                "valid_from": now,
                "status": MemoryStatus.ACTIVE,
                "belief_type": belief_type,
                "polarity": Polarity.ASSERT,
                "portability": Portability.CONTEXTUAL,
                "origin_scopes": ["integration"],
                "last_reinforced_at": now,
                "formation_run_id": uuid4(),
                "consolidation_policy_version": "formation@1",
                "authority": MemoryAuthority.USER,
                "store_position": await uow.memories.next_position(),
                "created_at": now,
                "updated_at": now,
            }
        )
        return await uow.memories.upsert_belief(record)


async def test_postgres_memory_http_journey_lists_pages_and_opens_detail(tmp_path: Path) -> None:
    """The real composition serves a basic browse journey over PostgreSQL."""

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"m17http{uuid4().hex[:10]}"
        older = await _write(
            composition,
            session_id,
            subject=marker,
            statement=f"{marker} older belief",
        )
        newer = await _write(
            composition,
            session_id,
            subject=marker,
            statement=f"{marker} newer belief",
            sensitivity=Sensitivity.RESTRICTED,
        )

        async with _http_client(composition) as client:
            first = await client.get(
                "/v1/memories",
                params={"ceiling": "restricted", "subject": marker, "limit": 1},
            )
            assert first.status_code == 200, first.text
            assert first.headers["cache-control"] == "private, no-store"
            first_body = first.json()
            assert [item["id"] for item in first_body["items"]] == [str(newer.id)]
            assert first_body["next_cursor"] is not None

            second = await client.get(
                "/v1/memories",
                params={
                    "ceiling": "restricted",
                    "subject": marker,
                    "limit": 1,
                    "cursor": first_body["next_cursor"],
                },
            )
            assert second.status_code == 200, second.text
            second_body = second.json()
            assert [item["id"] for item in second_body["items"]] == [str(older.id)]
            assert second_body["next_cursor"] is None

            detail = await client.get(f"/v1/memories/{newer.id}", params={"ceiling": "restricted"})
            assert detail.status_code == 200, detail.text
            assert detail.headers["cache-control"] == "private, no-store"
            assert detail.json()["statement"] == newer.statement


async def test_postgres_text_search_reaches_the_read_service(tmp_path: Path) -> None:
    """The API's `text` filter is answered by PostgreSQL full-text search.

    `to_tsvector('simple', subject || ' ' || statement)` matched by
    `plainto_tsquery('simple', term)`, one term per whitespace-separated
    word, unioned. The `simple` configuration lowercases and never stems, so
    the observable behavior is case folding, punctuation stripping, a
    hyphenated word carried both whole and in parts, and coverage of the
    subject as well as the statement. Every query is scoped to this test's
    own session, because the development database is shared.
    """

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"m17txt{uuid4().hex[:10]}"
        themed = await _write(
            composition,
            session_id,
            subject=f"dash-{marker}",
            statement="Dashboards use the Emerald themes",
        )
        charging = await _write(
            composition,
            session_id,
            subject=f"wear-{marker}",
            statement="The watch charges overnight",
        )
        cadence = await _write(
            composition,
            session_id,
            subject=f"cadence-{marker}",
            statement="Deployment runs on Fridays.",
        )
        hyphenated = await _write(
            composition,
            session_id,
            subject=f"host-{marker}",
            statement="Backups land on veet-bot storage",
        )
        everything = {themed.id, charging.id, cadence.id, hyphenated.id}

        service = _service(composition)
        principal = _reader(composition)

        async def found(text: str) -> set[UUID]:
            page = await _list(service, principal, session_id=session_id, text=text, limit=200)
            return {item.id for item in page.items}

        # Without a text query the session filter alone carries the corpus.
        untexted = await _list(service, principal, session_id=session_id, limit=200)
        assert {item.id for item in untexted.items} == everything

        # The vector lowercases, so a lowercase term matches "Emerald".
        assert await found("emerald") == {themed.id}
        # ...and it does not stem: `simple` keeps "themes" distinct from "theme".
        assert await found("themes") == {themed.id}
        assert await found("theme") == set()

        # Any-term semantics: two terms union rather than intersect.
        assert await found("emerald overnight") == {themed.id, charging.id}

        # The parser strips the trailing period from "Fridays."
        assert await found("fridays") == {cadence.id}

        # A hyphenated word is carried whole and in parts.
        assert await found("veet-bot") == {hyphenated.id}
        assert await found("veet") == {hyphenated.id}

        # The vector covers the subject, not only the statement.
        assert await found(f"wear-{marker}") == {charging.id}

        # A term nothing carries selects nothing, and ends the page.
        empty = await _list(service, principal, session_id=session_id, text="kryptonite", limit=200)
        assert empty.items == []
        assert empty.next_cursor is None

        # Text composes with the other filters through one SQL predicate.
        composed = await _list(
            service,
            principal,
            session_id=session_id,
            text="emerald overnight",
            subject=f"dash-{marker}",
            limit=200,
        )
        assert {item.id for item in composed.items} == {themed.id}


async def test_postgres_keyset_pagination_walks_every_page(tmp_path: Path) -> None:
    """A cursor walk over real, gapped store positions skips and repeats nothing.

    Store positions come from a cluster-wide, non-transactional sequence, so
    they are gapped and cannot be assumed contiguous. Beliefs are inserted
    between page fetches: one inside the unwalked range and one behind the
    walk. The walk must still see every belief live throughout exactly once,
    and `next_cursor` must be null on the last page and only there.
    """

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"m17page{uuid4().hex[:10]}"
        seeded = [
            await _write(
                composition,
                session_id,
                subject=marker,
                statement=f"{marker} belief number {index}",
            )
            for index in range(7)
        ]
        positions = [record.store_position for record in seeded]
        assert positions == sorted(positions)

        service = _service(composition)
        principal = _reader(composition)

        first = await _list(service, principal, subject=marker, limit=2)
        assert len(first.items) == 2
        assert first.next_cursor is not None

        # An unchanged store answers the same first page identically.
        replayed = await _list(service, principal, subject=marker, limit=2)
        assert replayed == first

        # The sequence only moves forward, so a belief written mid-walk lands
        # ahead of the cursor, in the range the walk has already passed. The
        # keyset predicate must leave it there rather than pulling it into a
        # later page, and must lose nothing that was already in flight.
        ahead = await _write(
            composition,
            session_id,
            subject=marker,
            statement=f"{marker} written mid-walk",
        )
        assert ahead.store_position > max(positions)

        pages = [first]
        walked = list(first.items)
        cursor: str | None = first.next_cursor
        for _ in range(10):
            page = await _list(service, principal, subject=marker, limit=2, cursor=cursor)
            pages.append(page)
            walked.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        else:  # pragma: no cover - the walk must terminate
            raise AssertionError("the keyset walk did not reach a final page")

        assert pages[-1].next_cursor is None
        assert all(page.next_cursor is not None for page in pages[:-1])
        assert all(len(page.items) == 2 for page in pages[:-1])

        walked_ids = [item.id for item in walked]
        assert len(walked_ids) == len(set(walked_ids)), "the walk repeated a belief"
        assert set(walked_ids) == {record.id for record in seeded}
        assert ahead.id not in walked_ids

        # Every page is newest-first, and the order holds across page joins.
        by_id = {record.id: record.store_position for record in seeded}
        walked_positions = [by_id[identifier] for identifier in walked_ids]
        assert walked_positions == sorted(walked_positions, reverse=True)

        # Replaying the penultimate cursor against the unchanged store
        # returns the identical final page.
        assert (
            await _list(service, principal, subject=marker, limit=2, cursor=pages[-2].next_cursor)
            == pages[-1]
        )


async def test_postgres_ceiling_filters_all_four_sensitivities(tmp_path: Path) -> None:
    """The ceiling predicate selects exactly the at-or-below set, at every level.

    One belief per sensitivity value, then a list and a detail read at each of
    the four ceilings: the list carries the at-or-below set and nothing more,
    and a detail read above the ceiling is `not_found`, indistinguishable from
    an identifier that was never written.
    """

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"m17ceil{uuid4().hex[:10]}"
        by_sensitivity = {
            sensitivity: await _write(
                composition,
                session_id,
                subject=marker,
                statement=f"{marker} belief at {sensitivity.value}",
                sensitivity=sensitivity,
            )
            for sensitivity in SENSITIVITIES
        }

        service = _service(composition)
        principal = _reader(composition)
        absent = uuid4()

        for index, ceiling in enumerate(SENSITIVITIES):
            visible = {by_sensitivity[sensitivity].id for sensitivity in SENSITIVITIES[: index + 1]}
            hidden = {by_sensitivity[sensitivity].id for sensitivity in SENSITIVITIES[index + 1 :]}

            page = await _list(service, principal, ceiling=ceiling, subject=marker, limit=200)
            assert {item.id for item in page.items} == visible, ceiling
            assert all(SENSITIVITIES.index(item.sensitivity) <= index for item in page.items), (
                ceiling
            )

            for identifier in visible:
                detail = await service.get(principal, identifier, ceiling=ceiling)
                assert detail.id == identifier
                assert SENSITIVITIES.index(detail.sensitivity) <= index

            with pytest.raises(NotFoundError) as absent_raised:
                await service.get(principal, absent, ceiling=ceiling)
            missing_error = (type(absent_raised.value), str(absent_raised.value))

            for identifier in hidden:
                with pytest.raises(NotFoundError) as above_raised:
                    await service.get(principal, identifier, ceiling=ceiling)
                # Above the ceiling is the same error, of the same class,
                # with the same message as an identifier that does not exist.
                assert (
                    type(above_raised.value),
                    str(above_raised.value),
                ) == missing_error, (ceiling, identifier)
