"""Linear Alembic revision-graph gate."""

from pathlib import Path

from scripts.architecture_checks import migration_graph_errors

ROOT = Path(__file__).resolve().parents[2]


def test_migration_graph() -> None:
    assert migration_graph_errors(ROOT) == []


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
