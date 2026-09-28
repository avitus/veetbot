"""A refused or failed chat-attachment ingestion is recorded, never raised (ADR-0120).

The gate suite drives the happy path and the secret-scan refusal end to end;
these tests hold the sweep's failure accounting: each refusal carries its reason
code, a vanished source is refused as not found, a transient failure stays
pending until its third attempt fails it, and one bad file never stops the rest
of the batch.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.domain.artifacts import (
    AUTO_INGEST_ATTEMPTS_KEY,
    AUTO_INGEST_KEY,
    AUTO_INGEST_REASON_KEY,
)
from agent_core.domain.errors import NotFoundError, ToolValidationError
from agent_core.domain.knowledge import KnowledgeIngestRequest
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from agent_core.knowledge.service import KnowledgeService
from agent_core.knowledge.uploads import (
    MAX_INGEST_ATTEMPTS,
    UploadAutoIngest,
    refusal_code,
    upload_document_id,
)
from tests.contract.support import NOW, PRINCIPAL_ID, SESSION_ID, TENANT, memory_uow_factory
from tests.contract.support import principal as owner


class _Knowledge:
    """Answers each artifact with the next scripted outcome for it."""

    def __init__(self, outcomes: dict[UUID, list[BaseException | None]]) -> None:
        self._outcomes = outcomes
        self.requests: list[tuple[KnowledgeIngestRequest, TrustLevel]] = []

    async def ingest(self, request: KnowledgeIngestRequest, *, origin_trust: TrustLevel) -> None:
        self.requests.append((request, origin_trust))
        outcome = self._outcomes[request.source.id].pop(0)
        if outcome is not None:
            raise outcome


def _upload(number: int) -> ArtifactRef:
    return ArtifactRef(
        id=UUID(int=number),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        session_id=SESSION_ID,
        run_id=None,
        name=f"notes-{number}.md",
        media_type="text/markdown",
        storage_uri=f"memory://{number}",
        sha256=f"{number:064x}",
        size_bytes=10,
        origin="upload",
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        expires_at=None,
        created_at=NOW + timedelta(seconds=number),
        metadata={AUTO_INGEST_KEY: "pending"},
    )


async def _stack(
    outcomes: dict[int, list[BaseException | None]],
) -> tuple[Any, _Knowledge, UploadAutoIngest]:
    _clock, factory = await memory_uow_factory()
    async with factory() as uow:
        for number in outcomes:
            await uow.artifacts.create(_upload(number))
    knowledge = _Knowledge({UUID(int=number): list(script) for number, script in outcomes.items()})
    sweep = UploadAutoIngest(
        uow_factory=factory,
        knowledge=cast(KnowledgeService, knowledge),
        principal=owner(),
    )
    return factory, knowledge, sweep


async def _metadata(factory: Any, number: int) -> dict[str, Any]:
    async with factory() as uow:
        return dict((await uow.artifacts.get(UUID(int=number), owner())).metadata)


async def test_a_transient_failure_retries_until_its_third_attempt_fails_it() -> None:
    assert MAX_INGEST_ATTEMPTS == 3
    factory, knowledge, sweep = await _stack({1: [RuntimeError("database blip")] * 3})

    assert await sweep.sweep_once() == 0
    first = await _metadata(factory, 1)
    assert (first[AUTO_INGEST_KEY], first[AUTO_INGEST_REASON_KEY]) == ("pending", "transient")
    assert first[AUTO_INGEST_ATTEMPTS_KEY] == 1

    assert await sweep.sweep_once() == 0
    assert (await _metadata(factory, 1))[AUTO_INGEST_ATTEMPTS_KEY] == 2

    assert await sweep.sweep_once() == 0
    failed = await _metadata(factory, 1)
    assert (failed[AUTO_INGEST_KEY], failed[AUTO_INGEST_REASON_KEY]) == ("failed", "transient")
    assert failed[AUTO_INGEST_ATTEMPTS_KEY] == 3

    # A failed file leaves the pending set: a later sweep never retries it.
    assert await sweep.sweep_once() == 0
    assert len(knowledge.requests) == 3


async def test_a_retry_that_succeeds_is_ingested_and_clears_its_attempts() -> None:
    factory, knowledge, sweep = await _stack({1: [RuntimeError("blip"), None]})

    assert await sweep.sweep_once() == 0
    assert await sweep.sweep_once() == 1

    recorded = await _metadata(factory, 1)
    assert recorded[AUTO_INGEST_KEY] == "ingested"
    assert AUTO_INGEST_REASON_KEY not in recorded
    assert AUTO_INGEST_ATTEMPTS_KEY not in recorded
    request, trust = knowledge.requests[-1]
    assert trust is TrustLevel.USER
    assert request.document_id == upload_document_id(_upload(1))
    assert request.title == "notes-1.md"


async def test_a_vanished_source_is_refused_as_not_found_without_a_retry() -> None:
    factory, knowledge, sweep = await _stack({1: [NotFoundError("artifact not found")]})

    assert await sweep.sweep_once() == 0
    assert await sweep.sweep_once() == 0

    recorded = await _metadata(factory, 1)
    assert (recorded[AUTO_INGEST_KEY], recorded[AUTO_INGEST_REASON_KEY]) == (
        "refused",
        "not_found",
    )
    assert len(knowledge.requests) == 1


async def test_one_refused_or_failing_file_does_not_stop_the_batch() -> None:
    factory, _knowledge, sweep = await _stack(
        {
            1: [ToolValidationError("knowledge source failed the secret scan")],
            2: [RuntimeError("blip")],
            3: [None],
        }
    )

    assert await sweep.sweep_once() == 1

    assert (await _metadata(factory, 1))[AUTO_INGEST_REASON_KEY] == "secret_scan"
    assert (await _metadata(factory, 1))[AUTO_INGEST_KEY] == "refused"
    assert (await _metadata(factory, 2))[AUTO_INGEST_KEY] == "pending"
    assert (await _metadata(factory, 3))[AUTO_INGEST_KEY] == "ingested"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("knowledge source failed the secret scan", "secret_scan"),
        ("knowledge source is encrypted", "encrypted"),
        ("knowledge source has no extractable text", "no_text"),
        ("knowledge source exceeds the byte ceiling", "too_large"),
        ("unsupported knowledge media type 'image/png'", "unsupported_type"),
        ("knowledge source is not valid UTF-8", "not_utf8"),
        ("knowledge source could not be read", "unreadable"),
        ("knowledge source is outside the caller scope", "refused"),
    ],
)
def test_refusal_codes_name_the_reason_and_default_to_refused(message: str, code: str) -> None:
    assert refusal_code(ToolValidationError(message)) == code
