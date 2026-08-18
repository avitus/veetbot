"""Linear Alembic revision-graph gate."""

from pathlib import Path
from typing import cast

from sqlalchemy import Table

from agent_core.adapters.persistence.sqlalchemy_models import EventRow
from scripts.architecture_checks import migration_graph_errors

ROOT = Path(__file__).resolve().parents[2]


def test_migration_graph() -> None:
    assert migration_graph_errors(ROOT) == []


def test_event_table_indexes_the_memory_formation_scan() -> None:
    event_table = cast(Table, EventRow.__table__)
    indexes = {tuple(column.name for column in index.columns) for index in event_table.indexes}

    assert ("event_type", "session_id", "sequence") in indexes


def test_migration_graph_reports_multiple_heads(tmp_path: Path) -> None:
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    for revision in ("alpha", "beta"):
        (versions / f"{revision}.py").write_text(
            f'revision = "{revision}"\ndown_revision = None\n', encoding="utf-8"
        )
    revision_module = tmp_path / "src" / "agent_core" / "adapters" / "persistence" / "revision.py"
    revision_module.parent.mkdir(parents=True)
    revision_module.write_text('EXPECTED_REVISION = "alpha"\n', encoding="utf-8")

    assert "migration graph has 2 heads: alpha, beta" in migration_graph_errors(tmp_path)
