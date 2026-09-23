"""Owner-sent documents are added to knowledge (ADR-0120).

Sending a text, Markdown, or PDF attachment marks it pending; the maintenance
worker ingests it through the ordinary knowledge service with `USER` origin
trust. The secret scan still refuses, a refusal is recorded on the artifact
with a reason code, and the same file sent twice is one document.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from agent_core.knowledge.uploads import upload_document_id
from tests.gates.test_attachment_submit_adr0120 import _artifact, _send, _uploaded
from tests.gates.test_attachment_upload_adr0120 import PNG, _client, _composition, _session


def _text_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer, built by hand with exact offsets."""

    content = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        output += b"%010d 00000 n \n" % offset
    output += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(output)


async def _sweep(composition: Any) -> None:
    await composition.maintenance_factory().run_once()


async def _document(composition: Any, artifact: dict[str, Any]) -> Any:
    stored = await _artifact(composition, artifact["id"])
    async with composition.uow_factory() as uow:
        return await uow.knowledge.latest(stored.tenant_id, upload_document_id(stored))


async def test_owner_sent_documents_are_ingested_and_searchable(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        notes = await _uploaded(
            client,
            session_id,
            b"# Garden plan\n\nPlant the tomatoes after the last frost in May.",
            "garden.md",
            "text/markdown",
            "u1",
        )
        pdf = await _uploaded(
            client,
            session_id,
            _text_pdf("Quarterly revenue grew eleven percent"),
            "q3.pdf",
            "application/pdf",
            "u2",
        )
        image = await _uploaded(client, session_id, PNG, "cat.png", "image/png", "u3")
        sent = await _send(
            client,
            session_id,
            [
                {"type": "text", "text": "keep these"},
                {"type": "file", "artifact_id": notes["id"], "media_type": "text/markdown"},
                {"type": "file", "artifact_id": pdf["id"], "media_type": "application/pdf"},
                {"type": "image", "artifact_id": image["id"], "media_type": "image/png"},
            ],
        )
        assert sent.status_code == 202, sent.text
        await _sweep(composition)

        for artifact, title in [(notes, "garden.md"), (pdf, "q3.pdf")]:
            stored = await _artifact(composition, artifact["id"])
            assert stored.metadata["auto_ingest"] == "ingested", stored.metadata
            document = await _document(composition, artifact)
            assert document is not None
            assert document.title == title
            assert document.visibility.value == "principal"
        assert "auto_ingest" not in (await _artifact(composition, image["id"])).metadata

        from agent_core.domain.knowledge import KnowledgeQuery

        result = await composition.knowledge.search(
            KnowledgeQuery(
                tenant_id=composition.principal.tenant_id,
                principal_id=composition.principal.principal_id,
                current_scope=None,
                text="tomatoes frost",
                budget_tokens=2_000,
                max_passages=5,
                min_score=0.0,
            ),
            session_id=session_id,
        )
        assert any(
            passage.title == "garden.md" and "tomatoes" in passage.text
            for passage in result.passages
        )


async def test_the_secret_scan_refuses_and_records_a_reason(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        secret = await _uploaded(
            client, session_id, b"api_key = sk-live-123456", "keys.txt", "text/plain", "u1"
        )
        sent = await _send(
            client,
            session_id,
            [{"type": "file", "artifact_id": secret["id"], "media_type": "text/plain"}],
        )
        assert sent.status_code == 202
        await _sweep(composition)
        stored = await _artifact(composition, secret["id"])
        assert stored.metadata["auto_ingest"] == "refused"
        assert stored.metadata["auto_ingest_reason"] == "secret_scan"
        assert await _document(composition, secret) is None


async def test_the_same_file_sent_twice_is_one_document(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        body = b"# Recipe\n\nTwo eggs, one cup of flour."
        artifacts = []
        for index in range(2):
            session_id = await _session(client)
            uploaded = await _uploaded(
                client, session_id, body, "recipe.md", "text/markdown", f"u{index}"
            )
            sent = await _send(
                client,
                session_id,
                [{"type": "file", "artifact_id": uploaded["id"], "media_type": "text/markdown"}],
            )
            assert sent.status_code == 202
            artifacts.append(uploaded)
        await _sweep(composition)
        first = await _document(composition, artifacts[0])
        second = await _document(composition, artifacts[1])
        assert first is not None and second is not None
        # One document; each chat's copy is its own version, so deleting one
        # chat leaves the document the other chat sent.
        assert first.document_id == second.document_id
        assert first.version == 2
        for artifact in artifacts:
            assert (await _artifact(composition, artifact["id"])).metadata[
                "auto_ingest"
            ] == "ingested"


async def test_deleting_the_chat_deletes_its_document(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        notes = await _uploaded(
            client, session_id, b"# Trip\n\nFlight on Friday.", "trip.md", "text/markdown", "u1"
        )
        sent = await _send(
            client,
            session_id,
            [{"type": "file", "artifact_id": notes["id"], "media_type": "text/markdown"}],
        )
        assert sent.status_code == 202
        await _sweep(composition)
        stored = await _artifact(composition, notes["id"])
        assert await _document(composition, notes) is not None
        deleted = await client.delete(f"/v1/sessions/{session_id}")
        assert deleted.status_code in {202, 204}, deleted.text
        await _sweep(composition)
        async with composition.uow_factory() as uow:
            assert await uow.knowledge.latest(stored.tenant_id, upload_document_id(stored)) is None


def test_document_ids_follow_the_content_not_the_upload() -> None:
    from datetime import UTC, datetime

    from agent_core.domain.policies import TrustLevel
    from agent_core.domain.trajectory import ArtifactRef

    def upload(n: int, sha: str) -> ArtifactRef:
        return ArtifactRef(
            id=UUID(int=n),
            tenant_id="t",
            principal_id="p",
            session_id=UUID(int=100 + n),
            run_id=None,
            name="a.md",
            media_type="text/markdown",
            storage_uri="",
            sha256=sha,
            size_bytes=1,
            origin="upload",
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
            expires_at=None,
            created_at=datetime(2026, 9, 23, tzinfo=UTC),
        )

    assert upload_document_id(upload(1, "a" * 64)) == upload_document_id(upload(2, "a" * 64))
    assert upload_document_id(upload(1, "a" * 64)) != upload_document_id(upload(1, "b" * 64))
