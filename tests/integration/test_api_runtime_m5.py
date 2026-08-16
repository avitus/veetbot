"""PostgreSQL-backed Milestone 5 API concurrency and replay verification."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import httpx
from sqlalchemy import func, select, update

from agent_core.adapters.artifacts.local import LocalTrajectoryArtifactStore
from agent_core.adapters.persistence.sqlalchemy_models import (
    ConsolidationRunRow,
    ConsolidationWatermarkRow,
    EventRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    MemoryRow,
    RunRow,
    SessionDeletionArtifactRow,
    SessionDeletionRow,
    SessionRow,
)
from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.knowledge import (
    DocumentAuthority,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestPrepared,
    KnowledgeVisibility,
)
from agent_core.domain.memory import (
    BeliefType,
    ConsolidationRun,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Portability,
    Sensitivity,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, StopReason
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.trajectory import ArtifactRef, TrajectoryExport
from agent_core.domain.views import (
    ContentBlock,
    StreamFrame,
    TextContentBlock,
    TransientStreamFrame,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings


@asynccontextmanager
async def _client(
    composition: Composition,
    *,
    principal: Principal | None = None,
    client_address: tuple[str, int] = ("127.0.0.1", 43105),
) -> Any:
    app = create_app(
        composition.services,
        composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(
        app=app,
        client=client_address,
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _create_session(client: httpx.AsyncClient) -> UUID:
    response = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def test_postgres_submit_idempotency_is_atomic_under_concurrency() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        session = await composition.services.sessions.create(composition.principal, "general", {})
        content: list[ContentBlock] = [TextContentBlock(text="one durable run")]

        first, second = await asyncio.gather(
            composition.services.runs.submit(
                composition.principal, session.id, content, "same-key", None
            ),
            composition.services.runs.submit(
                composition.principal, session.id, content, "same-key", None
            ),
        )

        assert first.run_id == second.run_id
        assert {first.replayed, second.replayed} == {False, True}
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session.id, 0, composition.principal)
        assert [event.event_type for event in events].count("run.queued") == 1

        async with _client(composition) as client:
            changed = await client.post(
                f"/v1/sessions/{session.id}/messages",
                headers={"Idempotency-Key": "same-key"},
                json={"content": [{"type": "text", "text": "different body"}]},
            )
        assert changed.status_code == 409
        assert changed.json()["error"]["details"]["reason"] == "idempotency_key_reused"


async def test_postgres_session_titles_persist_and_legacy_rows_derive_from_history() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        _client(composition) as client,
    ):
        session_id = await _create_session(client)
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": [{"type": "text", "text": "  Restore   this\nconversation  "}]},
        )
        assert submitted.status_code == 202, submitted.text

        stored = await client.get(f"/v1/sessions/{session_id}")
        assert stored.json()["title"] == "Restore this conversation"

        async with composition.uow_factory() as uow:
            database_session = cast(PostgresUnitOfWork, uow)._session
            assert database_session is not None
            await database_session.execute(
                update(SessionRow).where(SessionRow.id == session_id).values(title=None)
            )

        recovered = await client.get(f"/v1/sessions/{session_id}")
        assert recovered.json()["title"] == "Restore this conversation"
        async with composition.uow_factory() as uow:
            database_session = cast(PostgresUnitOfWork, uow)._session
            assert database_session is not None
            persisted_title = await database_session.scalar(
                select(SessionRow.title).where(SessionRow.id == session_id)
            )
        assert persisted_title == "Restore this conversation"

        index = await client.get("/v1/sessions")
        listed = {UUID(row["id"]): row for row in index.json()["items"]}
        assert listed[session_id]["title"] == "Restore this conversation"


async def test_postgres_history_activity_ownership_and_delete_lifecycle() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        owner = composition.principal
        other = Principal(
            tenant_id=owner.tenant_id,
            principal_id="another-history-owner",
            roles={"user"},
            scopes=set(PLATFORM_SCOPES),
        )
        async with (
            _client(composition) as client,
            _client(
                composition,
                principal=other,
                client_address=("127.0.0.1", 43106),
            ) as other_client,
        ):
            first = await _create_session(client)
            second = await _create_session(client)
            hidden = await _create_session(other_client)

            submitted = await client.post(
                f"/v1/sessions/{first}/messages",
                json={"content": [{"type": "text", "text": "make this newest"}]},
            )
            assert submitted.status_code == 202, submitted.text
            run_id = UUID(submitted.json()["run_id"])

            first_page = await client.get("/v1/sessions", params={"limit": 1})
            assert first_page.status_code == 200, first_page.text
            assert [UUID(row["id"]) for row in first_page.json()["items"]] == [first]
            assert first_page.json()["items"][0]["active_run_id"] == str(run_id)
            assert first_page.json()["items"][0]["last_run_id"] == str(run_id)
            cursor = first_page.json()["next_cursor"]
            assert cursor is not None

            second_page = await client.get("/v1/sessions", params={"limit": 1, "cursor": cursor})
            assert second_page.status_code == 200, second_page.text
            assert [UUID(row["id"]) for row in second_page.json()["items"]] == [second]
            assert second_page.json()["next_cursor"] is None
            assert hidden not in {
                UUID(row["id"])
                for row in [
                    *first_page.json()["items"],
                    *second_page.json()["items"],
                ]
            }

            other_index = await other_client.get("/v1/sessions")
            assert other_index.status_code == 200, other_index.text
            assert [UUID(row["id"]) for row in other_index.json()["items"]] == [hidden]
            assert (await other_client.get(f"/v1/sessions/{first}")).status_code == 404
            assert (await other_client.delete(f"/v1/sessions/{first}")).status_code == 404

            blocked = await client.delete(f"/v1/sessions/{first}")
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["error"]["code"] == "conflict"
            assert blocked.json()["error"]["details"] == {
                "reason": "active_run_exists",
                "run_id": str(run_id),
            }

            cancelled = await client.post(f"/v1/runs/{run_id}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == RunStatus.CANCELLED.value
            assert (await client.delete(f"/v1/sessions/{first}")).status_code == 204
            assert (await client.delete(f"/v1/sessions/{first}")).status_code == 204
            assert (await client.get(f"/v1/sessions/{first}")).status_code == 404
            remaining = await client.get("/v1/sessions")
            assert [UUID(row["id"]) for row in remaining.json()["items"]] == [second]


async def test_postgres_session_delete_cascades_and_clears_artifact_work() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        _client(composition) as client,
    ):
        session_id = await _create_session(client)
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": [{"type": "text", "text": "delete this everywhere"}]},
        )
        run_id = UUID(submitted.json()["run_id"])
        assert (await client.post(f"/v1/runs/{run_id}/cancel")).status_code == 200

        content = b"session-scoped artifact"
        artifact = ArtifactRef(
            id=UUID(int=9100),
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            name="delete.txt",
            media_type="text/plain",
            storage_uri="pending",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
            expires_at=composition.clock.now() + timedelta(days=1),
            created_at=composition.clock.now(),
        )
        store = LocalTrajectoryArtifactStore(composition.settings.artifact_root)
        artifact = await store.write(artifact, content)
        async with composition.uow_factory() as uow:
            await uow.trajectory_exports.create(
                TrajectoryExport(
                    export_id=UUID(int=9101),
                    tenant_id=artifact.tenant_id,
                    principal_id=artifact.principal_id,
                    run_id=run_id,
                    artifact=artifact,
                    builder_version="delete-test",
                    ruleset_version="delete-test",
                    created_at=composition.clock.now(),
                )
            )
            events = await uow.events.list_after(session_id, 0, composition.principal)
            now = composition.clock.now()
            memory = MemoryRecord(
                id=UUID(int=9102),
                tenant_id=composition.principal.tenant_id,
                principal_id=composition.principal.principal_id,
                scope="private",
                subject="deletion-test",
                statement="This session-scoped memory must be erased.",
                source_session_id=session_id,
                source_event_ids=[events[0].id],
                confidence=1,
                sensitivity=Sensitivity.PUBLIC,
                valid_from=now,
                status=MemoryStatus.ACTIVE,
                belief_type=BeliefType.FACT,
                portability=Portability.PORTABLE,
                origin_scopes=["private"],
                last_reinforced_at=now,
                formation_run_id=run_id,
                consolidation_policy_version="delete-test",
                authority=MemoryAuthority.USER,
                store_position=await uow.memories.next_position(),
                created_at=now,
                updated_at=now,
            )
            await uow.memories.upsert_belief(memory)
            await uow.memories.record_consolidation(
                ConsolidationRun(
                    id=UUID(int=9103),
                    tenant_id=composition.principal.tenant_id,
                    principal_id=composition.principal.principal_id,
                    trigger="delete-test",
                    scope="private",
                    session_id=session_id,
                    watermark_before=0,
                    watermark_after=events[-1].sequence,
                    model="deterministic",
                    policy_version="delete-test",
                    candidates_proposed=1,
                    committed=1,
                    reinforced=0,
                    superseded=0,
                    rejected=0,
                    started_at=now,
                    finished_at=now,
                )
            )
            await uow.memories.set_consolidation_watermark(
                session_id, composition.principal, events[-1].sequence
            )
            document_id = UUID(int=9104)
            document_row_id = UUID(int=9105)
            chunk_text = "Session-scoped knowledge must also be erased."
            chunk_digest = hashlib.sha256(chunk_text.encode()).hexdigest()
            await uow.knowledge.ingest(
                KnowledgeIngestPrepared(
                    document=KnowledgeDocument(
                        row_id=document_row_id,
                        document_id=document_id,
                        tenant_id=composition.principal.tenant_id,
                        ingested_by_principal_id=composition.principal.principal_id,
                        visibility=KnowledgeVisibility.PRINCIPAL,
                        title="Deletion test",
                        source_ref=artifact,
                        media_type=artifact.media_type,
                        authority=DocumentAuthority.PRINCIPAL_SUPPLIED,
                        version=1,
                        chunker_version="delete-test",
                        valid_from=now,
                        ingested_at=now,
                        sensitivity=Sensitivity.PUBLIC,
                    ),
                    chunks=[
                        KnowledgeChunk(
                            chunk_id=f"kc_{chunk_digest[:16]}",
                            document_row_id=document_row_id,
                            document_id=document_id,
                            version=1,
                            ordinal=0,
                            heading_path=[],
                            text=chunk_text,
                            tokens=8,
                            contains_instruction_like_text=False,
                            content_sha256=chunk_digest,
                        )
                    ],
                )
            )

        assert (await client.delete(f"/v1/sessions/{session_id}")).status_code == 204
        assert (await client.delete(f"/v1/sessions/{session_id}")).status_code == 204

        async with composition.uow_factory() as uow:
            database_session = cast(PostgresUnitOfWork, uow)._session
            assert database_session is not None
            assert await database_session.get(SessionRow, session_id) is None
            assert (
                await database_session.scalar(
                    select(func.count()).select_from(RunRow).where(RunRow.session_id == session_id)
                )
                == 0
            )
            assert (
                await database_session.scalar(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.session_id == session_id)
                )
                == 0
            )
            assert await database_session.get(SessionDeletionRow, session_id) is not None
            for model, predicate in (
                (MemoryRow, MemoryRow.source_session_id == session_id),
                (ConsolidationRunRow, ConsolidationRunRow.session_id == session_id),
                (
                    ConsolidationWatermarkRow,
                    ConsolidationWatermarkRow.session_id == session_id,
                ),
                (
                    KnowledgeDocumentRow,
                    KnowledgeDocumentRow.source_artifact_id == artifact.id,
                ),
                (
                    KnowledgeChunkRow,
                    KnowledgeChunkRow.document_row_id == document_row_id,
                ),
            ):
                assert (
                    await database_session.scalar(
                        select(func.count()).select_from(model).where(predicate)
                    )
                    == 0
                )
            assert (
                await database_session.scalar(
                    select(func.count())
                    .select_from(SessionDeletionArtifactRow)
                    .where(SessionDeletionArtifactRow.session_id == session_id)
                )
                == 0
            )
        assert not list(composition.settings.artifact_root.rglob(f"*{artifact.id}*"))


async def test_postgres_sse_reconnect_is_gapless_and_duplicate_free() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        _client(composition) as client,
    ):
        session_id = await _create_session(client)
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": [{"type": "text", "text": "stream me"}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = UUID(submitted.json()["run_id"])
        cancelled = await client.post(f"/v1/runs/{run_id}/cancel")
        assert cancelled.status_code == 200

        first = await client.get(f"/v1/runs/{run_id}/events")
        assert first.status_code == 200
        first_ids = [
            int(line.removeprefix("id: "))
            for line in first.text.splitlines()
            if line.startswith("id: ")
        ]
        assert first_ids == sorted(set(first_ids))
        assert len(first_ids) >= 3

        replay = await client.get(
            f"/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(first_ids[0])},
        )
        assert replay.status_code == 200, replay.text
        replay_ids = [
            int(line.removeprefix("id: "))
            for line in replay.text.splitlines()
            if line.startswith("id: ")
        ]
        assert replay_ids == first_ids[1:]

        caught_up = await client.get(
            f"/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(first_ids[-1])},
        )
        assert caught_up.status_code == 200, caught_up.text
        assert "id: " not in caught_up.text


async def test_api_cancellation_is_observed_by_a_separate_worker_composition() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                text="this response must never commit",
                stop_reason=StopReason.END_TURN,
                delay_ms=1000,
            )
        ]
    )
    async with (
        build(settings=database_settings(), storage="postgres") as api_composition,
        build(
            settings=database_settings(), storage="postgres", script=script
        ) as worker_composition,
        _client(api_composition) as client,
    ):
        session_id = await _create_session(client)
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": [{"type": "text", "text": "cancel cross-process"}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = UUID(submitted.json()["run_id"])
        worker = DurableWorker(
            uow_factory=worker_composition.uow_factory,
            executor=worker_composition.executor,
            clock=worker_composition.clock,
            worker_id="separate-api-cancellation-worker",
            lease_seconds=0.3,
            heartbeat_divisor=3,
        )
        work = asyncio.create_task(worker.run_once())
        for _attempt in range(100):
            observed = await client.get(f"/v1/runs/{run_id}")
            if observed.json()["status"] == RunStatus.RUNNING.value:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("separate worker did not claim the run")

        response = await client.post(f"/v1/runs/{run_id}/cancel")
        assert response.status_code == 202
        assert response.json()["cancel_requested_at"] is not None
        assert await work
        final = await client.get(f"/v1/runs/{run_id}")

    assert final.json()["status"] == RunStatus.CANCELLED.value


async def test_postgres_live_transport_delivers_transient_model_deltas() -> None:
    script = FakeModelScript(
        turns=[ScriptedTurn(text="a live delta", stop_reason=StopReason.END_TURN)]
    )
    async with (
        build(settings=database_settings(), storage="postgres") as api_composition,
        build(
            settings=database_settings(), storage="postgres", script=script
        ) as worker_composition,
    ):
        session = await api_composition.services.sessions.create(
            api_composition.principal, "general", {}
        )
        submitted = await api_composition.services.runs.submit(
            api_composition.principal,
            session.id,
            [TextContentBlock(text="stream the answer")],
            None,
            None,
        )
        stream = cast(
            AsyncGenerator[StreamFrame, None],
            api_composition.services.runs.stream(api_composition.principal, submitted.run_id, None),
        )
        await anext(stream)  # Opens LISTEN before the worker begins model I/O.
        worker = DurableWorker(
            uow_factory=worker_composition.uow_factory,
            executor=worker_composition.executor,
            clock=worker_composition.clock,
            worker_id="live-delta-worker",
        )
        work = asyncio.create_task(worker.run_once())
        live: TransientStreamFrame | None = None
        try:
            for _attempt in range(20):
                frame = await asyncio.wait_for(anext(stream), timeout=2)
                if isinstance(frame, TransientStreamFrame) and frame.event == "message.delta":
                    live = frame
                    break
            else:
                raise AssertionError("no transient message.delta frame arrived")
        finally:
            await work
            await stream.aclose()

    assert live is not None
    assert live.data["text"] == "a live delta"
