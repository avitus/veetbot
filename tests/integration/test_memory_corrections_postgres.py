"""ADR-0224 on Postgres: corrections are one transaction; counts match the list.

The owner's correction event, the CHANGED rejection, the replacement belief
and the supersession link commit together or not at all, and the SQL counts
apply exactly the predicates the SQL browse applies.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_core.bootstrap import build
from agent_core.domain.memory import (
    BeliefType,
    MemoryReviewOutcome,
    MemoryStatus,
    RejectionKind,
    Sensitivity,
)
from agent_core.domain.views import MemoryView
from agent_core.memory.formation import GovernedMemoryService
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.integration.test_memory_postgres_m9 import _remember, _settings


async def test_postgres_changed_review_cites_the_owners_correction(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), memory_api_enabled=True)
    async with build(settings=settings, storage="postgres") as composition:
        owner = composition.principal.model_copy(
            update={"scopes": set(PLATFORM_SCOPES) | {"memory.read", "memory.write"}}
        )
        session_id = await composition.sessions.create()
        marker = f"memfix{uuid4().hex[:10]}"
        belief = await _remember(
            composition, session_id, f"The {marker} standup is at nine", subject=f"standup-{marker}"
        )
        corrected = f"The {marker} standup is at ten"

        view = await composition.services.memory.review(
            owner,
            belief.id,
            MemoryReviewOutcome.CHANGED,
            ceiling=Sensitivity.RESTRICTED,
            key=f"fix-{marker}",
            replacement_statement=corrected,
            expected_updated_at=belief.updated_at,
        )

        assert isinstance(view, MemoryView)
        assert view.statement == corrected
        async with composition.uow_factory() as uow:
            old = await uow.memories.get(belief.id, owner)
            events = await uow.events.list_after(session_id, 0, owner)
        correction = next(e for e in events if e.event_type == "memory.owner_correction")
        assert view.source_event_ids == [correction.sequence]
        assert old.status is MemoryStatus.SUPERSEDED
        assert old.superseded_by == view.id


async def test_postgres_a_failed_replacement_rolls_back_every_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: GovernedMemoryService, **_: Any) -> Any:
        raise RuntimeError("replacement could not be formed")

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memroll{uuid4().hex[:10]}"
        belief = await _remember(
            composition, session_id, f"The {marker} office is downtown", subject=f"office-{marker}"
        )
        async with composition.uow_factory() as uow:
            before = await uow.events.list_after(session_id, 0, composition.principal)
        monkeypatch.setattr(GovernedMemoryService, "remember_formation", broken)

        with pytest.raises(RuntimeError):
            await composition.memory.reject(
                belief.id,
                RejectionKind.CHANGED,
                replacement_statement=f"The {marker} office is uptown",
            )

        async with composition.uow_factory() as uow:
            after = await uow.events.list_after(session_id, 0, composition.principal)
            current = await uow.memories.get(belief.id, composition.principal)
            rejections = await uow.memories.outstanding_rejections(
                composition.principal.tenant_id, composition.principal.principal_id
            )
        assert after == before
        assert current.status is MemoryStatus.ACTIVE
        assert current.superseded_by is None
        assert all(rejection.belief_id != belief.id for rejection in rejections)


async def test_postgres_counts_match_the_list_they_label(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), memory_api_enabled=True)
    async with build(settings=settings, storage="postgres") as composition:
        owner = composition.principal.model_copy(
            update={"scopes": set(PLATFORM_SCOPES) | {"memory.read", "memory.write"}}
        )
        session_id = await composition.sessions.create()
        marker = f"memcount{uuid4().hex[:10]}"
        await _remember(composition, session_id, f"{marker} likes tea", subject=f"tea-{marker}")
        await _remember(
            composition,
            session_id,
            f"{marker} works from Lisbon",
            subject=f"work-{marker}",
            belief_type=BeliefType.FACT,
            sensitivity=Sensitivity.RESTRICTED,
        )
        retired = await _remember(
            composition, session_id, f"{marker} owns a kayak", subject=f"kayak-{marker}"
        )
        await composition.memory.reject(retired.id, RejectionKind.UNTRUE)
        service = composition.services.memory

        async def listed(ceiling: Sensitivity, **filters: Any) -> int:
            page = await service.list(
                owner,
                ceiling=ceiling,
                statuses=filters.get("statuses"),
                belief_types=filters.get("belief_types"),
                subject=None,
                session_id=None,
                text=None,
                limit=200,
                cursor=None,
                flagged=filters.get("flagged"),
            )
            return len(page.items)

        for ceiling in (Sensitivity.INTERNAL, Sensitivity.RESTRICTED):
            counts = await service.counts(owner, ceiling=ceiling)
            assert counts.live == await listed(ceiling)
            assert counts.flagged == await listed(ceiling, flagged=True)
            for status in MemoryStatus:
                assert counts.by_status[status] == await listed(ceiling, statuses=[status])
            for belief_type in BeliefType:
                assert counts.by_type[belief_type] == await listed(
                    ceiling, belief_types=[belief_type]
                )
        assert (await service.counts(owner, ceiling=Sensitivity.RESTRICTED)).live == 2
        assert (await service.counts(owner, ceiling=Sensitivity.INTERNAL)).live == 1
