"""The knowledge catalog renders titles, dates and passage counts, never a passage."""

from __future__ import annotations

from datetime import UTC, date, datetime

from agent_core.context.knowledge_catalog import (
    knowledge_catalog_item,
    render_knowledge_catalog,
)
from agent_core.domain.knowledge import KnowledgeCatalog, KnowledgeCatalogLine
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel

MANUAL = KnowledgeCatalogLine(
    title="BMW Owner Manual.pdf",
    doc_date=None,
    ingested_at=datetime(2026, 11, 2, 23, 40, tzinfo=UTC),
    passages=401,
)
GUIDE = KnowledgeCatalogLine(
    title="Deployment Guide",
    doc_date=date(2026, 5, 2),
    ingested_at=datetime(2026, 7, 28, 0, 0, tzinfo=UTC),
    passages=12,
)
NOTE = KnowledgeCatalogLine(
    title="Garden note",
    doc_date=None,
    ingested_at=datetime(2026, 10, 1, 7, 0, tzinfo=UTC),
    passages=1,
)


def test_spec_example_lines_render_byte_exactly() -> None:
    catalog = KnowledgeCatalog(lines=(MANUAL, GUIDE, NOTE), omitted=0)

    assert render_knowledge_catalog(catalog) == (
        "BMW Owner Manual.pdf: 401 passages, added 2026-11-02\n"
        "Deployment Guide: 12 passages, dated 2026-05-02, added 2026-07-28\n"
        "Garden note: 1 passage, added 2026-10-01"
    )


def test_the_added_date_is_the_utc_day_of_ingestion() -> None:
    # 23:40 in Los Angeles on the 2nd is the 3rd in UTC; the catalog says the UTC day.
    late = MANUAL.model_copy(update={"ingested_at": datetime(2026, 11, 3, 7, 40, tzinfo=UTC)})

    assert render_knowledge_catalog(KnowledgeCatalog(lines=(late,), omitted=0)) == (
        "BMW Owner Manual.pdf: 401 passages, added 2026-11-03"
    )


def test_omitted_documents_add_a_closing_line() -> None:
    catalog = KnowledgeCatalog(lines=(NOTE,), omitted=3)

    assert render_knowledge_catalog(catalog) == (
        "Garden note: 1 passage, added 2026-10-01\n"
        "3 more documents; knowledge.search still reaches them"
    )


def test_one_omitted_document_is_singular() -> None:
    catalog = KnowledgeCatalog(lines=(NOTE,), omitted=1)

    assert render_knowledge_catalog(catalog).endswith(
        "1 more document; knowledge.search still reaches it"
    )


def test_rendering_is_byte_stable() -> None:
    catalog = KnowledgeCatalog(lines=(MANUAL, GUIDE, NOTE), omitted=2)

    first = render_knowledge_catalog(catalog)
    second = render_knowledge_catalog(catalog.model_copy(deep=True))

    assert first.encode("utf-8") == second.encode("utf-8")


def test_catalog_item_is_internal_tool_with_its_source() -> None:
    item = knowledge_catalog_item("Garden note: 1 passage, added 2026-10-01")

    assert item.trust is TrustLevel.INTERNAL_TOOL
    assert item.source == "knowledge.catalog"
    assert item.principal_id is None
    assert item.content == [TextPart(text="Garden note: 1 passage, added 2026-10-01")]
