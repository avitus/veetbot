"""Linear Alembic revision-graph gate."""

from pathlib import Path

from scripts.architecture_checks import migration_graph_errors

ROOT = Path(__file__).resolve().parents[2]


def test_migration_graph() -> None:
    assert migration_graph_errors(ROOT) == []
