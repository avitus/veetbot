"""Milestone 18 gate checks for the first-party Gmail MCP servers."""

from __future__ import annotations

from pathlib import Path

from scripts.architecture_checks import architecture_errors

ROOT = Path(__file__).resolve().parents[2]


def test_gmail_mcp_two_way_isolation(tmp_path: Path) -> None:
    """Gate 1: `gmail_mcp` and `agent_core` import nothing from each other."""

    assert not [error for error in architecture_errors(ROOT) if "gmail_mcp" in error]

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    fixtures = {
        "src/agent_core/runtime/leak.py": "from gmail_mcp.server import MODES\nVALUE = MODES\n",
        "src/gmail_mcp/bad.py": (
            "from agent_core.domain.policies import TrustLevel\nVALUE = TrustLevel\n"
        ),
    }
    for relative, content in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    errors = architecture_errors(tmp_path)
    assert any("agent_core imports the gmail_mcp server package" in error for error in errors), (
        errors
    )
    assert any("gmail_mcp imports the agent_core platform package" in error for error in errors), (
        errors
    )
