"""ADR-0281: Veetbot reads and searches the source of the release it is running."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.adapters.code.source_tree import FilesystemSourceTree
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult
from agent_core.tools.code import CodeReadTool
from tests.contract.support import principal, tool_context


def _text(result: ToolResult) -> str:
    part = result.content[0]
    assert isinstance(part, TextPart)
    return part.text


@pytest.fixture
def tree(tmp_path: Path) -> FilesystemSourceTree:
    (tmp_path / "src/agent_core").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "deploy").mkdir()
    (tmp_path / "src/agent_core/__pycache__").mkdir()
    (tmp_path / "cockpit/node_modules/x").mkdir(parents=True)
    (tmp_path / "src/agent_core/jobs.py").write_text(
        "".join(f"line {number} maximum_response_bytes\n" for number in range(1, 2001))
    )
    (tmp_path / "docs/jobs.md").write_text("The sample uses GraphQL.\n")
    (tmp_path / "AGENTS.md").write_text("Contract.\n")
    (tmp_path / "src/agent_core/__pycache__/jobs.pyc").write_bytes(b"\x00compiled")
    (tmp_path / "cockpit/node_modules/x/index.js").write_text("maximum_response_bytes\n")
    # What a release keeps beside the tracked source, and must never be served.
    (tmp_path / ".release.env").write_text("VEETBOT_RELEASE_ID=secret-canary\n")
    (tmp_path / ".owner-scopes.env").write_text("AUTH_SCOPES=secret-canary\n")
    (tmp_path / "deploy/veetbot.env").write_text("TOKEN=secret-canary\n")
    (tmp_path / "deploy/veetbot.env.example").write_text("TOKEN=REQUIRED\n")
    (tmp_path / "docs/private.pem").write_text("secret-canary\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv/secret.py").write_text("secret-canary\n")
    (tmp_path / "docs/link.md").symlink_to(tmp_path / ".release.env")
    return FilesystemSourceTree(tmp_path, release_id="20261101-120000-abc1234")


def _context() -> ToolExecutionContext:
    return replace(
        tool_context(), principal=principal().model_copy(update={"scopes": {"code.read"}})
    )


async def test_status_names_the_running_release(tree: FilesystemSourceTree) -> None:
    result = await CodeReadTool(tree).execute({"operation": "status"}, _context())
    assert result.ok and result.structured is not None
    assert result.structured["release_id"] == "20261101-120000-abc1234"
    assert result.structured["revision"] == "abc1234"
    assert result.output_trust is TrustLevel.INTERNAL_TOOL


async def test_read_pages_a_file_by_line_within_the_inline_budget(
    tree: FilesystemSourceTree,
) -> None:
    tool = CodeReadTool(tree)
    first = await tool.execute({"operation": "read", "path": "src/agent_core/jobs.py"}, _context())
    assert first.ok and first.structured is not None
    assert _text(first).startswith("1: line 1 ")
    assert len(_text(first).encode()) <= 3900
    following = first.structured["next_line"]
    assert following > 1
    second = await tool.execute(
        {"operation": "read", "path": "src/agent_core/jobs.py", "start_line": following},
        _context(),
    )
    assert _text(second).startswith(f"{following}: line {following} ")


async def test_list_shows_only_source(tree: FilesystemSourceTree) -> None:
    tool = CodeReadTool(tree)
    root = await tool.execute({"operation": "list", "path": ""}, _context())
    assert root.structured is not None
    names = {entry["name"] for entry in root.structured["entries"]}
    assert {"src", "docs", "AGENTS.md", "cockpit", "deploy"} <= names
    assert not names & {".release.env", ".owner-scopes.env", ".venv"}
    docs = await tool.execute({"operation": "list", "path": "docs"}, _context())
    assert docs.structured is not None
    assert {entry["name"] for entry in docs.structured["entries"]} == {"jobs.md"}


async def test_search_finds_source_and_skips_generated_and_private_files(
    tree: FilesystemSourceTree,
) -> None:
    tool = CodeReadTool(tree)
    found = await tool.execute(
        {"operation": "search", "pattern": "maximum_response_bytes"}, _context()
    )
    assert found.ok and found.structured is not None
    text = _text(found)
    assert "src/agent_core/jobs.py:1:" in text
    assert "node_modules" not in text and "__pycache__" not in text
    assert found.structured["truncated"] is True
    assert len(text.encode()) <= 3900
    secret = await tool.execute({"operation": "search", "pattern": "secret-canary"}, _context())
    assert secret.ok and secret.structured is not None
    assert secret.structured["matches"] == 0
    narrowed = await tool.execute(
        {"operation": "search", "pattern": "GraphQL", "path": "docs"}, _context()
    )
    assert "docs/jobs.md:1:" in _text(narrowed)


async def test_search_treats_a_flag_shaped_pattern_as_text(tree: FilesystemSourceTree) -> None:
    result = await CodeReadTool(tree).execute(
        {"operation": "search", "pattern": "--include=*"}, _context()
    )
    assert result.ok and result.structured is not None
    assert result.structured["matches"] == 0


@pytest.mark.parametrize(
    "path",
    [
        ".release.env",
        ".owner-scopes.env",
        "deploy/veetbot.env",
        "docs/private.pem",
        ".venv/secret.py",
        "docs/link.md",
        "../outside",
        "/etc/passwd",
        "src/../.release.env",
        "src/agent_core/__pycache__/jobs.pyc",
    ],
)
async def test_private_generated_or_escaping_paths_are_refused(
    tree: FilesystemSourceTree, path: str
) -> None:
    result = await CodeReadTool(tree).execute({"operation": "read", "path": path}, _context())
    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "code.path_refused"
    assert "secret-canary" not in str(result)


async def test_a_binary_file_is_refused(tree: FilesystemSourceTree, tmp_path: Path) -> None:
    (tmp_path / "docs/logo.md").write_bytes(b"\x89PNG\x00\x00")
    result = await CodeReadTool(tree).execute(
        {"operation": "read", "path": "docs/logo.md"}, _context()
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "code.not_text"
