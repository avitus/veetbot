"""Import-graph enforcement for the modular monolith."""

from pathlib import Path

from scripts.architecture_checks import architecture_errors

ROOT = Path(__file__).resolve().parents[2]


def test_import_boundaries() -> None:
    assert architecture_errors(ROOT) == []


def test_evals_isolation() -> None:
    errors = architecture_errors(ROOT)
    assert not [error for error in errors if "agent_core.evals" in error]


def test_orm_confined() -> None:
    errors = architecture_errors(ROOT)
    assert not [
        error for error in errors if "sqlalchemy" in error.lower() or "ORM type crosses" in error
    ]


def test_boundary_walk_rejects_representative_violations(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    package = tmp_path / "src" / "agent_core"
    fixtures = {
        "domain/bad.py": "import sqlalchemy\n",
        "ports/bad.py": (
            "from typing import Protocol\n"
            "from openai import OpenAI\n"
            "class BadPort(Protocol):\n"
            "    def call(self, client: OpenAI) -> str: ...\n"
        ),
        "application/bad.py": "from fastapi import Request\n",
        "runtime/randomness.py": "from uuid import uuid4 as make_id\nVALUE = make_id()\n",
        "runtime/session.py": (
            "from sqlalchemy.ext.asyncio import create_async_engine as build_engine\n"
            "ENGINE = build_engine('sqlite+aiosqlite://')\n"
        ),
    }
    for relative, content in fixtures.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    errors = architecture_errors(tmp_path)
    assert any("domain imports forbidden dependency sqlalchemy" in error for error in errors)
    assert any("ports imports forbidden dependency openai" in error for error in errors)
    assert any("provider SDK openai crosses adapter boundary" in error for error in errors)
    assert any(
        "runtime/application imports forbidden dependency fastapi" in error for error in errors
    )
    assert any("ambient nondeterminism call uuid.uuid4" in error for error in errors)
    assert any("module-scope database resource" in error for error in errors)
