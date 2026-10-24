"""Widget repository contract: the public branch, one widget directory, fast-forward only.

Both adapters pass it: the fake repository, and the GitHub App adapter against
the in-memory GitHub remote in `fake_github.py`, which enforces fast-forward
ref updates and installation tokens the way GitHub does (docs/plan/cockpit.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.cockpit.git import FakeWidgetRepository
from agent_core.adapters.cockpit.github import GitHubAppTokens, GitHubWidgetRepository
from agent_core.adapters.determinism import FixedClock
from agent_core.domain.cockpit import WIDGET_BRANCH_README, commit_trailers, widget_commit_message
from agent_core.ports.cockpit import BranchMovedError, WidgetPathRefusedError, WidgetRepository
from tests.contract.fake_github import (
    APP_ID,
    INSTALLATION_ID,
    NAME,
    OWNER,
    FakeGitHubRemote,
    app_private_key,
)

REVISION_1 = UUID("00000000-0000-0000-0000-00000000c001")
REVISION_2 = UUID("00000000-0000-0000-0000-00000000c002")
FILES = {
    "widget.json": "{}",
    "widget.tsx": "export default function W() { return null; }",
    "fixtures/typical.json": "{}",
}
KEY = app_private_key()


@dataclass
class Harness:
    repository: WidgetRepository
    commit_as_person: Callable[..., str]
    tree: Callable[[str], Mapping[str, str]]


@pytest.fixture(params=["fake", "github"])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    if request.param == "fake":
        fake = FakeWidgetRepository()
        yield Harness(fake, fake.commit_as_person, fake.tree)
        return
    clock = FixedClock(datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
    remote = FakeGitHubRemote(private_key_pem=KEY, now=clock.now)
    async with httpx.AsyncClient(transport=remote.transport()) as client:
        tokens = GitHubAppTokens(
            client,
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            private_key_pem=KEY,
            clock=clock,
        )
        repository = GitHubWidgetRepository(client, tokens, repository=f"{OWNER}/{NAME}")
        yield Harness(repository, remote.commit_as_person, remote.files_at)


async def _branch(harness: Harness) -> tuple[WidgetRepository, str]:
    return harness.repository, await harness.repository.ensure_branch()


async def test_the_branch_is_created_once_with_its_readme(harness: Harness) -> None:
    repository = harness.repository
    assert await repository.head() is None
    root = await repository.ensure_branch()
    assert await repository.ensure_branch() == root
    assert await repository.head() == root
    [commit] = await repository.commits_since(None, limit=10)
    assert (commit.sha, commit.parent_sha, commit.changed_slugs) == (root, None, ())
    assert dict(harness.tree(root)) == {"cockpit/widgets/README.md": WIDGET_BRANCH_README}


async def test_a_write_is_a_fast_forward_inside_one_widget_directory(harness: Harness) -> None:
    repository, root = await _branch(harness)
    message = widget_commit_message("create", "weather", REVISION_1)
    head = await repository.write_widget("weather", FILES, parent_sha=root, message=message)
    assert await repository.head() == head
    assert await repository.read_widget("weather", at=head) == FILES
    assert await repository.read_widget("weather", at=root) is None
    assert set(harness.tree(head)) == {
        "cockpit/widgets/README.md",
        *(f"cockpit/widgets/weather/{name}" for name in FILES),
    }
    [commit] = await repository.commits_since(root, limit=10)
    assert commit.parent_sha == root
    assert commit.changed_slugs == ("weather",)
    assert commit.outside_widgets is False
    assert dict(commit.trailers) == commit_trailers(message)


async def test_a_write_replaces_the_widget_directory_exactly(harness: Harness) -> None:
    repository, root = await _branch(harness)
    first = await repository.write_widget(
        "weather",
        FILES,
        parent_sha=root,
        message=widget_commit_message("create", "weather", REVISION_1),
    )
    smaller = {"widget.json": "{}", "widget.tsx": "export default () => null;"}
    second = await repository.write_widget(
        "weather",
        smaller,
        parent_sha=first,
        message=widget_commit_message("update", "weather", REVISION_2),
    )
    assert await repository.read_widget("weather", at=second) == smaller
    assert await repository.read_widget("weather", at=first) == FILES


async def test_a_stale_parent_never_overwrites_the_branch(harness: Harness) -> None:
    repository, root = await _branch(harness)
    head = await repository.write_widget(
        "weather",
        FILES,
        parent_sha=root,
        message=widget_commit_message("create", "weather", REVISION_1),
    )
    with pytest.raises(BranchMovedError) as error:
        await repository.write_widget(
            "markets",
            FILES,
            parent_sha=root,
            message=widget_commit_message("create", "markets", REVISION_2),
        )
    assert error.value.head == head
    with pytest.raises(BranchMovedError):
        await repository.remove_widget(
            "weather",
            parent_sha=root,
            message=widget_commit_message("delete", "weather", REVISION_2),
        )
    assert await repository.head() == head


@pytest.mark.parametrize(
    ("slug", "files"),
    [
        ("Weather", FILES),
        ("../escape", FILES),
        ("weather", {"../../src/agent_core/x.py": "x"}),
        ("weather", {"/etc/passwd": "x"}),
        ("weather", {"fixtures\\typical.json": "{}"}),
        ("weather", {"nested/deeper/widget.tsx": "x"}),
        ("weather", {"widget.json": "{}", "script.sh": "rm -rf /"}),
        ("weather", {}),
    ],
)
async def test_paths_outside_one_widget_are_refused_before_anything_is_written(
    harness: Harness, slug: str, files: dict[str, str]
) -> None:
    repository, root = await _branch(harness)
    with pytest.raises(WidgetPathRefusedError):
        await repository.write_widget(
            slug,
            files,
            parent_sha=root,
            message=widget_commit_message("create", "weather", REVISION_1),
        )
    assert await repository.head() == root


async def test_removal_deletes_the_directory_and_history_keeps_it(harness: Harness) -> None:
    repository, root = await _branch(harness)
    written = await repository.write_widget(
        "weather",
        FILES,
        parent_sha=root,
        message=widget_commit_message("create", "weather", REVISION_1),
    )
    removed = await repository.remove_widget(
        "weather",
        parent_sha=written,
        message=widget_commit_message("delete", "weather", REVISION_2),
    )
    assert await repository.read_widget("weather", at=removed) is None
    assert await repository.read_widget("weather", at=written) == FILES
    with pytest.raises(WidgetPathRefusedError):
        await repository.remove_widget(
            "markets",
            parent_sha=removed,
            message=widget_commit_message("delete", "markets", REVISION_2),
        )


async def test_human_commits_are_listed_with_what_they_touched(harness: Harness) -> None:
    repository, root = await _branch(harness)
    human = harness.commit_as_person(
        {
            "cockpit/widgets/markets/widget.tsx": "export default () => null;",
            "cockpit/widgets/weather/widget.json": "{}",
            "src/agent_core/sneaky.py": "x",
        },
        message="tweak two widgets\n",
    )
    [commit] = await repository.commits_since(root, limit=10)
    assert commit.sha == human
    assert commit.changed_slugs == ("markets", "weather")
    assert commit.outside_widgets is True
    assert dict(commit.trailers) == {}


async def test_a_lost_response_is_found_by_its_revision_trailer(harness: Harness) -> None:
    repository, root = await _branch(harness)
    first = await repository.write_widget(
        "weather",
        FILES,
        parent_sha=root,
        message=widget_commit_message("create", "weather", REVISION_1),
    )
    harness.commit_as_person({"cockpit/widgets/markets/widget.json": "{}"}, message="human\n")
    assert await repository.find_revision(REVISION_1, limit=10) == first
    assert await repository.find_revision(REVISION_2, limit=10) is None
    assert await repository.find_revision(REVISION_1, limit=1) is None
    oldest_first = await repository.commits_since(None, limit=10)
    assert [commit.sha for commit in oldest_first][:2] == [root, first]
    assert len(await repository.commits_since(None, limit=2)) == 2


async def test_history_from_a_commit_not_on_the_branch_is_refused(harness: Harness) -> None:
    repository, root = await _branch(harness)
    with pytest.raises(BranchMovedError) as error:
        await repository.commits_since("f" * 40, limit=10)
    assert error.value.head == root
