"""ADR-0120 attachments against PostgreSQL: unclaimed rows, claim, and ingestion."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_core.adapters.persistence.database import create_engine
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.gates.test_attachment_upload_adr0120 import PNG
from tests.integration.m2_support import database_settings


@asynccontextmanager
async def _postgres(tmp_path: Path) -> AsyncIterator[Composition]:
    settings = replace(
        database_settings(),
        artifact_root=tmp_path / "artifacts",
        attachment_uploads_enabled=True,
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
    )
    async with build(settings=settings, storage="postgres") as composition:
        yield composition


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
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


async def test_an_upload_is_stored_unclaimed_then_claimed_and_ingested(tmp_path: Path) -> None:
    async with _postgres(tmp_path) as composition, _client(composition) as client:
        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        session_id = created.json()["id"]

        async def upload(content: bytes, name: str, media_type: str, key: str) -> str:
            response = await client.post(
                f"/v1/sessions/{session_id}/artifacts",
                content=content,
                headers={
                    "Content-Type": media_type,
                    "X-Filename": name,
                    "Idempotency-Key": key,
                },
            )
            assert response.status_code == 201, response.text
            assert response.json()["run_id"] is None
            return str(response.json()["id"])

        image_id = await upload(PNG, "cat.png", "image/png", "u1")
        notes_id = await upload(
            b"# Plan\n\nWater the garden daily.", "plan.md", "text/markdown", "u2"
        )
        replay = await client.post(
            f"/v1/sessions/{session_id}/artifacts",
            content=PNG,
            headers={"Content-Type": "image/png", "X-Filename": "cat.png", "Idempotency-Key": "u1"},
        )
        assert replay.status_code == 200
        assert replay.json()["id"] == image_id

        sent = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={
                "content": [
                    {"type": "text", "text": "keep these"},
                    {"type": "image", "artifact_id": image_id, "media_type": "image/png"},
                    {"type": "file", "artifact_id": notes_id, "media_type": "text/markdown"},
                ]
            },
        )
        assert sent.status_code == 202, sent.text
        run_id = UUID(sent.json()["run_id"])
        async with composition.uow_factory() as uow:
            image = await uow.artifacts.get(UUID(image_id), composition.principal)
            notes = await uow.artifacts.get(UUID(notes_id), composition.principal)
            pending = await uow.artifacts.pending_auto_ingest(composition.principal, limit=10)
        assert image.run_id == run_id and image.expires_at is None
        assert notes.run_id == run_id and notes.metadata["auto_ingest"] == "pending"
        assert [artifact.id for artifact in pending] == [UUID(notes_id)]

        await composition.maintenance_factory().run_once()
        async with composition.uow_factory() as uow:
            ingested = await uow.artifacts.get(UUID(notes_id), composition.principal)
            remaining = await uow.artifacts.pending_auto_ingest(composition.principal, limit=10)
        assert ingested.metadata["auto_ingest"] == "ingested"
        assert remaining == []


async def test_only_uploads_and_knowledge_sources_may_lack_a_run(tmp_path: Path) -> None:
    async with _postgres(tmp_path) as composition, _client(composition) as client:
        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        session_id = UUID(created.json()["id"])
    engine = create_engine(os.environ["DATABASE_URL"])
    insert = text(
        "INSERT INTO artifacts (id, tenant_id, principal_id, session_id, run_id, name, "
        "media_type, storage_uri, sha256, size_bytes, origin, trust, metadata, created_at) "
        "VALUES (:id, 'local', 'local-user', :session, NULL, 'x', 'text/plain', '', :sha, 1, "
        ":origin, 'external_untrusted', '{}', now())"
    )
    try:
        with pytest.raises(IntegrityError, match="ck_artifacts_run_or_upload"):
            async with engine.begin() as connection:
                await connection.execute(
                    insert,
                    {
                        "id": UUID(int=80),
                        "session": session_id,
                        "sha": "0" * 64,
                        "origin": "tool_output",
                    },
                )
        async with engine.begin() as connection:
            await connection.execute(
                insert,
                {"id": UUID(int=82), "session": session_id, "sha": "0" * 64, "origin": "upload"},
            )
    finally:
        await engine.dispose()
