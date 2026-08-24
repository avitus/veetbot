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


async def test_all_modes_pass_the_shared_contract_suite() -> None:
    """Gate 2: one shared suite, all three modes, one fake Gmail API.

    The complete behavioral contract runs in the contract partition; this
    gate asserts the mechanism — the suite exists, drives every declared
    mode through one fake, and covers the claims the design names — and
    smokes each mode live against the seeded fake.
    """

    import ast

    from gmail_mcp.server import MODES
    from tests.contract.gmail_support import seeded_fake
    from tests.contract.test_gmail_mcp_contract import ROSTERS, server_for

    assert set(ROSTERS) == set(MODES) == {"read", "send", "write"}
    for mode, roster in ROSTERS.items():
        server = server_for(seeded_fake(), mode)
        assert {tool.name for tool in await server.list_tools()} == roster

    suite = ROOT / "tests" / "contract" / "test_gmail_mcp_contract.py"
    functions = {
        node.name
        for node in ast.walk(ast.parse(suite.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert {
        "test_every_mode_advertises_exactly_its_roster",
        "test_search_threads_returns_normalized_records",
        "test_get_thread_reduces_html_and_never_fetches_attachments",
        "test_upstream_failures_map_to_stable_codes",
        "test_the_access_token_never_appears_in_results_or_errors",
        "test_create_draft_stores_a_draft_by_value",
        "test_modify_labels_batches_over_threads",
        "test_trash_and_untrash_round_trip",
        "test_send_message_sends_by_value",
        "test_write_and_send_failures_are_stable_too",
    } <= functions


async def test_roster_confinement_and_no_permanent_deletion() -> None:
    """Gate 3: each mode serves exactly its roster; deletion is not a verb.

    Beyond the advertised names, the confinement is structural: the package
    speaks no `delete` anywhere — no tool, no client method, no Gmail
    endpoint — so permanent deletion cannot return as an implementation
    detail without failing this gate.
    """

    from gmail_mcp.gmail import GmailClient
    from gmail_mcp.server import MODES
    from tests.contract.gmail_support import seeded_fake
    from tests.contract.test_gmail_mcp_contract import server_for

    for mode, roster in MODES.items():
        server = server_for(seeded_fake(), mode)
        assert tuple(sorted(tool.name for tool in await server.list_tools())) == roster
        assert not any("delete" in name for name in roster)
    assert not [name for name in dir(GmailClient) if "delete" in name.lower()]
    for path in sorted((ROOT / "src" / "gmail_mcp").rglob("*.py")):
        assert "delete" not in path.read_text(encoding="utf-8").lower(), path
