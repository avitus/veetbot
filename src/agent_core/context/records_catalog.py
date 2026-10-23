"""The owner's records catalog as one body-region context item.

The catalog names each numeric series, its entry count, when it was last
observed and whether it is due. It never carries a value. Rendering is pure:
the same catalog, zone and clock always produce the same bytes.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import TextPart, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.domain.records import RecordPrecision

if TYPE_CHECKING:
    from agent_core.domain.records import CatalogLine, RecordsCatalog

CATALOG_SOURCE = "records.catalog"
CATALOG_SCOPE = "records.read"


class RecordsCatalogSource(Protocol):
    async def catalog(
        self, principal: Principal, *, ceiling: Sensitivity, max_series: int
    ) -> RecordsCatalog | None: ...


def _instant(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    offset = local.utcoffset()
    minutes = 0 if offset is None else int(offset.total_seconds()) // 60
    sign = "-" if minutes < 0 else "+"
    hours, remainder = divmod(abs(minutes), 60)
    return f"{local:%Y-%m-%d %H:%M} {sign}{hours:02d}:{remainder:02d}"


def _line(line: CatalogLine, zone: ZoneInfo) -> str:
    components = ", ".join(
        component.name if component.unit is None else f"{component.name} {component.unit}"
        for component in line.components
    )
    parts: list[str] = []
    if line.entry_count == 0 or line.last_observed_at is None:
        parts.append("no entries")
    else:
        noun = "entry" if line.entry_count == 1 else "entries"
        parts.append(f"{line.entry_count} {noun}")
        last = (
            line.last_observed_at.astimezone(zone).date().isoformat()
            if line.last_precision is RecordPrecision.DAY
            else _instant(line.last_observed_at, zone)
        )
        parts.append(f"last {last}")
    if line.cadence is None:
        parts.append("logged ad hoc")
    elif line.due_since is None:
        parts.append(f"due {line.cadence.value}")
    else:
        parts.append(f"due {line.cadence.value} since {_instant(line.due_since, zone)}")
    return f"{line.display_name} ({components}): {', '.join(parts)}"


def render_records_catalog(catalog: RecordsCatalog, *, zone: ZoneInfo, now: datetime) -> str:
    """One line per series in the order given, then the omission line.

    `now` is part of the rendering contract so a caller passes the clock it
    rendered the runtime date with; due state itself arrives precomputed on
    each line, so the text depends only on the catalog and the zone.
    """

    del now
    lines = [_line(line, zone) for line in catalog.lines]
    if catalog.omitted > 0:
        lines.append(f"{catalog.omitted} more series; records.list has them")
    return "\n".join(lines)


def catalog_item(text: str) -> UserMessage:
    return UserMessage(
        content=[TextPart(text=text)],
        trust=TrustLevel.INTERNAL_TOOL,
        principal_id=None,
        source=CATALOG_SOURCE,
    )
