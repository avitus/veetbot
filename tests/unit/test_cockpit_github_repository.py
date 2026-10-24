"""What only the GitHub App adapter can get wrong (docs/plan/cockpit.md, the credential).

The shared behaviour is the repository contract suite; these cases cover the
App token, outages and lost responses, and that nothing out of scope or forced
ever reaches the remote.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.cockpit.github import GitHubAppTokens, GitHubWidgetRepository
from agent_core.adapters.determinism import FixedClock
from agent_core.domain.cockpit import widget_commit_message
from agent_core.ports.cockpit import (
    WidgetPathRefusedError,
    WidgetRepositoryUnavailableError,
)
from tests.contract.fake_github import (
    APP_ID,
    INSTALLATION_ID,
    NAME,
    OWNER,
    FakeGitHubRemote,
    app_private_key,
)

KEY = app_private_key()
REVISION = UUID("00000000-0000-0000-0000-00000000c0de")
FILES = {"widget.json": "{}", "widget.tsx": "export default () => null;"}


def _clock() -> FixedClock:
    return FixedClock(datetime(2026, 10, 23, 18, 0, tzinfo=UTC))


@pytest.fixture
async def wired() -> AsyncIterator[tuple[FakeGitHubRemote, GitHubWidgetRepository, FixedClock]]:
    clock = _clock()
    remote = FakeGitHubRemote(private_key_pem=KEY, now=clock.now)
    async with httpx.AsyncClient(transport=remote.transport()) as client:
        tokens = GitHubAppTokens(
            client,
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            private_key_pem=KEY,
            clock=clock,
        )
        yield remote, GitHubWidgetRepository(client, tokens, repository=f"{OWNER}/{NAME}"), clock


async def test_one_installation_token_serves_requests_until_it_nears_expiry(
    wired: tuple[FakeGitHubRemote, GitHubWidgetRepository, FixedClock],
) -> None:
    remote, repository, clock = wired
    root = await repository.ensure_branch()
    await repository.write_widget(
        "weather",
        FILES,
        parent_sha=root,
        message=widget_commit_message("create", "weather", REVISION),
    )
    assert remote.tokens_issued == 1
    clock.advance(timedelta(minutes=56))
    await repository.head()
    assert remote.tokens_issued == 2


async def test_a_rejected_token_is_replaced_once(
    wired: tuple[FakeGitHubRemote, GitHubWidgetRepository, FixedClock],
) -> None:
    remote, repository, _clock = wired
    await repository.ensure_branch()
    remote._tokens.clear()  # GitHub revoked every installation token.
    assert await repository.head() is not None
    assert remote.tokens_issued == 2


async def test_an_outage_and_a_lost_ref_update_are_unavailable_not_failures(
    wired: tuple[FakeGitHubRemote, GitHubWidgetRepository, FixedClock],
) -> None:
    remote, repository, _clock = wired
    root = await repository.ensure_branch()
    message = widget_commit_message("create", "weather", REVISION)
    remote.outage = True
    with pytest.raises(WidgetRepositoryUnavailableError):
        await repository.write_widget("weather", FILES, parent_sha=root, message=message)
    remote.outage = False
    assert await repository.head() == root

    remote.fail_next("PATCH", r"/repos/.*/git/refs/heads/cockpit/widgets", "lost")
    with pytest.raises(WidgetRepositoryUnavailableError):
        await repository.write_widget("weather", FILES, parent_sha=root, message=message)
    landed = await repository.find_revision(REVISION, limit=10)
    assert landed is not None and landed == await repository.head()


async def test_every_ref_update_is_a_fast_forward_of_the_configured_branch(
    wired: tuple[FakeGitHubRemote, GitHubWidgetRepository, FixedClock],
) -> None:
    remote, repository, _clock = wired
    root = await repository.ensure_branch()
    head = await repository.write_widget(
        "weather",
        FILES,
        parent_sha=root,
        message=widget_commit_message("create", "weather", REVISION),
    )
    await repository.remove_widget(
        "weather", parent_sha=head, message=widget_commit_message("delete", "weather", REVISION)
    )
    writes = [request for request in remote.requests if request.method in {"POST", "PATCH"}]
    updates = [request for request in writes if request.method == "PATCH"]
    assert updates and all(
        request.body == {"sha": request.body["sha"], "force": False} for request in updates
    )
    assert {request.path for request in updates} == {
        f"/repos/{OWNER}/{NAME}/git/refs/heads/cockpit/widgets"
    }
    created = [request for request in writes if request.path.endswith("/git/refs")]
    assert [request.body["ref"] for request in created] == ["refs/heads/cockpit/widgets"]
    tree_paths = {
        entry["path"]
        for request in writes
        if request.path.endswith("/git/trees")
        for entry in request.body["tree"]
    }
    assert tree_paths <= {"cockpit/widgets/README.md"} | {
        f"cockpit/widgets/weather/{name}" for name in FILES
    }


@pytest.mark.parametrize(
    ("slug", "files"),
    [("weather", {"../../.circleci/config.yml": "x"}), ("../weather", FILES), ("weather", {})],
)
async def test_a_refused_write_sends_nothing_to_the_remote(
    wired: tuple[FakeGitHubRemote, GitHubWidgetRepository, FixedClock],
    slug: str,
    files: dict[str, str],
) -> None:
    remote, repository, _clock = wired
    root = await repository.ensure_branch()
    before = len(remote.requests)
    with pytest.raises(WidgetPathRefusedError):
        await repository.write_widget(
            slug,
            files,
            parent_sha=root,
            message=widget_commit_message("create", "weather", REVISION),
        )
    assert remote.requests[before:] == []


@pytest.mark.parametrize("branch", ["main", "dev", "refs/heads/main", "cockpit/../main", ""])
def test_only_a_branch_under_cockpit_can_be_configured(branch: str) -> None:
    client = httpx.AsyncClient()
    tokens = GitHubAppTokens(
        client,
        app_id=APP_ID,
        installation_id=INSTALLATION_ID,
        private_key_pem=KEY,
        clock=_clock(),
    )
    with pytest.raises(ValueError, match="cockpit/"):
        GitHubWidgetRepository(client, tokens, repository=f"{OWNER}/{NAME}", branch=branch)
