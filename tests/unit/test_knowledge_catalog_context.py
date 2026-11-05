"""The knowledge catalog in context: row 13b, its trust, its absence rules and its yield.

knowledge-documents.md ("The document catalog in context", ADR-0299): the
builder reads the catalog through an injected source, renders it as one
`INTERNAL_TOOL` item with `source="knowledge.catalog"` after the records
catalog and before the runtime metadata, never touches the prefix, leaves it
out when the roster lacks `knowledge.search`, and drops it whole under pressure
after the records catalog and before in-turn recall trims.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.context.builder import BudgetedContextBuilder
from agent_core.context.estimator import ConservativeTokenEstimator, canonical_json_bytes
from agent_core.context.knowledge_catalog import (
    KnowledgeCatalogSource,
    render_knowledge_catalog,
)
from agent_core.context.records_catalog import RecordsCatalogSource
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.agents import Principal
from agent_core.domain.context import REGION_ASSIGNMENTS, ContextClass, ContextPlan, ContextRegion
from agent_core.domain.knowledge import KnowledgeCatalog, KnowledgeCatalogLine
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import ConversationItem, ModelRequest, TextPart, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.domain.records import CatalogLine, RecordComponent, RecordsCatalog
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.knowledge.catalog import KnowledgeCatalogReader
from agent_core.tools.knowledge_search import KnowledgeSearchTool
from tests.contract.memory_fixtures import prepared_knowledge
from tests.contract.support import (
    NOW,
    RUN_ID,
    SESSION_ID,
    agent,
    memory_uow_factory,
    principal,
    run,
)
from tests.gates.test_context_m7 import (
    _budget,
    _PlanStore,
    _StaticMemoryRetriever,
    _StaticQueryFormer,
    _working_state,
)

MANUAL = KnowledgeCatalog(
    lines=(
        KnowledgeCatalogLine(
            title="BMW Owner Manual.pdf",
            doc_date=None,
            ingested_at=datetime(2026, 11, 2, 23, 40, tzinfo=UTC),
            passages=401,
        ),
    ),
    omitted=0,
)
OWNER = principal().model_copy(update={"scopes": {"records.read", "records.write"}})


class _StaticKnowledgeCatalog:
    def __init__(self, catalog: KnowledgeCatalog | None) -> None:
        self.catalog_value = catalog
        self.calls: list[tuple[str | None, Sensitivity, int]] = []

    async def catalog(
        self,
        principal: Principal,
        *,
        current_scope: str | None,
        ceiling: Sensitivity,
        max_documents: int,
    ) -> KnowledgeCatalog | None:
        del principal
        self.calls.append((current_scope, ceiling, max_documents))
        return None if self.catalog_value is None else self.catalog_value.model_copy(deep=True)


class _StaticRecordsCatalog:
    async def catalog(
        self, principal: Principal, *, ceiling: Sensitivity, max_series: int
    ) -> RecordsCatalog | None:
        del principal, ceiling, max_series
        return RecordsCatalog(
            lines=(
                CatalogLine(
                    display_name="Sleep",
                    components=(RecordComponent(name="hours"),),
                    entry_count=0,
                    last_observed_at=None,
                    last_precision=None,
                    cadence=None,
                    due_since=None,
                ),
            ),
            omitted=0,
        )


async def _scope(session_id: UUID) -> str:
    assert session_id == SESSION_ID
    return "general"


def _builder(
    source: KnowledgeCatalogSource | None,
    *,
    roster: bool = True,
    records: RecordsCatalogSource | None = None,
    total_tokens: int = 32_768,
    knowledge_catalog_tokens: int = 400,
    max_documents: int = 12,
    recall: bool = False,
) -> BudgetedContextBuilder:
    estimator = ConservativeTokenEstimator()
    specs = (KnowledgeSearchTool.spec,) if roster else ()
    prefix = build_prefix(agent(), list(specs))
    plan = ContextPlan(
        session_id=SESSION_ID,
        epoch=1,
        prefix_sha256=hashlib.sha256(prefix_bytes(prefix, list(specs))).hexdigest(),
        prefix_tokens=estimator.estimate(prefix, "fake:scripted"),
        model_id="fake:scripted",
        tool_names=tuple(spec.name for spec in specs),
        tool_specs=specs,
        tool_schema_sha256=hashlib.sha256(b"[]").hexdigest(),
        policy_version="adr-0299-test",
        builder_version="context-builder@16",
        budget=_budget().model_copy(
            update={
                "total_tokens": total_tokens,
                "knowledge_catalog_tokens": knowledge_catalog_tokens,
                "records_catalog_tokens": 600,
            }
        ),
        created_at=NOW,
    )
    return BudgetedContextBuilder(
        _PlanStore(plan),
        estimator,
        FixedClock(NOW),
        _working_state(),
        _StaticMemoryRetriever() if recall else None,
        _StaticQueryFormer() if recall else None,
        _scope,
        catalog_source=records,
        knowledge_catalog_source=source,
        knowledge_catalog_max_documents=max_documents,
    )


def _checkpoint(text: str = "What is that clicking sound after I park?") -> RunCheckpoint:
    return RunCheckpoint(
        run_id=RUN_ID,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text=text)])],
        created_at=NOW,
    )


def _body(request: ModelRequest) -> list[ConversationItem]:
    return request.conversation[int(request.metadata["region_a_items"]) :]


def _texts(items: list[ConversationItem]) -> list[str]:
    return [
        part.text
        for item in items
        for part in getattr(item, "content", [])
        if isinstance(part, TextPart)
    ]


def _body_bytes(request: ModelRequest) -> bytes:
    return canonical_json_bytes([item.model_dump(mode="json") for item in _body(request)])


def _index(texts: list[str], marker: str) -> int:
    return next(index for index, text in enumerate(texts) if marker in text)


async def test_catalog_row_bytes_trust_and_position() -> None:
    assert set(REGION_ASSIGNMENTS) == set(ContextClass)
    assert REGION_ASSIGNMENTS[ContextClass.KNOWLEDGE_CATALOG] is ContextRegion.BODY
    active = run(status=RunStatus.RUNNING)

    without = await _builder(None).build(active, _checkpoint(), agent(), OWNER)
    source = _StaticKnowledgeCatalog(MANUAL)
    request = await _builder(source).build(active, _checkpoint(), agent(), OWNER)

    assert source.calls == [("general", Sensitivity.RESTRICTED, 12)]
    body = _body(request)
    texts = _texts(body)
    catalog_texts = [text for text in texts if 'source="knowledge.catalog"' in text]
    assert len(catalog_texts) == 1
    assert catalog_texts[0].startswith(
        '<untrusted trust="internal_tool" source="knowledge.catalog"'
    )
    assert f"\n{render_knowledge_catalog(MANUAL)}\n</untrusted:" in catalog_texts[0]
    assert "BMW Owner Manual.pdf: 401 passages, added 2026-11-02" in catalog_texts[0]
    # Row 13b: in the body, directly ahead of the runtime metadata row, and
    # the prefix never moves.
    assert _index(texts, 'source="knowledge.catalog"') == _index(texts, "Runtime metadata") - 1
    assert request.metadata["prefix_sha256"] == without.metadata["prefix_sha256"]
    assert request.metadata["context_origin_trust"] == TrustLevel.USER.value

    # With both catalogs present the records catalog (row 13a) comes first.
    both = await _builder(source, records=_StaticRecordsCatalog()).build(
        active, _checkpoint(), agent(), OWNER
    )
    both_texts = _texts(_body(both))
    records_index = _index(both_texts, 'source="records.catalog"')
    knowledge_index = _index(both_texts, 'source="knowledge.catalog"')
    assert records_index == knowledge_index - 1
    assert knowledge_index == _index(both_texts, "Runtime metadata") - 1


async def test_catalog_is_absent_without_a_source_a_roster_entry_or_documents() -> None:
    active = run(status=RunStatus.RUNNING)
    baseline = await _builder(None).build(active, _checkpoint(), agent(), OWNER)

    # No documents: the body equals one built without a source, byte for byte.
    empty = _StaticKnowledgeCatalog(None)
    assert _body_bytes(
        await _builder(empty).build(active, _checkpoint(), agent(), OWNER)
    ) == _body_bytes(baseline)
    assert empty.calls == [("general", Sensitivity.RESTRICTED, 12)]

    # The tool is the path: a roster without knowledge.search, in its
    # definitions or its deferred index, reads no catalog.
    unlisted = _StaticKnowledgeCatalog(MANUAL)
    no_roster = await _builder(unlisted, roster=False).build(active, _checkpoint(), agent(), OWNER)
    plain = await _builder(None, roster=False).build(active, _checkpoint(), agent(), OWNER)
    assert _body_bytes(no_roster) == _body_bytes(plain)
    assert unlisted.calls == []

    # A zero class reads nothing either.
    zero = _StaticKnowledgeCatalog(MANUAL)
    capped = await _builder(zero, knowledge_catalog_tokens=0).build(
        active, _checkpoint(), agent(), OWNER
    )
    assert _body_bytes(capped) == _body_bytes(baseline)
    assert zero.calls == []
    none = _StaticKnowledgeCatalog(MANUAL)
    await _builder(none, max_documents=0).build(active, _checkpoint(), agent(), OWNER)
    assert none.calls == []


async def test_catalog_yields_whole_under_pressure() -> None:
    active = run(status=RunStatus.RUNNING)
    source = _StaticKnowledgeCatalog(MANUAL)
    roomy = await _builder(source, recall=True).assemble(active, _checkpoint(), agent(), OWNER)
    bare = await _builder(None, recall=True).assemble(active, _checkpoint(), agent(), OWNER)
    assert roomy.pressure.yield_steps == ()
    catalog_tokens = roomy.pressure.total_tokens - bare.pressure.total_tokens
    assert catalog_tokens > 0

    # A window that holds everything but the catalog: it yields whole, first,
    # and the in-turn recall survives.
    squeezed = await _builder(
        source, recall=True, total_tokens=bare.pressure.total_tokens + catalog_tokens // 2
    ).assemble(active, _checkpoint(), agent(), OWNER)
    assert squeezed.pressure.yield_steps[0] == "knowledge_catalog"
    assert "recall" not in squeezed.pressure.yield_steps
    assert squeezed.pressure.fits
    squeezed_texts = _texts(_body(squeezed.request))
    assert not any("knowledge.catalog" in text for text in squeezed_texts)
    assert not any("BMW Owner Manual" in text for text in squeezed_texts)
    assert any("<memory" in text for text in squeezed_texts)
    assert _body_bytes(squeezed.request) == _body_bytes(bare.request)

    # Tighter still: the catalog is gone before the recall trims.
    crushed = await _builder(
        source, recall=True, total_tokens=bare.pressure.total_tokens - 1
    ).assemble(active, _checkpoint(), agent(), OWNER)
    assert crushed.pressure.yield_steps[:2] == ("knowledge_catalog", "recall")

    # The records catalog is recorded ahead of it when both yield.
    both = await _builder(
        source,
        recall=True,
        records=_StaticRecordsCatalog(),
        total_tokens=bare.pressure.total_tokens - 1,
    ).assemble(active, _checkpoint(), agent(), OWNER)
    assert both.pressure.yield_steps[:3] == ("records_catalog", "knowledge_catalog", "recall")

    # A catalog over its own class cap drops whole, never truncated.
    capped = await _builder(source, recall=True, knowledge_catalog_tokens=1).assemble(
        active, _checkpoint(), agent(), OWNER
    )
    assert capped.pressure.yield_steps == ("knowledge_catalog",)
    assert _body_bytes(capped.request) == _body_bytes(bare.request)


async def test_reader_lists_the_store_and_is_silent_when_it_is_empty() -> None:
    clock, factory = await memory_uow_factory()
    reader = KnowledgeCatalogReader(factory, clock)

    assert (
        await reader.catalog(
            principal(), current_scope=None, ceiling=Sensitivity.RESTRICTED, max_documents=12
        )
        is None
    )

    prepared = prepared_knowledge()
    async with factory() as uow:
        await uow.knowledge.ingest(prepared)
    catalog = await reader.catalog(
        principal(), current_scope=None, ceiling=Sensitivity.RESTRICTED, max_documents=12
    )

    assert catalog is not None
    assert [(line.title, line.passages) for line in catalog.lines] == [
        (prepared.document.title, len(prepared.chunks))
    ]
    assert catalog.lines[0].ingested_at == prepared.document.ingested_at
    assert catalog.omitted == 0
