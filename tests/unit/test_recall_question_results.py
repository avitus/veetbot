"""A recorded run stays bound to the question content it tested (ADR-0339)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.recall_questions import (
    QuestionPathResult,
    QuestionResult,
    QuestionSplit,
    QuestionStatus,
    RecallQuestionContent,
)
from tests.contract.support import NOW
from tests.integration.m2_support import memory_settings

OWNER = Principal(
    tenant_id="local",
    principal_id="local-user",
    roles={"user"},
    scopes={"memory.read", "memory.write"},
)


@asynccontextmanager
async def _composition() -> AsyncIterator[Composition]:
    async with build(
        settings=replace(memory_settings(), recall_questions_enabled=True),
        storage="memory",
        sequential_ids=True,
        principal=OWNER,
        clock=FixedClock(NOW),
    ) as composition:
        yield composition


def _service(composition: Composition) -> Any:
    service = composition.services.recall_questions
    assert service is not None
    return service


def _content(fact: str = "Example Town", **overrides: object) -> RecallQuestionContent:
    values: dict[str, object] = {
        "question": "Where do I live?",
        "family": "attributes",
        "expected": ({"statement_contains": fact},),
    }
    values.update(overrides)
    return RecallQuestionContent.model_validate(values)


def _result(content: RecallQuestionContent, *, delivered: int = 1) -> QuestionResult:
    return QuestionResult(
        recorded_at=NOW,
        report_digest="a" * 64,
        content_sha256=content.criteria_sha256,
        paths=(
            QuestionPathResult(
                path="automatic",
                stages=("delivered" if delivered else "ranked",),
                required_total=1,
                required_delivered=delivered,
            ),
        ),
    )


def test_the_fingerprint_follows_what_a_question_measures_and_not_its_notes() -> None:
    town = _content()
    assert town.criteria_sha256 == _content(notes="checked in November").criteria_sha256
    assert town.criteria_sha256 != _content("New Town").criteria_sha256
    assert town.criteria_sha256 != _content(answer_must_contain=("Example",)).criteria_sha256


async def test_a_result_is_recorded_only_for_the_content_it_tested() -> None:
    async with _composition() as composition:
        service = _service(composition)
        town = _content()
        await service.create(OWNER, key="home-city", split=QuestionSplit.HOLDOUT, content=town)

        outcome = await service.record_results(OWNER, {"home-city": _result(town)})
        assert (outcome.recorded, outcome.stale, outcome.unknown) == (1, 0, 0)

        # The clone tested an older wording of the question: its result is refused.
        stale = await service.record_results(
            OWNER,
            {"home-city": _result(_content("Old Town"), delivered=0), "absent": _result(town)},
        )
        assert (stale.recorded, stale.stale, stale.unknown) == (0, 1, 1)
        (view,) = (await service.list_questions(OWNER)).items
        assert view.passed is True and view.latest_result == _result(town)


async def test_changing_what_a_question_measures_clears_its_recorded_result() -> None:
    async with _composition() as composition:
        service = _service(composition)
        town = _content()
        created = await service.create(
            OWNER, key="home-city", split=QuestionSplit.HOLDOUT, content=town
        )
        await service.record_results(OWNER, {"home-city": _result(town)})

        edited = await service.edit(
            OWNER, created.id, expected_revision=1, content=_content("New Town")
        )
        assert edited.latest_result is None and edited.passed is None
        (view,) = (await service.list_questions(OWNER)).items
        assert view.latest_result is None and view.passed is None


async def test_a_note_or_a_status_change_keeps_the_recorded_result() -> None:
    async with _composition() as composition:
        service = _service(composition)
        town = _content()
        created = await service.create(
            OWNER, key="home-city", split=QuestionSplit.HOLDOUT, content=town
        )
        await service.record_results(OWNER, {"home-city": _result(town)})

        noted = await service.edit(
            OWNER, created.id, expected_revision=1, content=_content(notes="checked")
        )
        assert noted.passed is True
        retired = await service.set_status(
            OWNER, created.id, expected_revision=2, status=QuestionStatus.RETIRED
        )
        assert retired.passed is True and retired.latest_result == _result(town)


async def test_a_failed_import_leaves_every_question_as_it_was() -> None:
    async with _composition() as composition:
        service = _service(composition)
        town = _content()
        created = await service.create(
            OWNER, key="home-city", split=QuestionSplit.DEVELOPMENT, content=town
        )
        await service.create(
            OWNER, key="pets", split=QuestionSplit.HOLDOUT, content=_content("a dog")
        )

        with pytest.raises(ConflictError, match="cannot move"):
            await service.import_questions(
                OWNER,
                split=QuestionSplit.DEVELOPMENT,
                items=[("home-city", _content("New Town")), ("pets", _content("a cat"))],
            )

        views = {view.key: view for view in (await service.list_questions(OWNER)).items}
        assert views["home-city"].content == town and views["home-city"].revision == 1
        assert len((await service.history(OWNER, created.id)).items) == 1


async def test_an_edit_returns_the_result_the_store_holds_after_its_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run recorded between the edit's read and its save is what the edit reports."""

    async with _composition() as composition:
        service = _service(composition)
        town = _content()
        created = await service.create(
            OWNER, key="home-city", split=QuestionSplit.HOLDOUT, content=town
        )
        await service.record_results(OWNER, {"home-city": _result(town)})
        async with composition.uow_factory() as uow:
            store = uow.recall_questions
        save = store.save
        missed = _result(town, delivered=0)

        async def recorded_meanwhile(
            question: Any, *, expected_revision: int, clear_result: bool = False
        ) -> bool:
            await store.record_result(
                OWNER, "home-city", missed, expected_revision=expected_revision
            )
            return bool(
                await save(question, expected_revision=expected_revision, clear_result=clear_result)
            )

        monkeypatch.setattr(store, "save", recorded_meanwhile)
        noted = await service.edit(
            OWNER, created.id, expected_revision=1, content=_content(notes="checked")
        )
        retired = await service.set_status(
            OWNER, created.id, expected_revision=2, status=QuestionStatus.RETIRED
        )

    assert noted.passed is False and noted.latest_result == missed
    assert retired.passed is False and retired.revision == 3
