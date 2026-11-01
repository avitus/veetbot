"""ADR-0281: a source tree serves tracked source and nothing that sits beside it."""

from pathlib import Path

import pytest

from agent_core.adapters.code.source_tree import FilesystemSourceTree
from agent_core.ports.code import (
    SourceNotTextError,
    SourcePathRefusedError,
    SourcePatternInvalidError,
    SourceTree,
)


@pytest.fixture
def tree(tmp_path: Path) -> SourceTree:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("limit = 1\n")
    (tmp_path / "src/image.py").write_bytes(b"\x00binary")
    (tmp_path / "AGENTS.md").write_text("Contract.\n")
    (tmp_path / ".release.env").write_text("SECRET=canary\n")
    return FilesystemSourceTree(tmp_path, release_id="20261101-120000-abc1234")


async def test_it_names_its_release_and_reads_text(tree: SourceTree) -> None:
    assert tree.release_id == "20261101-120000-abc1234"
    assert await tree.read_text("src/app.py") == "limit = 1\n"


async def test_it_lists_only_source(tree: SourceTree) -> None:
    names = {entry.name for entry in await tree.list_directory("")}
    assert names == {"src", "AGENTS.md"}


async def test_it_refuses_what_is_not_source(tree: SourceTree) -> None:
    for path in (".release.env", "../outside", "/etc/passwd", "src/missing.py"):
        with pytest.raises(SourcePathRefusedError):
            await tree.read_text(path)
    with pytest.raises(SourceNotTextError):
        await tree.read_text("src/image.py")


def test_it_allows_a_new_source_path_but_not_a_private_one(tree: SourceTree) -> None:
    assert tree.allows("src/new.py")
    assert not tree.allows(".release.env")
    assert not tree.allows("")


async def test_search_is_bounded_and_rejects_a_bad_pattern(tree: SourceTree) -> None:
    found = await tree.search("limit", maximum_bytes=4096)
    assert found.lines == ("src/app.py:1:limit = 1",)
    assert not found.truncated
    assert (await tree.search("SECRET", maximum_bytes=4096)).lines == ()
    with pytest.raises(SourcePatternInvalidError):
        await tree.search("(", maximum_bytes=4096)


async def test_a_search_reads_grep_output_past_its_first_chunk(tmp_path: Path) -> None:
    """Grep writes in chunks; every match counts until the cap, and none is lost silently."""

    (tmp_path / "src").mkdir()
    (tmp_path / "src/many.py").write_text("".join(f"needle {n} {'x' * 90}\n" for n in range(3000)))
    tree = FilesystemSourceTree(tmp_path, release_id="20261101-120000-abc1234")

    found = await tree.search("needle", maximum_bytes=1024 * 1024)

    assert len(found.lines) == 3000
    assert not found.truncated


async def test_a_search_past_the_output_cap_keeps_whole_lines_and_says_so(tmp_path: Path) -> None:
    """A wide match ends at the cap with the matches it read, never a hung grep."""

    (tmp_path / "src").mkdir()
    (tmp_path / "src/many.py").write_text("".join(f"needle {n} {'x' * 90}\n" for n in range(20000)))
    tree = FilesystemSourceTree(tmp_path, release_id="20261101-120000-abc1234")

    found = await tree.search("needle", maximum_bytes=4 * 1024 * 1024)

    assert found.truncated
    assert len(found.lines) > 1000
    # The cap can fall inside a line; a cut line must not be presented as a match.
    assert found.lines[-1].endswith("x" * 90)
