"""Behavioral contract for the people import control composition interface."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_core.adapters.determinism import SystemClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.application.people import PublicPeopleService
from agent_core.bootstrap import build
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelEvent,
    ModelRequest,
    ModelUsage,
    ResolvedModel,
    ScriptedTurn,
)
from agent_core.domain.people_imports import PeopleImportCancel, PeopleImportRequest
from tests.contract.support import principal
from tests.integration.m2_support import memory_settings


@pytest.mark.parametrize("state", ["completed", "cancelled"])
async def test_terminal_import_job_blocks_a_new_slice_until_its_run_settles(state: str) -> None:
    from uuid import uuid4

    from agent_core.domain.errors import ConflictError
    from agent_core.domain.people import PeopleImportJob
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import run

    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "session.read", "session.write"}}
    )
    async with build(
        settings=replace(memory_settings(), people_enabled=True),
        storage="memory",
        principal=owner,
        memory_people_evaluation_mode=True,
    ) as app:
        source = await app.services.sessions.create(owner, "general", {})
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": source.id,
                "scope": {
                    "session_ids": [source.id],
                    "since": app.clock.now() - timedelta(days=1),
                    "until": app.clock.now(),
                    "max_records": 10,
                    "max_cost_usd": "1",
                },
            }
        )
        service = app.services.people
        assert service is not None
        previous = await service.create_import(
            owner, request, key="old", ceiling=Sensitivity.SENSITIVE
        )
        active = run(status=RunStatus.RUNNING).model_copy(
            update={"id": uuid4(), "session_id": source.id}
        )
        async with app.uow_factory() as uow:
            await uow.runs.create(active)
            job = await uow.people.get(owner, previous.id, ceiling=Sensitivity.RESTRICTED)
            assert isinstance(job, PeopleImportJob)
            await uow.people.put(
                job.model_copy(
                    update={
                        "state": state,
                        "run_id": active.id,
                        "revision": job.revision + 1,
                    }
                ),
                expected_revision=job.revision,
            )
        next_job = await service.create_import(
            owner, request, key="new", ceiling=Sensitivity.SENSITIVE
        )
        apply = request.model_copy(
            update={
                "phase": "apply",
                "operation_id": next_job.id,
                "expected_revision": next_job.revision,
            }
        )
        with pytest.raises(ConflictError, match="another People import is still active"):
            await service.create_import(owner, apply, key="start", ceiling=Sensitivity.SENSITIVE)


@pytest.mark.parametrize("failed_attempts", [1, 2])
async def test_failed_analysis_retries_its_original_source_without_counting_it_twice(
    failed_attempts: int,
) -> None:
    import json

    owner = principal().model_copy(
        update={
            "scopes": {
                "people.read",
                "people.write",
                "session.read",
                "session.write",
            }
        }
    )
    script = FakeModelScript(turns=[])
    provider = FakeModelProvider(script, SystemClock())
    async with build(
        settings=replace(memory_settings(), people_enabled=True),
        storage="memory",
        principal=owner,
        memory_people_evaluation_mode=True,
        model_provider_overrides={"fake": provider},
    ) as app:
        source = await app.services.sessions.create(owner, "general", {})
        content = "I prefer jasmine tea."
        async with app.uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=source.id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    payload={"content": content},
                )
            )
        sequence = event.sequence
        script.turns.extend([ScriptedTurn(text="{}") for _ in range(3 * failed_attempts)])
        script.turns.extend(
            [
                ScriptedTurn(text=json.dumps(response))
                for response in [
                    {
                        "episodes": [
                            {
                                "narrative": f"[e:{sequence}] {content}",
                                "subjects": ["beverage preferences"],
                                "source_event_ids": [sequence],
                            }
                        ]
                    },
                    {"predictions": []},
                    {
                        "candidates": [
                            {
                                "subject": "beverage preferences",
                                "statement": "User prefers jasmine tea.",
                                "source_event_ids": [sequence],
                                "sensitivity_guess": "internal",
                                "claim_kind": "preference",
                                "derivation": "direct",
                                "polarity": "assert",
                                "people": None,
                                "evidence_spans": [{"source_event_id": sequence, "text": content}],
                            }
                        ],
                        "coverage": [
                            {
                                "coverage_unit_id": f"{sequence}:1",
                                "decision": "formed",
                                "candidate_indexes": [0],
                                "prediction_indexes": [],
                            }
                        ],
                        "interactions": [],
                    },
                ]
            ]
        )
        service = app.services.people
        assert service is not None
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": str(source.id),
                "scope": {
                    "session_ids": [str(source.id)],
                    "since": (event.created_at - timedelta(seconds=1)).isoformat(),
                    "until": app.clock.now().isoformat(),
                    "max_records": 10,
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await service.create_import(
            owner, request, key="preview", ceiling=Sensitivity.SENSITIVE
        )
        failed = await service.create_import(
            owner,
            request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        assert failed.state == "failed" and failed.error_code == "analysis_incomplete"
        assert failed.records_processed == 0 and failed.failures == 1
        first_read = failed.records_read
        for attempt in range(failed_attempts):
            resumed = await service.create_import(
                owner,
                request.model_copy(
                    update={
                        "phase": "resume",
                        "operation_id": failed.id,
                        "expected_revision": failed.revision,
                    }
                ),
                key=f"resume-{attempt}",
                ceiling=Sensitivity.SENSITIVE,
            )
            assert resumed.records_read == first_read
            if attempt < failed_attempts - 1:
                assert resumed.state == "failed" and resumed.failures == 1
                assert resumed.records_processed == 0
                failed = resumed
            else:
                assert resumed.state == "completed" and resumed.analysis_complete
                assert resumed.records_processed == 1 and resumed.failures == 0
        assert len(provider.requests) == 3 * (failed_attempts + 1)


@pytest.mark.parametrize("failure", ["cancel", "uncertain", "overrun"])
async def test_import_interruption_preserves_progress_and_blocks_late_formation(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = principal().model_copy(
        update={
            "scopes": {
                "people.read",
                "people.write",
                "session.read",
                "session.write",
            }
        }
    )
    turns = (
        []
        if failure == "uncertain"
        else [
            ScriptedTurn(
                text="{}",
                usage=ModelUsage(cost=Decimal("0.01") if failure == "overrun" else Decimal(0)),
            )
        ]
    )
    provider = FakeModelProvider(FakeModelScript(turns=turns), SystemClock())
    original_stream = provider.stream
    service = None
    job_id = None

    async def interrupting_stream(
        request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        async for event in original_stream(request, resolved, attempt):
            yield event
        if failure == "cancel":
            assert isinstance(service, PublicPeopleService) and job_id is not None
            job = await service.get_import(owner, job_id, ceiling=Sensitivity.SENSITIVE)
            await service.cancel_import(
                owner,
                job_id,
                PeopleImportCancel(expected_revision=job.revision),
                key="cancel-during-provider",
                ceiling=Sensitivity.SENSITIVE,
            )

    monkeypatch.setattr(provider, "stream", interrupting_stream)
    async with build(
        settings=replace(memory_settings(), people_enabled=True),
        storage="memory",
        principal=owner,
        memory_people_evaluation_mode=True,
        model_provider_overrides={"fake": provider},
    ) as app:
        source = await app.services.sessions.create(owner, "general", {})
        async with app.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=source.id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    payload={"content": "I prefer jasmine tea."},
                )
            )
        service = app.services.people
        assert service is not None
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": str(source.id),
                "scope": {
                    "session_ids": [str(source.id)],
                    "since": (app.clock.now() - timedelta(days=1)).isoformat(),
                    "until": (app.clock.now() + timedelta(days=1)).isoformat(),
                    "max_records": 10,
                    "max_cost_usd": "0.001",
                },
            }
        )
        preview = await service.create_import(
            owner, request, key="preview", ceiling=Sensitivity.SENSITIVE
        )
        from agent_core.domain.errors import NotFoundError
        from agent_core.ports.people_runtime import PeopleImportControl

        assert isinstance(service, PublicPeopleService)
        control: PeopleImportControl = service.imports
        async with app.uow_factory() as uow:
            stored = await control._get(uow, owner, preview.id, Sensitivity.SENSITIVE)
            assert stored.id == preview.id
            foreign = owner.model_copy(update={"principal_id": "another-owner"})
            with pytest.raises(NotFoundError):
                await control._get(uow, foreign, preview.id, Sensitivity.RESTRICTED)
        job_id = preview.id
        result = await service.create_import(
            owner,
            request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        assert result.state == ("cancelled" if failure == "cancel" else "failed")
        assert result.records_processed == 0
        assert not result.analysis_complete
        assert len(provider.requests) == 1
        if failure == "overrun":
            assert result.spent_usd == Decimal("0.01")
        async with app.uow_factory() as uow:
            assert await uow.memories.list_memories(owner) == []
            assert await uow.memories.consolidation_watermark(source.id, owner) == 0


@pytest.mark.parametrize("during_call", [False, True])
async def test_import_yields_to_active_chat_before_reading_or_spending(
    during_call: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import run

    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "session.read", "session.write"}}
    )
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="{}") for _ in range(4)]), SystemClock()
    )
    wake = asyncio.Event()

    class RetryClock(SystemClock):
        async def sleep(self, seconds: float) -> None:
            await wake.wait()

    async with build(
        settings=replace(memory_settings(), people_enabled=True),
        storage="memory",
        clock=RetryClock(),
        principal=owner,
        memory_people_evaluation_mode=True,
        model_provider_overrides={"fake": provider},
    ) as app:
        source = await app.services.sessions.create(owner, "general", {})
        chat = await app.services.sessions.create(owner, "general", {})
        active = run(status=RunStatus.RUNNING).model_copy(
            update={"session_id": chat.id, "priority": 0}
        )
        async with app.uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=source.id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    payload={"content": "I prefer jasmine tea."},
                )
            )
            if not during_call:
                await uow.runs.create(active)
        original_stream = provider.stream
        interrupted = False

        async def foreground_chat_arrives(
            request: ModelRequest,
            resolved: ResolvedModel,
            attempt: ModelAttempt,
        ) -> AsyncIterator[ModelEvent]:
            nonlocal interrupted
            async for model_event in original_stream(request, resolved, attempt):
                yield model_event
            if during_call and not interrupted:
                interrupted = True
                async with app.uow_factory() as uow:
                    await uow.runs.create(active)

        monkeypatch.setattr(provider, "stream", foreground_chat_arrives)
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": str(source.id),
                "scope": {
                    "session_ids": [str(source.id)],
                    "since": event.created_at.isoformat(),
                    "until": app.clock.now().isoformat(),
                    "max_records": 10,
                    "max_cost_usd": "1",
                },
            }
        )
        service = app.services.people
        assert service is not None
        preview = await service.create_import(
            owner, request, key="idle-preview", ceiling=Sensitivity.SENSITIVE
        )
        result = await service.create_import(
            owner,
            request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="idle-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        assert result.state == "queued" and result.error_code == "waiting_for_chat"
        assert result.records_read == result.records_processed == 0
        assert result.spent_usd == result.reserved_usd == 0
        assert len(provider.requests) == int(during_call)
        assert result.run_id is not None
        async with app.uow_factory() as uow:
            queued = await uow.runs.get(result.run_id, owner)
            assert queued.scheduled_for is not None and queued.scheduled_for > app.clock.now()
            assert queued.deadline_at is not None
            assert (queued.deadline_at - queued.scheduled_for).total_seconds() == 120
            await uow.runs.transition(active.id, RunStatus.RUNNING, RunStatus.COMPLETED)
        # The local composition must resume its queued import without another
        # user request. Invalid fixture output then pauses at the first source.
        wake.set()
        async with asyncio.timeout(2):
            while result.state in {"queued", "running"}:
                await asyncio.sleep(0.01)
                result = await service.get_import(owner, result.id, ceiling=Sensitivity.SENSITIVE)
        assert result.state == "failed" and result.error_code == "analysis_incomplete"
        assert result.records_read == 1
