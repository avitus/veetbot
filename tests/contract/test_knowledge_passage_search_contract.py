"""Passage search: visibility admits, scope ranks, one document cannot fill the budget.

knowledge-documents.md ("Scope and visibility", "Retrieval") fixes these rules:
`principal_id` on a document records who ingested it and is not the isolation
predicate; visibility resolved against the current scope and the sensitivity
ceiling are hard filters; scope affinity is a ranking feature only; and a
per-document cap of two passages keeps one document from filling the budget.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
from agent_core.domain.knowledge import (
    DocumentAuthority,
    KnowledgeDocument,
    KnowledgeIngestPrepared,
    KnowledgeQuery,
    KnowledgeVisibility,
    RetrievedPassage,
)
from agent_core.domain.memory import Sensitivity
from agent_core.knowledge.chunking import DeterministicChunker
from tests.contract.memory_fixtures import prepared_knowledge
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT

OTHER_PRINCIPAL = "principal-b"


def _document(
    number: int,
    text: str,
    *,
    visibility: KnowledgeVisibility = KnowledgeVisibility.PRINCIPAL,
    project_scope: str | None = None,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    tenant_id: str = TENANT,
    valid_from: datetime = NOW,
    valid_to: datetime | None = None,
) -> KnowledgeIngestPrepared:
    base = prepared_knowledge().document
    document = KnowledgeDocument.model_validate(
        base.model_dump()
        | {
            "row_id": UUID(int=1_000 + number),
            "document_id": UUID(int=2_000 + number),
            "tenant_id": tenant_id,
            "visibility": visibility,
            "project_scope": project_scope,
            "title": f"Document {number}",
            "sensitivity": sensitivity,
            "authority": DocumentAuthority.PRINCIPAL_SUPPLIED,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_ref": base.source_ref.model_copy(update={"tenant_id": tenant_id}),
        }
    )
    chunks = DeterministicChunker().chunk(
        text,
        document.title,
        document_row_id=document.row_id,
        document_id=document.document_id,
        version=document.version,
    )
    return KnowledgeIngestPrepared(document=document, chunks=chunks)


def _query(**changes: object) -> KnowledgeQuery:
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "principal_id": PRINCIPAL_ID,
        "current_scope": None,
        "text": "rollback procedure",
        "budget_tokens": 3_000,
        "max_passages": 10,
        "min_score": 0.1,
    }
    return KnowledgeQuery.model_validate(values | changes)


async def _store(*documents: KnowledgeIngestPrepared) -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore(FixedClock(NOW))
    for prepared in documents:
        await store.ingest(prepared)
    return store


def _ids(passages: list[RetrievedPassage]) -> list[UUID]:
    return [passage.document_id for passage in passages]


async def test_visibility_not_the_ingesting_principal_decides_who_reads_a_document() -> None:
    store = await _store(
        _document(1, "The rollback procedure is private."),
        _document(2, "The rollback procedure is shared.", visibility=KnowledgeVisibility.TENANT),
        _document(
            3,
            "The rollback procedure for Atlas.",
            visibility=KnowledgeVisibility.PROJECT,
            project_scope="atlas",
        ),
    )
    private, shared, project = (UUID(int=2_000 + number) for number in (1, 2, 3))

    assert set(_ids(await store.search(_query()))) == {private, shared}
    colleague = _query(principal_id=OTHER_PRINCIPAL)
    assert set(_ids(await store.search(colleague))) == {shared}
    in_atlas = _query(principal_id=OTHER_PRINCIPAL, current_scope="atlas")
    assert set(_ids(await store.search(in_atlas))) == {shared, project}
    elsewhere = _query(current_scope="zephyr")
    assert project not in _ids(await store.search(elsewhere))
    assert await store.search(_query(tenant_id="tenant-b")) == []


async def test_scope_affinity_ranks_a_project_document_above_an_equal_tenant_one() -> None:
    store = await _store(
        _document(1, "Rollback procedure: revert the tag.", visibility=KnowledgeVisibility.TENANT),
        _document(
            2,
            "Rollback procedure: revert the tag.",
            visibility=KnowledgeVisibility.PROJECT,
            project_scope="atlas",
        ),
    )

    ranked = await store.search(_query(current_scope="atlas"))

    assert _ids(ranked) == [UUID(int=2_002), UUID(int=2_001)]
    assert ranked[0].score > ranked[1].score


async def test_one_document_contributes_at_most_two_passages() -> None:
    sections = "\n\n".join(
        f"# Step {index}\n\nThe rollback procedure step {index} reverts one service."
        for index in range(5)
    )
    store = await _store(
        _document(1, sections),
        _document(2, "# Other\n\nA different rollback procedure note."),
    )

    passages = await store.search(_query())

    assert _ids(passages).count(UUID(int=2_001)) == 2
    assert UUID(int=2_002) in _ids(passages)
    assert len(passages) == 3
    assert [passage.score for passage in passages] == sorted(
        (passage.score for passage in passages), reverse=True
    )
    assert len(await store.search(_query(max_per_document=1, max_passages=10))) == 2
    assert len(await store.search(_query(max_passages=1))) == 1


async def test_sensitivity_ceiling_and_validity_window_are_hard_filters() -> None:
    store = await _store(
        _document(1, "Rollback procedure, restricted.", sensitivity=Sensitivity.RESTRICTED),
        _document(2, "Rollback procedure, current."),
        _document(
            3,
            "Rollback procedure, retired edition.",
            valid_from=NOW - timedelta(days=30),
            valid_to=NOW - timedelta(days=1),
        ),
        _document(4, "Rollback procedure, not yet valid.", valid_from=NOW + timedelta(days=1)),
    )

    assert set(_ids(await store.search(_query()))) == {UUID(int=2_001), UUID(int=2_002)}
    ceiling = _query(sensitivity_ceiling=Sensitivity.SENSITIVE)
    assert _ids(await store.search(ceiling)) == [UUID(int=2_002)]
    historical = _query(as_of=NOW - timedelta(days=2))
    assert _ids(await store.search(historical)) == [UUID(int=2_003)]
