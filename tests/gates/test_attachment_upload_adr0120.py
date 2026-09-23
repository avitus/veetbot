"""The chat attachment upload route (ADR-0120).

`POST /v1/sessions/{id}/artifacts` takes one file as the raw body under the
existing `artifact.write` scope, only when `AGENT_ATTACHMENT_UPLOADS_ENABLED`
is set. The upload belongs to the session until a sent message claims it; the
stored type is detected from the bytes, and a replayed key returns the same
artifact.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import SecretStr
from pypdf import PdfWriter

from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.model.attachments import UPLOAD_MAX_BYTES
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import AGENT_ID, NOW

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _settings(
    tmp_path: Path, *, enabled: bool = True, auth_mode: AuthMode = AuthMode.DEV
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://unused/agent",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=auth_mode,
        auth_token=SecretStr("test-bearer-token") if auth_mode is AuthMode.TOKEN else None,
        sandbox=(
            SandboxMechanism.MICROVM if auth_mode is AuthMode.TOKEN else SandboxMechanism.FAKE
        ),
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
        attachment_uploads_enabled=enabled,
    )


@asynccontextmanager
async def _composition(
    tmp_path: Path, *, enabled: bool = True, auth_mode: AuthMode = AuthMode.DEV
) -> AsyncIterator[Composition]:
    async with build(
        settings=_settings(tmp_path, enabled=enabled), sequential_ids=True
    ) as composition:
        yield replace(
            composition, settings=_settings(tmp_path, enabled=enabled, auth_mode=auth_mode)
        )


@asynccontextmanager
async def _client(
    composition: Composition, *, principal: Principal | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        composition.services,
        composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _session(client: httpx.AsyncClient) -> UUID:
    response = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def _upload(
    client: httpx.AsyncClient,
    session_id: UUID,
    content: bytes | Any,
    *,
    filename: str | None = "photo.png",
    media_type: str | None = "image/png",
    key: str | None = "upload-1",
    extra: dict[str, str] | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if filename is not None:
        headers["X-Filename"] = quote(filename, safe="")
    if media_type is not None:
        headers["Content-Type"] = media_type
    if key is not None:
        headers["Idempotency-Key"] = key
    headers.update(extra or {})
    return await client.post(
        f"/v1/sessions/{session_id}/artifacts", content=content, headers=headers
    )


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


async def test_an_upload_is_stored_unclaimed_and_its_key_replays(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        created = await _upload(client, session_id, PNG, filename="Café photo.png")
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "private, no-store"
        view = created.json()
        assert view["session_id"] == str(session_id)
        assert view["run_id"] is None
        assert view["name"] == "Café photo.png"
        assert view["media_type"] == "image/png"
        assert view["size_bytes"] == len(PNG)
        assert view["metadata"]["attachment"] == {"kind": "image"}
        assert created.headers["location"] == f"/v1/artifacts/{view['id']}"

        content = await client.get(f"/v1/artifacts/{view['id']}/content")
        assert content.status_code == 200
        assert content.content == PNG
        assert content.headers["content-disposition"].startswith("attachment")

        replayed = await _upload(client, session_id, PNG, filename="Café photo.png")
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == view

        reused = await _upload(client, session_id, JPEG, filename="Café photo.png")
        assert reused.status_code == 409
        assert reused.json()["error"]["details"]["reason"] == "idempotency_key_reused"

        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        receipts = [event for event in events if event.event_type == "artifact.uploaded"]
        assert len(receipts) == 1
        assert receipts[0].payload["artifact_id"] == view["id"]
        assert "photo" not in str(receipts[0].payload)


async def test_the_stored_type_is_detected_from_the_bytes(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)

        async def upload(content: bytes, name: str, declared: str, key: str) -> dict[str, Any]:
            response = await _upload(
                client, session_id, content, filename=name, media_type=declared, key=key
            )
            assert response.status_code == 201, response.text
            return dict(response.json())

        mislabelled = await upload(JPEG, "a.png", "image/png", "k1")
        assert mislabelled["media_type"] == "image/jpeg"
        assert mislabelled["metadata"]["attachment"] == {"kind": "image"}

        pdf = await upload(_pdf(2), "report.pdf", "application/octet-stream", "k2")
        assert pdf["media_type"] == "application/pdf"
        assert pdf["metadata"]["attachment"] == {"kind": "pdf", "page_count": 2}

        broken = await upload(b"%PDF-1.7\nnot really", "broken.pdf", "application/pdf", "k3")
        assert broken["media_type"] == "application/pdf"
        assert broken["metadata"]["attachment"] == {"kind": "other"}

        markdown = await upload("# Notes\nÜber".encode(), "notes.md", "text/markdown", "k4")
        assert markdown["media_type"] == "text/markdown"
        assert markdown["metadata"]["attachment"] == {"kind": "text"}

        binary = await upload(b"\xff\xfe\x00bad", "data.txt", "text/plain", "k5")
        assert binary["media_type"] == "application/octet-stream"
        assert binary["metadata"]["attachment"] == {"kind": "other"}

        heic = await upload(b"\x00\x00\x00\x18ftypheic", "IMG.HEIC", "image/heic", "k6")
        assert heic["media_type"] == "image/heic"
        assert heic["metadata"]["attachment"] == {"kind": "other"}


async def test_malformed_uploads_are_refused_before_storage(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        for filename in ['quote".png', "dir/x.png", "back\\slash.png", "line\nbreak.png", ""]:
            response = await _upload(client, session_id, PNG, filename=filename)
            assert response.status_code == 400, (filename, response.text)
            assert response.json()["error"]["code"] == "malformed_request"
        long_name = "x" * 252 + ".png"
        assert (await _upload(client, session_id, PNG, filename=long_name)).status_code == 400
        assert (await _upload(client, session_id, PNG, filename=None)).status_code == 400
        assert (await _upload(client, session_id, PNG, key=None)).status_code == 400
        assert (await _upload(client, session_id, PNG, key="k" * 256)).status_code == 400
        assert (await _upload(client, session_id, b"")).status_code == 400
        assert (await _upload(client, session_id, PNG, media_type="not a type")).status_code == 400
        undecodable = await _upload(
            client, session_id, PNG, filename=None, extra={"X-Filename": "%FF%FE.png"}
        )
        assert undecodable.status_code == 400
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        assert not [event for event in events if event.event_type == "artifact.uploaded"]


async def test_a_closed_or_foreign_session_takes_no_upload(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        foreign = UUID(int=9001)
        closed = UUID(int=9002)
        async with composition.uow_factory() as uow:
            await uow.sessions.create(
                Session(
                    id=foreign,
                    tenant_id=composition.principal.tenant_id,
                    principal_id="someone-else",
                    agent_id=AGENT_ID,
                    agent_version="1.0.0",
                    status=SessionStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await uow.sessions.create(
                Session(
                    id=closed,
                    tenant_id=composition.principal.tenant_id,
                    principal_id=composition.principal.principal_id,
                    agent_id=AGENT_ID,
                    agent_version="1.0.0",
                    status=SessionStatus.CLOSED,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        assert (await _upload(client, foreign, PNG)).status_code == 404
        assert (await _upload(client, UUID(int=9003), PNG)).status_code == 404
        assert (await _upload(client, closed, PNG)).status_code == 409


async def test_uploading_requires_authentication_and_artifact_write(tmp_path: Path) -> None:
    async with (
        _composition(tmp_path, auth_mode=AuthMode.TOKEN) as composition,
        _client(composition) as client,
    ):
        missing = await _upload(client, UUID(int=1), PNG)
        assert missing.status_code == 401
    async with _composition(tmp_path) as composition:
        reader = composition.principal.model_copy(
            update={"scopes": set(PLATFORM_SCOPES) - {"artifact.write"}}
        )
        async with _client(composition) as owner_client:
            session_id = await _session(owner_client)
        async with _client(composition, principal=reader) as client:
            refused = await _upload(client, session_id, PNG)
            assert refused.status_code == 403


async def test_the_upload_limit_is_32_mib_on_this_route_only(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        two_mib = b"\x00" * (2 * 1024 * 1024)
        accepted = await _upload(
            client, session_id, two_mib, filename="blob.bin", media_type="application/zip"
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["metadata"]["attachment"] == {"kind": "other"}

        declared = await _upload(
            client,
            session_id,
            b"\x00",
            key="declared",
            extra={"Content-Length": str(UPLOAD_MAX_BYTES + 1)},
        )
        assert declared.status_code == 413
        assert "32 MiB" in declared.json()["error"]["message"]

        async def oversized() -> AsyncIterator[bytes]:
            chunk = b"\x00" * (1024 * 1024)
            for _ in range(UPLOAD_MAX_BYTES // len(chunk) + 1):
                yield chunk

        streamed = await _upload(client, session_id, oversized(), key="streamed")
        assert streamed.status_code == 413

        message = await client.post(
            f"/v1/sessions/{session_id}/messages",
            content=b'{"content":[{"type":"text","text":"' + b"a" * two_mib.__len__() + b'"}]}',
            headers={"Content-Type": "application/json"},
        )
        assert message.status_code == 413
        assert "1 MiB" in message.json()["error"]["message"]


async def test_the_route_is_absent_while_the_flag_is_off(tmp_path: Path) -> None:
    async with (
        _composition(tmp_path, enabled=False) as composition,
        _client(composition) as client,
    ):
        session_id = await _session(client)
        small = await _upload(client, session_id, PNG)
        assert small.status_code == 404
        large = await _upload(client, session_id, b"\x00" * (2 * 1024 * 1024))
        assert large.status_code == 413
        paths = client._transport.app.openapi()["paths"]  # type: ignore[attr-defined]
        assert "/v1/sessions/{session_id}/artifacts" not in paths


async def test_concurrent_uploads_with_one_key_store_one_artifact(tmp_path: Path) -> None:
    import asyncio

    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        first, second = await asyncio.gather(
            _upload(client, session_id, PNG, key="same"),
            _upload(client, session_id, PNG, key="same"),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 201]
        assert first.json()["id"] == second.json()["id"]
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        assert len([event for event in events if event.event_type == "artifact.uploaded"]) == 1
        stored = [path for path in (tmp_path / "artifacts").rglob("*") if path.is_file()]
        assert len(stored) == 1


async def test_a_failed_row_leaves_neither_bytes_nor_a_receipt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from agent_core.adapters.persistence.memory import InMemoryArtifactRepository
    from agent_core.domain.errors import ArtifactStorageError

    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)

        async def refuse(self: object, artifact: object) -> object:
            del self, artifact
            raise ArtifactStorageError("metadata store unavailable")

        monkeypatch.setattr(InMemoryArtifactRepository, "create", refuse)
        failed = await _upload(client, session_id, PNG)
        assert failed.status_code == 503
        assert not [path for path in (tmp_path / "artifacts").rglob("*") if path.is_file()]
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        assert not [event for event in events if event.event_type == "artifact.uploaded"]
