"""The documents on file as one body-region context item.

The catalog names each current document the run may read, with its dates
and how many passages it holds. It never carries a passage. Rendering is
pure: the same catalog always produces the same bytes (ADR-0299).
"""

from __future__ import annotations

from datetime import UTC
from typing import Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.knowledge import KnowledgeCatalog, KnowledgeCatalogLine
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import TextPart, UserMessage
from agent_core.domain.policies import TrustLevel

CATALOG_SOURCE = "knowledge.catalog"
# The tool is the path to the corpus; a roster without it gets no catalog.
CATALOG_TOOL = "knowledge.search"


class KnowledgeCatalogSource(Protocol):
    async def catalog(
        self,
        principal: Principal,
        *,
        current_scope: str | None,
        ceiling: Sensitivity,
        max_documents: int,
    ) -> KnowledgeCatalog | None: ...


def _line(line: KnowledgeCatalogLine) -> str:
    noun = "passage" if line.passages == 1 else "passages"
    parts = [f"{line.passages} {noun}"]
    if line.doc_date is not None:
        parts.append(f"dated {line.doc_date.isoformat()}")
    parts.append(f"added {line.ingested_at.astimezone(UTC).date().isoformat()}")
    return f"{line.title}: {', '.join(parts)}"


def render_knowledge_catalog(catalog: KnowledgeCatalog) -> str:
    """One line per document in the order given, then the omission line."""

    lines = [_line(line) for line in catalog.lines]
    if catalog.omitted == 1:
        lines.append("1 more document; knowledge.search still reaches it")
    elif catalog.omitted > 1:
        lines.append(f"{catalog.omitted} more documents; knowledge.search still reaches them")
    return "\n".join(lines)


def knowledge_catalog_item(text: str) -> UserMessage:
    return UserMessage(
        content=[TextPart(text=text)],
        trust=TrustLevel.INTERNAL_TOOL,
        principal_id=None,
        source=CATALOG_SOURCE,
    )
