"""Job publication reuses Git CAS without writing into the widget namespace."""

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.cockpit.git import FakeWidgetRepository
from agent_core.adapters.cockpit.github import GitHubAppTokens, GitHubWidgetRepository
from agent_core.adapters.determinism import FixedClock
from agent_core.ports.cockpit import BranchMovedError, WidgetPathRefusedError, WidgetRepository
from tests.contract.fake_github import (
    APP_ID,
    INSTALLATION_ID,
    NAME,
    OWNER,
    FakeGitHubRemote,
    app_private_key,
)
from tests.contract.support import NOW

FILES = {"job.json": '{"title":"Count items"}', "main.py": "def main(inputs, job): return 3"}


@pytest.fixture(params=["fake", "github"])
async def repository(request: pytest.FixtureRequest) -> AsyncIterator[WidgetRepository]:
    if request.param == "fake":
        yield FakeWidgetRepository(package_kind="job")
        return
    key = app_private_key()
    clock = FixedClock(NOW)
    remote = FakeGitHubRemote(private_key_pem=key, now=clock.now)
    async with httpx.AsyncClient(transport=remote.transport()) as client:
        tokens = GitHubAppTokens(
            client, app_id=APP_ID, installation_id=INSTALLATION_ID, private_key_pem=key, clock=clock
        )
        yield GitHubWidgetRepository(
            client, tokens, repository=f"{OWNER}/{NAME}", branch="cockpit/jobs", package_kind="job"
        )


async def test_job_files_commit_read_import_and_remove_on_their_own_branch(
    repository: WidgetRepository,
) -> None:
    parent = await repository.ensure_branch()
    revision_id = UUID(int=268)
    sha = await repository.write_widget(
        "count-items",
        FILES,
        parent_sha=parent,
        message=f"Jobs: Create count-items\n\nVeetbot-Revision: {revision_id}\n",
    )
    assert await repository.read_widget("count-items", at=sha) == FILES
    assert await repository.find_revision(revision_id, limit=10) == sha
    [commit] = await repository.commits_since(parent, limit=10)
    assert commit.changed_slugs == ("count-items",)
    assert not commit.outside_widgets
    with pytest.raises(BranchMovedError):
        await repository.write_widget("count-items", FILES, parent_sha=parent, message="Stale")
    deleted = await repository.remove_widget("count-items", parent_sha=sha, message="Archive")
    assert await repository.read_widget("count-items", at=deleted) is None
    assert await repository.read_widget("count-items", at=sha) == FILES


@pytest.mark.parametrize(
    "files", [{"../widget.tsx": "bad"}, {"widget.tsx": "bad"}, {"secrets.json": "bad"}]
)
async def test_job_repository_refuses_other_package_files(
    repository: WidgetRepository, files: dict[str, str]
) -> None:
    parent = await repository.ensure_branch()
    with pytest.raises(WidgetPathRefusedError):
        await repository.write_widget("count-items", files, parent_sha=parent, message="Refused")
    assert await repository.head() == parent
