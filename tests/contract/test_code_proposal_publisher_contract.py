"""What the proposal adapter may and may not send to the public repository (ADR-0281).

The App opens one pull request from a new `veetbot/` branch to `dev`. It never
moves an existing ref, never merges or reviews, and refuses a bad draft before
any request, including the token request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.cockpit.github import GitHubAppTokens
from agent_core.adapters.code.github import CodeProposalRefusedError, GitHubCodeProposals
from agent_core.adapters.determinism import FixedClock
from agent_core.application.code_proposals import CodeProposalWorker
from agent_core.domain.code import CodeDraft, CodeDraftState, CodeProposal
from agent_core.domain.errors import ConflictError
from agent_core.ports.cockpit import WidgetRepositoryUnavailableError
from tests.contract.fake_github import (
    APP_ID,
    INSTALLATION_ID,
    NAME,
    OWNER,
    FakeGitHubRemote,
    app_private_key,
)

KEY = app_private_key()
NOW = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
DRAFT_ID = UUID("00000000-0000-0000-0000-00000000c0de")
OTHER_DRAFT_ID = UUID("00000000-0000-0000-0000-00000000beef")
BRANCH = "veetbot/fix-widget-0000c0de"
REPOSITORY_PATH = f"/repos/{OWNER}/{NAME}"
TITLE = "Fix the widget"
BODY = "Why the widget kept pausing."
MESSAGE = f"{TITLE}\n\nVeetbot-Code-Draft: {DRAFT_ID}"
BASE_FILES = {"src/a.py": "old\n", "scripts/run.sh": "#!/bin/sh\n", "src/gone.py": "bye\n"}


@dataclass
class ProposalRemote(FakeGitHubRemote):
    """The shared fake plus the blob, short-sha and pull routes proposals use."""

    pulls: list[dict[str, Any]] = field(default_factory=list)
    executable: set[str] = field(default_factory=set)
    # Runs just before a create, standing in for a concurrent worker that won the race.
    before_create_ref: Callable[[], None] | None = None
    before_create_pull: Callable[[], None] | None = None

    def _route(self, request: httpx.Request, path: str, body: Any) -> httpx.Response:
        if not path.startswith(f"{REPOSITORY_PATH}/") or not self._authorized(request):
            return super()._route(request, path, body)
        route = path.removeprefix(REPOSITORY_PATH)
        method = request.method
        if method == "POST" and route == "/git/blobs":
            assert body["encoding"] == "utf-8"
            return httpx.Response(201, json={"sha": self._blob(body["content"])})
        if method == "GET" and route.startswith("/commits/"):
            prefix = route.removeprefix("/commits/")
            matches = [sha for sha in self.commits if sha.startswith(prefix)]
            if len(matches) != 1:
                return httpx.Response(422, json={"message": "No commit found"})
            return httpx.Response(200, json=self._public_commit(matches[0]))
        if method == "GET" and route.startswith("/git/trees/"):
            response = super()._route(request, path, body)
            listing = response.json()
            for entry in listing.get("tree", []):
                if entry["path"] in self.executable:
                    entry["mode"] = "100755"
            return httpx.Response(response.status_code, json=listing)
        if method == "POST" and route == "/git/refs" and self.before_create_ref:
            self.before_create_ref()
        if method == "GET" and route == "/pulls":
            params = request.url.params
            found = [
                pull
                for pull in self.pulls
                if f"{OWNER}:{pull['head']['ref']}" == params["head"]
                and pull["base"]["ref"] == params["base"]
                and pull["state"] == params["state"]
            ]
            return httpx.Response(200, json=found)
        if method == "POST" and route == "/pulls":
            if self.before_create_pull:
                self.before_create_pull()
            return self._open_pull(body)
        return super()._route(request, path, body)

    def _open_pull(self, body: Any) -> httpx.Response:
        head_ref, base_ref = body["head"], body["base"]
        if f"refs/heads/{head_ref}" not in self.refs or f"refs/heads/{base_ref}" not in self.refs:
            return httpx.Response(422, json={"message": "Validation Failed"})
        if any(pull["head"]["ref"] == head_ref and pull["state"] == "open" for pull in self.pulls):
            return httpx.Response(422, json={"message": "A pull request already exists"})
        number = len(self.pulls) + 100
        pull = {
            "number": number,
            "state": "open",
            "title": body["title"],
            "body": body["body"],
            "html_url": f"https://github.com/{OWNER}/{NAME}/pull/{number}",
            "head": {"ref": head_ref},
            "base": {"ref": base_ref},
        }
        self.pulls.append(pull)
        return httpx.Response(201, json=pull)


@dataclass
class Wired:
    remote: ProposalRemote
    adapter: GitHubCodeProposals
    client: httpx.AsyncClient
    base: str


@pytest.fixture
async def wired() -> AsyncIterator[Wired]:
    clock = FixedClock(NOW)
    remote = ProposalRemote(private_key_pem=KEY, now=clock.now)
    base = remote._commit(
        remote._tree({path: remote._blob(text) for path, text in BASE_FILES.items()}),
        [],
        "The release",
    )
    remote.refs["refs/heads/main"] = base
    remote.refs["refs/heads/dev"] = base
    async with httpx.AsyncClient(transport=remote.transport()) as client:
        tokens = GitHubAppTokens(
            client,
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            private_key_pem=KEY,
            clock=clock,
        )
        adapter = GitHubCodeProposals(client, tokens, repository=f"{OWNER}/{NAME}")
        yield Wired(remote, adapter, client, base)


def _draft(
    base: str,
    *,
    files: dict[str, str | None] | None = None,
    branch: str = BRANCH,
    title: str = TITLE,
    draft_id: UUID = DRAFT_ID,
    base_revision: str | None = None,
) -> CodeDraft:
    return CodeDraft(
        id=draft_id,
        tenant_id="tenant",
        principal_id="owner",
        slug="fix-widget",
        base_release="20261101-abcdef0",
        base_revision=base[:7] if base_revision is None else base_revision,
        files={"src/a.py": "new\n", "src/gone.py": None} if files is None else files,
        state=CodeDraftState.PROPOSING,
        version=3,
        proposal=CodeProposal(title=title, body=BODY, branch=branch, next_attempt_at=NOW),
        created_at=NOW,
        updated_at=NOW,
    )


def _writes(remote: FakeGitHubRemote, route: str) -> list[Any]:
    return [
        request.body
        for request in remote.requests
        if request.method == "POST" and request.path == f"{REPOSITORY_PATH}{route}"
    ]


async def test_publish_opens_one_pull_request_from_a_new_branch_based_on_the_release(
    wired: Wired,
) -> None:
    remote = wired.remote
    receipt = await wired.adapter.publish(_draft(wired.base))

    head = remote.refs[f"refs/heads/{BRANCH}"]
    assert receipt.commit_sha == head
    assert receipt.pull_number == remote.pulls[0]["number"]
    assert receipt.pull_url == remote.pulls[0]["html_url"]
    assert remote.commits[head].message == MESSAGE
    assert remote.commits[head].parents == [wired.base]
    assert remote.files_at(head) == {"src/a.py": "new\n", "scripts/run.sh": "#!/bin/sh\n"}
    assert _writes(remote, "/git/blobs") == [{"content": "new\n", "encoding": "utf-8"}]
    pull = remote.pulls[0]
    assert (pull["head"]["ref"], pull["base"]["ref"]) == (BRANCH, "dev")
    assert (pull["title"], pull["body"]) == (TITLE, BODY)


async def test_a_replaced_file_keeps_its_executable_bit(wired: Wired) -> None:
    wired.remote.executable.add("scripts/run.sh")
    await wired.adapter.publish(_draft(wired.base, files={"scripts/run.sh": "#!/bin/sh\necho\n"}))
    [tree] = _writes(wired.remote, "/git/trees")
    assert tree["tree"][0]["mode"] == "100755"


async def test_deleting_a_path_the_release_lacks_is_not_sent(wired: Wired) -> None:
    # GitHub answers 422 to a null sha for a path missing from the base tree.
    await wired.adapter.publish(
        _draft(wired.base, files={"src/a.py": "new\n", "src/never-there.py": None})
    )
    [tree] = _writes(wired.remote, "/git/trees")
    assert [entry["path"] for entry in tree["tree"]] == ["src/a.py"]


async def test_a_draft_that_changes_nothing_on_the_release_is_refused_without_a_write(
    wired: Wired,
) -> None:
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base, files={"src/never-there.py": None}))
    assert _writes(wired.remote, "/git/trees") == []
    assert f"refs/heads/{BRANCH}" not in wired.remote.refs


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "dev",
        "refs/heads/dev",
        "feature/x",
        "veetbot/",
        "veetbot/../main",
        "veetbot/a..b",
        "veetbot//a",
        "veetbot/a/",
        "veetbot/.a",
        "veetbot/a.lock",
        "veetbot/a b",
        "veetbot/a?b",
        "veetbot/a~1",
        "veetbot/a\nb",
    ],
)
async def test_a_bad_branch_is_refused_before_any_request(wired: Wired, branch: str) -> None:
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base, branch=branch))
    assert wired.remote.requests == []


@pytest.mark.parametrize(
    "path",
    [
        ".circleci/config.yml",
        ".github/workflows/ci.yml",
        "deploy/app/release.sh",
        "policy/default.yaml",
        "src/agent_core/policy/scopes.py",
        ".GitHub/workflows/ci.yml",
        "Deploy/app/release.sh",
        "./.github/workflows/ci.yml",
        "src//../.github/x",
        "src/../deploy/x",
        "/etc/passwd",
        "src\\a.py",
        ".git/config",
        "src/.git/hooks/pre-commit",
        "src//a.py",
        "src/a.py/",
        "deploy",
        "src/a\n.py",
    ],
)
async def test_a_bad_path_is_refused_before_any_request(wired: Wired, path: str) -> None:
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base, files={"src/a.py": "new\n", path: "x"}))
    assert wired.remote.requests == []


async def test_a_protected_delete_is_refused_before_any_request(wired: Wired) -> None:
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base, files={"deploy/app/release.sh": None}))
    assert wired.remote.requests == []


async def test_an_empty_draft_is_refused_before_any_request(wired: Wired) -> None:
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base, files={}))
    assert wired.remote.requests == []


async def test_a_draft_without_a_proposal_is_refused_before_any_request(wired: Wired) -> None:
    draft = _draft(wired.base).model_copy(update={"proposal": None})
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(draft)
    assert wired.remote.requests == []


@pytest.mark.parametrize("revision", ["main", "dev", "abc", "../../x", "ABCDEF0", "a" * 41])
async def test_a_base_that_is_not_a_short_sha_is_refused_before_any_request(
    wired: Wired, revision: str
) -> None:
    # `GET /commits/dev` would resolve, so a name must never reach it.
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base, base_revision=revision))
    assert wired.remote.requests == []


async def test_a_title_cannot_forge_a_second_line_or_trailer(wired: Wired) -> None:
    title = f"Fix\n\nVeetbot-Code-Draft: {OTHER_DRAFT_ID}"
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base, title=title))
    assert wired.remote.requests == []


async def test_a_retry_after_the_ref_response_was_lost_reuses_the_branch(wired: Wired) -> None:
    remote = wired.remote
    remote.fail_next("POST", rf"{REPOSITORY_PATH}/git/refs", "lost")
    with pytest.raises(WidgetRepositoryUnavailableError):
        await wired.adapter.publish(_draft(wired.base))
    head = remote.refs[f"refs/heads/{BRANCH}"]
    commits_before = len(_writes(remote, "/git/commits"))

    receipt = await wired.adapter.publish(_draft(wired.base))

    assert receipt.commit_sha == head == remote.refs[f"refs/heads/{BRANCH}"]
    assert len(_writes(remote, "/git/commits")) == commits_before
    assert len(remote.pulls) == 1


async def test_a_retry_after_the_pull_response_was_lost_returns_that_pull(wired: Wired) -> None:
    remote = wired.remote
    remote.fail_next("POST", rf"{REPOSITORY_PATH}/pulls", "lost")
    with pytest.raises(WidgetRepositoryUnavailableError):
        await wired.adapter.publish(_draft(wired.base))
    assert len(remote.pulls) == 1

    receipt = await wired.adapter.publish(_draft(wired.base))

    assert receipt.pull_number == remote.pulls[0]["number"]
    assert len(remote.pulls) == 1
    assert len(_writes(remote, "/pulls")) == 1


async def test_a_retry_after_success_sends_no_write_at_all(wired: Wired) -> None:
    first = await wired.adapter.publish(_draft(wired.base))
    posts = sum(request.method == "POST" for request in wired.remote.requests)
    assert await wired.adapter.publish(_draft(wired.base)) == first
    assert sum(request.method == "POST" for request in wired.remote.requests) == posts


@pytest.mark.parametrize(
    "message",
    [
        "A person's own commit",
        f"{TITLE}\n\nVeetbot-Code-Draft: {OTHER_DRAFT_ID}",
        f"Veetbot-Code-Draft: {DRAFT_ID}\n\nThe trailer must close the message",
    ],
)
async def test_a_branch_whose_head_lacks_this_trailer_is_refused_and_left_alone(
    wired: Wired, message: str
) -> None:
    remote = wired.remote
    foreign = remote._commit(remote.commits[wired.base].tree, [wired.base], message)
    remote.refs[f"refs/heads/{BRANCH}"] = foreign
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base))
    assert remote.refs[f"refs/heads/{BRANCH}"] == foreign
    assert not any(request.method == "POST" for request in remote.requests[1:])
    assert remote.pulls == []


async def test_losing_the_ref_race_to_this_draft_continues_on_that_commit(wired: Wired) -> None:
    remote = wired.remote
    winner = remote._commit(remote.commits[wired.base].tree, [wired.base], MESSAGE)

    def race() -> None:
        remote.refs[f"refs/heads/{BRANCH}"] = winner

    remote.before_create_ref = race
    receipt = await wired.adapter.publish(_draft(wired.base))
    assert receipt.commit_sha == winner == remote.refs[f"refs/heads/{BRANCH}"]


async def test_losing_the_ref_race_to_anything_else_is_refused(wired: Wired) -> None:
    remote = wired.remote
    other = remote._commit(remote.commits[wired.base].tree, [wired.base], "A person's commit")

    def race() -> None:
        remote.refs[f"refs/heads/{BRANCH}"] = other

    remote.before_create_ref = race
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base))
    assert remote.refs[f"refs/heads/{BRANCH}"] == other
    assert remote.pulls == []


async def test_losing_the_pull_race_returns_the_pull_that_won(wired: Wired) -> None:
    remote = wired.remote

    def race() -> None:
        remote.before_create_pull = None
        remote._open_pull({"head": BRANCH, "base": "dev", "title": TITLE, "body": BODY})

    remote.before_create_pull = race
    receipt = await wired.adapter.publish(_draft(wired.base))
    assert receipt.pull_number == remote.pulls[0]["number"]
    assert len(remote.pulls) == 1


async def test_an_outage_is_unavailable_and_carries_no_upstream_text(wired: Wired) -> None:
    wired.remote.outage = True
    with pytest.raises(WidgetRepositoryUnavailableError) as raised:
        await wired.adapter.publish(_draft(wired.base))
    assert "Service Unavailable" not in str(raised.value)


async def test_an_upstream_refusal_names_the_status_not_the_body(wired: Wired) -> None:
    wired.remote.fail_next("POST", rf"{REPOSITORY_PATH}/git/blobs", "unavailable")
    with pytest.raises(WidgetRepositoryUnavailableError) as raised:
        await wired.adapter.publish(_draft(wired.base))
    assert str(raised.value) == "GitHub answered 503 to POST /git/blobs"


async def test_a_rejected_token_is_replaced_once(wired: Wired) -> None:
    await wired.adapter.publish(_draft(wired.base))
    wired.remote._tokens.clear()
    await wired.adapter.publish(_draft(wired.base))
    assert wired.remote.tokens_issued == 2


async def test_the_repository_name_is_checked_when_the_adapter_is_built(wired: Wired) -> None:
    tokens = GitHubAppTokens(
        wired.client,
        app_id=APP_ID,
        installation_id=INSTALLATION_ID,
        private_key_pem=KEY,
        clock=FixedClock(NOW),
    )
    with pytest.raises(ValueError, match="owner/name"):
        GitHubCodeProposals(wired.client, tokens, repository="not-a-repository")


async def test_nothing_ever_targets_main_dev_or_a_merge_or_review_endpoint(wired: Wired) -> None:
    remote = wired.remote
    before = {ref: remote.refs[ref] for ref in ("refs/heads/main", "refs/heads/dev")}

    # The whole surface: a publish, both lost responses, both races and a refusal.
    await wired.adapter.publish(_draft(wired.base))
    await wired.adapter.publish(_draft(wired.base))
    remote.fail_next("POST", rf"{REPOSITORY_PATH}/git/refs", "lost")
    lost_ref = _draft(
        wired.base, branch="veetbot/second-00000001", draft_id=OTHER_DRAFT_ID, files={"b.py": "b"}
    )
    with pytest.raises(WidgetRepositoryUnavailableError):
        await wired.adapter.publish(lost_ref)
    remote.fail_next("POST", rf"{REPOSITORY_PATH}/pulls", "lost")
    with pytest.raises(WidgetRepositoryUnavailableError):
        await wired.adapter.publish(lost_ref)
    await wired.adapter.publish(lost_ref)
    with pytest.raises(CodeProposalRefusedError):
        await wired.adapter.publish(_draft(wired.base, draft_id=UUID(int=9)))

    assert remote.requests
    token_path = f"/app/installations/{INSTALLATION_ID}/access_tokens"
    for request in remote.requests:
        assert request.method in {"GET", "POST"}, request
        assert "merge" not in request.path and "/reviews" not in request.path, request
        assert not request.path.endswith(("/heads/main", "/heads/dev")), request
        assert request.path == token_path or request.path.startswith(REPOSITORY_PATH), request
    assert {request.path.removeprefix(REPOSITORY_PATH) for request in remote.requests} >= {
        "/git/blobs",
        "/git/trees",
        "/git/commits",
        "/git/refs",
        "/pulls",
    }
    created = _writes(remote, "/git/refs")
    assert created and all(body["ref"].startswith("refs/heads/veetbot/") for body in created)
    assert all(body.get("force") is None for body in created)
    assert all(body["base"] == "dev" for body in _writes(remote, "/pulls"))
    assert {ref: remote.refs[ref] for ref in before} == before


class _OneDraftStore:
    """The store's outbox contract for one draft, enough to drive the real worker."""

    def __init__(self, draft: CodeDraft) -> None:
        self.draft = draft

    async def due_proposals(self, now: datetime, *, limit: int) -> list[CodeDraft]:
        proposal = self.draft.proposal
        due = (
            self.draft.state is CodeDraftState.PROPOSING
            and proposal is not None
            and proposal.next_attempt_at <= now
        )
        return [self.draft] if due else []

    async def put(self, draft: CodeDraft, *, expected_version: int | None) -> CodeDraft:
        if expected_version != self.draft.version:
            raise ConflictError("stale")
        self.draft = draft
        return draft


class _OneDraftUow:
    def __init__(self, store: _OneDraftStore) -> None:
        self.code_drafts = store

    async def __aenter__(self) -> _OneDraftUow:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


async def test_the_worker_survives_a_lost_pull_response_and_opens_exactly_one(
    wired: Wired,
) -> None:
    remote = wired.remote
    store = _OneDraftStore(_draft(wired.base))
    clock = FixedClock(NOW)
    worker = CodeProposalWorker(
        uow_factory=lambda: _OneDraftUow(store),  # type: ignore[arg-type]
        clock=clock,
        publisher=wired.adapter,
    )
    remote.fail_next("POST", rf"{REPOSITORY_PATH}/pulls", "lost")

    assert await worker.run_once() == 0
    assert store.draft.proposal is not None
    assert store.draft.proposal.attempts == 1
    assert store.draft.proposal.last_error == "github_unreachable"

    clock.advance(timedelta(minutes=1))
    assert await worker.run_once() == 1
    assert store.draft.state is CodeDraftState.PROPOSED
    assert store.draft.proposal.pull_number == remote.pulls[0]["number"]
    assert store.draft.proposal.commit_sha == remote.refs[f"refs/heads/{BRANCH}"]
    assert len(remote.pulls) == 1
