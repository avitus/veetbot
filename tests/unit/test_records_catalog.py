"""The records catalog renders names, counts, last times and due state, never a value."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_core.context.records_catalog import catalog_item, render_records_catalog
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.records import (
    CatalogLine,
    RecordCadence,
    RecordComponent,
    RecordPrecision,
    RecordsCatalog,
)

LOS_ANGELES = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)

BLOOD_PRESSURE = CatalogLine(
    display_name="blood pressure",
    components=(
        RecordComponent(name="systolic", unit="mmHg"),
        RecordComponent(name="diastolic", unit="mmHg"),
    ),
    entry_count=31,
    last_observed_at=datetime(2026, 10, 21, 14, 40, tzinfo=UTC),
    last_precision=RecordPrecision.EXACT,
    cadence=RecordCadence.DAILY,
    due_since=datetime(2026, 10, 22, 14, 40, tzinfo=UTC),
)
WEIGHT = CatalogLine(
    display_name="weight",
    components=(RecordComponent(name="kg"),),
    entry_count=12,
    last_observed_at=datetime(2026, 10, 19, 19, 0, tzinfo=UTC),
    last_precision=RecordPrecision.DAY,
    cadence=None,
    due_since=None,
)
SLEEP = CatalogLine(
    display_name="sleep",
    components=(RecordComponent(name="hours"),),
    entry_count=0,
    last_observed_at=None,
    last_precision=None,
    cadence=RecordCadence.DAILY,
    due_since=None,
)


def test_spec_example_lines_render_byte_exactly() -> None:
    catalog = RecordsCatalog(lines=(BLOOD_PRESSURE, WEIGHT, SLEEP), omitted=0)

    assert render_records_catalog(catalog, zone=LOS_ANGELES, now=NOW) == (
        "blood pressure (systolic mmHg, diastolic mmHg): 31 entries, "
        "last 2026-10-21 07:40 -07:00, due daily since 2026-10-22 07:40 -07:00\n"
        "weight (kg): 12 entries, last 2026-10-19, logged ad hoc\n"
        "sleep (hours): no entries, due daily"
    )


def test_utc_renders_with_a_zero_offset() -> None:
    catalog = RecordsCatalog(lines=(BLOOD_PRESSURE,), omitted=0)

    assert render_records_catalog(catalog, zone=ZoneInfo("UTC"), now=NOW) == (
        "blood pressure (systolic mmHg, diastolic mmHg): 31 entries, "
        "last 2026-10-21 14:40 +00:00, due daily since 2026-10-22 14:40 +00:00"
    )


def test_weekly_cadence_not_yet_elapsed_has_no_since() -> None:
    line = WEIGHT.model_copy(
        update={"cadence": RecordCadence.WEEKLY, "last_precision": RecordPrecision.EXACT}
    )

    assert render_records_catalog(
        RecordsCatalog(lines=(line,), omitted=0), zone=ZoneInfo("UTC"), now=NOW
    ) == ("weight (kg): 12 entries, last 2026-10-19 19:00 +00:00, due weekly")


def test_one_entry_is_singular() -> None:
    line = WEIGHT.model_copy(update={"entry_count": 1})

    assert render_records_catalog(
        RecordsCatalog(lines=(line,), omitted=0), zone=ZoneInfo("UTC"), now=NOW
    ) == ("weight (kg): 1 entry, last 2026-10-19, logged ad hoc")


def test_omitted_series_add_a_closing_line() -> None:
    catalog = RecordsCatalog(lines=(SLEEP,), omitted=3)

    assert render_records_catalog(catalog, zone=ZoneInfo("UTC"), now=NOW) == (
        "sleep (hours): no entries, due daily\n3 more series; records.list has them"
    )


def test_rendering_is_byte_stable() -> None:
    catalog = RecordsCatalog(lines=(BLOOD_PRESSURE, WEIGHT, SLEEP), omitted=2)

    first = render_records_catalog(catalog, zone=LOS_ANGELES, now=NOW)
    second = render_records_catalog(catalog.model_copy(deep=True), zone=LOS_ANGELES, now=NOW)

    assert first.encode("utf-8") == second.encode("utf-8")


def test_rendering_is_byte_stable_across_hash_seeds() -> None:
    """The catalog must render byte-identically across calls (owner-records.md:905),
    including across processes with different hash seeds, so set-ordered iteration
    in the renderer cannot leak into the frozen context prefix."""
    script = (
        "from tests.unit.test_records_catalog import BLOOD_PRESSURE, LOS_ANGELES\n"
        "from tests.unit.test_records_catalog import NOW, SLEEP, WEIGHT\n"
        "from agent_core.context.records_catalog import render_records_catalog\n"
        "from agent_core.domain.records import RecordsCatalog\n"
        "catalog = RecordsCatalog(lines=(SLEEP, WEIGHT, BLOOD_PRESSURE), omitted=2)\n"
        "print(render_records_catalog(catalog, zone=LOS_ANGELES, now=NOW), end='')\n"
    )
    renderings = set()
    for seed in ("0", "4242"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONHASHSEED": seed},
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        renderings.add(completed.stdout.encode("utf-8"))
    assert len(renderings) == 1


def test_catalog_item_is_internal_tool_with_its_source() -> None:
    item = catalog_item("sleep (hours): no entries, due daily")

    assert item.trust is TrustLevel.INTERNAL_TOOL
    assert item.source == "records.catalog"
    assert item.principal_id is None
    assert item.content == [TextPart(text="sleep (hours): no entries, due daily")]
