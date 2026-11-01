"""Veetbot's pull request proposals through the GitHub App (ADR-0281).

Only the maintenance role holds the App's private key. A proposal creates new
Git objects, one new `veetbot/` branch and one pull request to `dev`. The adapter
has no call that moves an existing ref, merges or reviews, and a bad draft is
refused before any request. A retry after a lost response recognizes its own
branch by the commit trailer and refuses a branch it did not write.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx

from agent_core.adapters.cockpit.github import GITHUB_API_URL, GitHubAppTokens
from agent_core.domain.code import (
    BASE_BRANCH,
    BRANCH_PREFIX,
    CodeDraft,
    CodeProposal,
    CodeReceipt,
    path_is_protected,
)
from agent_core.domain.errors import ConflictError
from agent_core.ports.cockpit import WidgetRepositoryUnavailableError

DRAFT_TRAILER = "Veetbot-Code-Draft"
_REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_BRANCH_TAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
# Only hexadecimal: `GET /commits/dev` would resolve a branch name as a base.
_BASE_REVISION = re.compile(r"[0-9a-f]{7,40}")
_EXECUTABLE = "100755"
_REGULAR = "100644"


class CodeProposalRefusedError(ConflictError):
    """The draft or the branch it names may not be published; no retry changes that."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="code_proposal_refused")


def _branch_allowed(branch: str) -> bool:
    """Whether a branch sits under the Veetbot prefix and is a valid ref name."""
    if not branch.startswith(BRANCH_PREFIX) or ".." in branch:
        return False
    tail = branch.removeprefix(BRANCH_PREFIX)
    named = f"{tail}/"
    return _BRANCH_TAIL.fullmatch(tail) is not None and ".lock/" not in named and "./" not in named


def _path_allowed(path: str) -> bool:
    """A plain repository path outside what decides credentials or platform rules.

    Lowercased because the owner's Mac folds case, and with a trailing slash so
    a file named exactly like a protected directory is refused too.
    """

    if not path.isprintable() or path.startswith("/") or "\\" in path or ".." in path:
        return False
    if any(part in {"", "."} or part.lower() == ".git" for part in path.split("/")):
        return False
    return not path_is_protected(f"{path.lower()}/")


def _checked_proposal(draft: CodeDraft) -> CodeProposal:
    """Everything that can be refused without asking GitHub."""

    proposal = draft.proposal
    if proposal is None:
        raise CodeProposalRefusedError("The draft has no approved proposal.")
    if not draft.files:
        raise CodeProposalRefusedError("The draft changes nothing.")
    if not _branch_allowed(proposal.branch):
        raise CodeProposalRefusedError(f"A proposal branch is a new {BRANCH_PREFIX} branch.")
    if not all(_path_allowed(path) for path in draft.files):
        raise CodeProposalRefusedError("The draft names a protected or unsafe path.")
    if _BASE_REVISION.fullmatch(draft.base_revision) is None:
        raise CodeProposalRefusedError("The draft's base is not a commit id.")
    # One line, so a title cannot forge the trailer that identifies a branch.
    if len(proposal.title.splitlines()) != 1:
        raise CodeProposalRefusedError("A proposal title is one line.")
    return proposal


def _commit_message(title: str, draft_id: UUID) -> str:
    return f"{title}\n\n{DRAFT_TRAILER}: {draft_id}"


def _carries_trailer(message: str, draft_id: UUID) -> bool:
    return message.rstrip().rsplit("\n", 1)[-1] == f"{DRAFT_TRAILER}: {draft_id}"


class GitHubCodeProposals:
    """The `CodeProposalPublisher` port over one repository."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens: GitHubAppTokens,
        *,
        repository: str,
        api_url: str = GITHUB_API_URL,
    ) -> None:
        """Bind the GitHub client to its configured credentials and repository."""
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("the proposal repository is owner/name")
        self._client = client
        self._tokens = tokens
        self._owner = repository.split("/", 1)[0]
        self._repo_url = f"{api_url.rstrip('/')}/repos/{repository}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object = None,
        params: Mapping[str, str | int] | None = None,
        accept: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        """Send one repository request, replacing a rejected token once."""
        for attempt in range(2):
            token = await self._tokens.token()
            try:
                response = await self._client.request(
                    method,
                    f"{self._repo_url}{path}",
                    json=json_body,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
            except httpx.HTTPError as error:
                raise WidgetRepositoryUnavailableError(
                    f"GitHub did not answer {method} {path}"
                ) from error
            if response.status_code == 401 and attempt == 0:
                self._tokens.invalidate()
                continue
            if response.status_code in accept:
                return response
            raise WidgetRepositoryUnavailableError(
                f"GitHub answered {response.status_code} to {method} {path}"
            )
        raise WidgetRepositoryUnavailableError("GitHub rejected a fresh installation token")

    async def publish(self, draft: CodeDraft) -> CodeReceipt:
        """Open the draft's pull request, or find the one an earlier attempt opened."""
        proposal = _checked_proposal(draft)
        commit_sha = await self._own_head(draft.id, proposal.branch)
        if commit_sha is None:
            commit_sha = await self._create_branch(draft, proposal)
        pull = await self._open_pull(proposal)
        return CodeReceipt(
            commit_sha=commit_sha,
            pull_number=int(pull["number"]),
            pull_url=str(pull["html_url"]),
        )

    async def _own_head(self, draft_id: UUID, branch: str) -> str | None:
        """The branch's head if this draft wrote it, None if the branch is free."""
        ref = await self._request("GET", f"/git/ref/heads/{branch}", accept=(200, 404))
        if ref.status_code == 404:
            return None
        sha = str(ref.json()["object"]["sha"])
        commit = await self._request("GET", f"/git/commits/{sha}")
        if not _carries_trailer(str(commit.json()["message"]), draft_id):
            raise CodeProposalRefusedError("The branch already exists and is not this draft's.")
        return sha

    async def _create_branch(self, draft: CodeDraft, proposal: CodeProposal) -> str:
        """Commit the draft on the release's base and create its new branch."""
        base = await self._request("GET", f"/commits/{draft.base_revision}")
        body = base.json()
        base_sha = str(body["sha"])
        if not base_sha.startswith(draft.base_revision):
            raise CodeProposalRefusedError("GitHub resolved the draft's base to another commit.")
        tree_sha = str(body["commit"]["tree"]["sha"])
        tree = await self._request(
            "POST",
            "/git/trees",
            json_body={"base_tree": tree_sha, "tree": await self._entries(draft, tree_sha)},
            accept=(201,),
        )
        commit = await self._request(
            "POST",
            "/git/commits",
            json_body={
                "message": _commit_message(proposal.title, draft.id),
                "tree": tree.json()["sha"],
                "parents": [base_sha],
            },
            accept=(201,),
        )
        sha = str(commit.json()["sha"])
        created = await self._request(
            "POST",
            "/git/refs",
            json_body={"ref": f"refs/heads/{proposal.branch}", "sha": sha},
            accept=(201, 422),
        )
        if created.status_code == 201:
            return sha
        # A concurrent attempt may have won; anything else on the branch is refused.
        head = await self._own_head(draft.id, proposal.branch)
        if head is None:
            raise WidgetRepositoryUnavailableError("GitHub answered 422 to POST /git/refs")
        return head

    async def _entries(self, draft: CodeDraft, tree_sha: str) -> list[dict[str, Any]]:
        """Tree entries for what the draft really changes, keeping an executable bit.

        GitHub answers 422 to a null sha for a path the base lacks, and such a
        delete is already satisfied, so it is not sent.
        """

        listing = await self._request("GET", f"/git/trees/{tree_sha}", params={"recursive": "1"})
        body = listing.json()
        if body.get("truncated"):
            raise WidgetRepositoryUnavailableError("the release tree is too large to list")
        modes = {str(item["path"]): str(item["mode"]) for item in body["tree"]}
        entries: list[dict[str, Any]] = []
        for path, content in sorted(draft.files.items()):
            if content is not None:
                blob = await self._request(
                    "POST",
                    "/git/blobs",
                    json_body={"content": content, "encoding": "utf-8"},
                    accept=(201,),
                )
                mode = _EXECUTABLE if modes.get(path) == _EXECUTABLE else _REGULAR
                sha: str | None = str(blob.json()["sha"])
                entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
            elif path in modes:
                entries.append({"path": path, "mode": _REGULAR, "type": "blob", "sha": None})
        if not entries:
            raise CodeProposalRefusedError("The draft changes nothing on its base.")
        return entries

    async def _find_pull(self, branch: str) -> dict[str, Any] | None:
        """The open pull request from this branch to the base branch, if any."""
        response = await self._request(
            "GET",
            "/pulls",
            params={"head": f"{self._owner}:{branch}", "base": BASE_BRANCH, "state": "open"},
        )
        found = response.json()
        return dict(found[0]) if found else None

    async def _open_pull(self, proposal: CodeProposal) -> dict[str, Any]:
        """Return the branch's pull request, opening it only when none exists."""
        existing = await self._find_pull(proposal.branch)
        if existing is not None:
            return existing
        created = await self._request(
            "POST",
            "/pulls",
            json_body={
                "title": proposal.title,
                "body": proposal.body,
                "head": proposal.branch,
                "base": BASE_BRANCH,
            },
            accept=(201, 422),
        )
        if created.status_code == 201:
            return dict(created.json())
        # A concurrent attempt may have opened it. Otherwise the 422 is the answer.
        existing = await self._find_pull(proposal.branch)
        if existing is None:
            raise WidgetRepositoryUnavailableError("GitHub answered 422 to POST /pulls")
        return existing
