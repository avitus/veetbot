"""The GitHub App widget repository (docs/plan/cockpit.md, the repository branch).

Only the maintenance role holds the App's private key. It mints a short-lived
App JWT with `cryptography`, exchanges it for an installation token, and writes
through the Git Data API: a tree based on the head's tree that changes only one
widget directory, a commit whose parent is the expected head, and a ref update
without `force`. Paths, the slug and the ref are checked before any request, so
nothing outside `cockpit/widgets/<slug>/` on the configured branch is ever sent.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import struct
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from agent_core.adapters.cockpit.git import BRANCH_ROOT_MESSAGE, touched_widgets
from agent_core.domain.cockpit import (
    WIDGET_BRANCH_DIRECTORY,
    WIDGET_BRANCH_README,
    commit_trailers,
    is_widget_file,
    is_widget_slug,
)
from agent_core.ports.cockpit import (
    BranchMovedError,
    WidgetCommit,
    WidgetPathRefusedError,
    WidgetRepositoryUnavailableError,
)
from agent_core.ports.determinism import Clock

GITHUB_API_URL = "https://api.github.com"
DEFAULT_WIDGET_BRANCH = "cockpit/widgets"
_REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
# The adapter only ever touches a branch under cockpit/; rulesets protect the rest.
_BRANCH = re.compile(r"^cockpit/[a-z0-9][a-z0-9-]{0,62}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_JWT_LIFETIME = timedelta(minutes=9)
_CLOCK_SKEW = timedelta(seconds=60)
_TOKEN_MARGIN = timedelta(minutes=5)
_PAGE = 100
# GitHub lists at most 300 files for one commit; a larger commit is out of scope.
_COMMIT_FILES_CAP = 300


def _access_acl(path: Path) -> bytes | None:
    """The file's POSIX access ACL, where the platform keeps one, else None."""

    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return None
    try:
        acl: bytes = getxattr(path, "system.posix_acl_access")
    except OSError:
        return None
    return acl


# `system.posix_acl_access` (linux/posix_acl_xattr.h): version 2, then entries.
_ACL_VERSION = 2
_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_GROUP, _ACL_MASK, _ACL_OTHER = (
    0x01,
    0x02,
    0x04,
    0x08,
    0x10,
    0x20,
)
# An ACL behind group-class bits always carries these; one without them is incomplete.
_ACL_REQUIRED = frozenset({_ACL_USER_OBJ, _ACL_GROUP_OBJ, _ACL_MASK, _ACL_OTHER})


def _private(mode: int, acl: bytes | None, reader_uid: int, owner_uid: int) -> bool:
    """Readable by its owner and, through an ACL, the service's own user, and no one else.

    systemd's credential copy is root-owned with an ACL naming the unit's user,
    so `stat` shows the ACL mask in the group bits. Group bits without an ACL
    are the group's own access, and an ACL-backed key must belong to root or
    the reader, whose owner entry would otherwise be one more reader.
    """

    if mode & 0o007:
        return False
    if not mode & 0o070:
        return True
    # The owner entry is one more reader, so it must be root or the reader itself.
    if owner_uid not in (0, reader_uid):
        return False
    if acl is None or len(acl) < 4 or (len(acl) - 4) % 8:
        return False
    if struct.unpack_from("<I", acl)[0] != _ACL_VERSION:
        return False
    tags: set[int] = set()
    for offset in range(4, len(acl), 8):
        tag, perm, ident = struct.unpack_from("<HHI", acl, offset)
        tags.add(tag)
        if tag == _ACL_USER and perm and ident != reader_uid:
            return False
        if tag in (_ACL_GROUP_OBJ, _ACL_GROUP, _ACL_OTHER) and perm:
            return False
    return tags >= _ACL_REQUIRED


def load_app_private_key(
    path: Path,
    *,
    acl_reader: Callable[[Path], bytes | None] = _access_acl,
    reader_uid: int | None = None,
) -> bytes:
    """The App's key as systemd's `LoadCredential` delivers it: private and present."""

    if not path.is_absolute() or path.is_symlink():
        raise ValueError("must be an absolute path to a private regular file")
    try:
        metadata = path.stat()
        key = path.read_bytes()
    except OSError as error:
        raise ValueError("is unavailable") from error
    uid = os.geteuid() if reader_uid is None else reader_uid
    if not stat.S_ISREG(metadata.st_mode) or not _private(
        stat.S_IMODE(metadata.st_mode), acl_reader(path), uid, metadata.st_uid
    ):
        raise ValueError("must be a private regular file readable only by its owner")
    if not key.strip():
        raise ValueError("is empty; install the GitHub App's private key")
    return key


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class GitHubAppTokens:
    """Installation tokens for one App installation, minted on demand and cached."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        app_id: int,
        installation_id: int,
        private_key_pem: bytes,
        clock: Clock,
        api_url: str = GITHUB_API_URL,
    ) -> None:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("a GitHub App key is an RSA private key")
        self._client = client
        self._app_id = app_id
        self._installation_id = installation_id
        self._key = key
        self._api_url = api_url.rstrip("/")
        self._clock = clock
        self._token: str | None = None
        self._expires_at = datetime.min.replace(tzinfo=UTC)

    def _app_jwt(self) -> str:
        issued = self._clock.now() - _CLOCK_SKEW
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = {
            "iat": int(issued.timestamp()),
            "exp": int((issued + _JWT_LIFETIME).timestamp()),
            "iss": str(self._app_id),
        }
        payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64url(signature)}"

    def invalidate(self) -> None:
        self._token = None

    async def token(self) -> str:
        if self._token is not None and self._clock.now() < self._expires_at - _TOKEN_MARGIN:
            return self._token
        url = f"{self._api_url}/app/installations/{self._installation_id}/access_tokens"
        try:
            response = await self._client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx.HTTPError as error:
            raise WidgetRepositoryUnavailableError("GitHub did not issue a token") from error
        if response.status_code != 201:
            raise WidgetRepositoryUnavailableError(
                f"GitHub refused an installation token ({response.status_code})"
            )
        body = response.json()
        self._token = str(body["token"])
        self._expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
        return self._token


class GitHubWidgetRepository:
    """The `WidgetRepository` port over one repository's widget branch."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens: GitHubAppTokens,
        *,
        repository: str,
        branch: str = DEFAULT_WIDGET_BRANCH,
        api_url: str = GITHUB_API_URL,
    ) -> None:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("the widget repository is owner/name")
        if not _BRANCH.fullmatch(branch):
            raise ValueError("the widget branch must be a branch under cockpit/")
        self._client = client
        self._tokens = tokens
        self._repo_url = f"{api_url.rstrip('/')}/repos/{repository}"
        self._branch = branch

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object = None,
        params: Mapping[str, str | int] | None = None,
        accept: tuple[int, ...] = (200,),
    ) -> httpx.Response:
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

    async def head(self) -> str | None:
        response = await self._request(
            "GET", f"/git/ref/heads/{self._branch}", accept=(200, 404, 409)
        )
        if response.status_code != 200:
            return None
        return str(response.json()["object"]["sha"])

    async def _commit_tree(self, sha: str) -> str:
        response = await self._request("GET", f"/git/commits/{sha}")
        return str(response.json()["tree"]["sha"])

    async def _tree_paths(self, tree_sha: str) -> dict[str, str]:
        response = await self._request("GET", f"/git/trees/{tree_sha}", params={"recursive": "1"})
        body = response.json()
        if body.get("truncated"):
            raise WidgetRepositoryUnavailableError("the widget branch tree is too large to list")
        return {
            str(entry["path"]): str(entry["sha"])
            for entry in body["tree"]
            if entry.get("type") == "blob"
        }

    async def ensure_branch(self) -> str:
        head = await self.head()
        if head is not None:
            return head
        readme = f"{WIDGET_BRANCH_DIRECTORY}/README.md"
        tree = await self._request(
            "POST",
            "/git/trees",
            json_body={
                "tree": [
                    {
                        "path": readme,
                        "mode": "100644",
                        "type": "blob",
                        "content": WIDGET_BRANCH_README,
                    }
                ]
            },
            accept=(201,),
        )
        commit = await self._request(
            "POST",
            "/git/commits",
            json_body={
                "message": BRANCH_ROOT_MESSAGE,
                "tree": tree.json()["sha"],
                "parents": [],
            },
            accept=(201,),
        )
        root = str(commit.json()["sha"])
        created = await self._request(
            "POST",
            "/git/refs",
            json_body={"ref": f"refs/heads/{self._branch}", "sha": root},
            accept=(201, 422),
        )
        if created.status_code == 422:
            current = await self.head()
            if current is None:
                raise WidgetRepositoryUnavailableError("the widget branch could not be created")
            return current
        return root

    async def _expect(self, parent_sha: str) -> None:
        head = await self.head()
        if head is None or head != parent_sha:
            raise BranchMovedError(head)

    async def _commit_change(
        self, parent_sha: str, entries: list[dict[str, Any]], message: str
    ) -> str:
        tree = await self._request(
            "POST",
            "/git/trees",
            json_body={"base_tree": await self._commit_tree(parent_sha), "tree": entries},
            accept=(201,),
        )
        commit = await self._request(
            "POST",
            "/git/commits",
            json_body={"message": message, "tree": tree.json()["sha"], "parents": [parent_sha]},
            accept=(201,),
        )
        sha = str(commit.json()["sha"])
        updated = await self._request(
            "PATCH",
            f"/git/refs/heads/{self._branch}",
            json_body={"sha": sha, "force": False},
            accept=(200, 422),
        )
        if updated.status_code == 422:
            raise BranchMovedError(await self.head())
        return sha

    async def write_widget(
        self, slug: str, files: Mapping[str, str], *, parent_sha: str, message: str
    ) -> str:
        if not is_widget_slug(slug) or not files or not all(is_widget_file(name) for name in files):
            raise WidgetPathRefusedError(
                f"a write must stay inside {WIDGET_BRANCH_DIRECTORY}/<slug>/"
            )
        await self._expect(parent_sha)
        prefix = f"{WIDGET_BRANCH_DIRECTORY}/{slug}/"
        existing = await self._tree_paths(await self._commit_tree(parent_sha))
        written = {f"{prefix}{name}": text for name, text in files.items()}
        entries: list[dict[str, Any]] = [
            {"path": path, "mode": "100644", "type": "blob", "sha": None}
            for path in sorted(existing)
            if path.startswith(prefix) and path not in written
        ]
        entries.extend(
            {"path": path, "mode": "100644", "type": "blob", "content": text}
            for path, text in sorted(written.items())
        )
        return await self._commit_change(parent_sha, entries, message)

    async def remove_widget(self, slug: str, *, parent_sha: str, message: str) -> str:
        if not is_widget_slug(slug):
            raise WidgetPathRefusedError(
                f"a removal must name one {WIDGET_BRANCH_DIRECTORY}/<slug>/"
            )
        await self._expect(parent_sha)
        prefix = f"{WIDGET_BRANCH_DIRECTORY}/{slug}/"
        existing = await self._tree_paths(await self._commit_tree(parent_sha))
        removed = sorted(path for path in existing if path.startswith(prefix))
        if not removed:
            raise WidgetPathRefusedError(f"{prefix} is not on the branch")
        entries: list[dict[str, Any]] = [
            {"path": path, "mode": "100644", "type": "blob", "sha": None} for path in removed
        ]
        return await self._commit_change(parent_sha, entries, message)

    async def read_widget(self, slug: str, *, at: str) -> dict[str, str] | None:
        if not is_widget_slug(slug) or not _COMMIT_SHA.fullmatch(at):
            return None
        prefix = f"{WIDGET_BRANCH_DIRECTORY}/{slug}/"
        paths = await self._tree_paths(await self._commit_tree(at))
        files: dict[str, str] = {}
        for path, blob in sorted(paths.items()):
            if not path.startswith(prefix):
                continue
            response = await self._request("GET", f"/git/blobs/{blob}")
            body = response.json()
            files[path.removeprefix(prefix)] = base64.b64decode(body["content"]).decode("utf-8")
        return files or None

    async def _described(self, sha: str, parent_sha: str | None, message: str) -> WidgetCommit:
        response = await self._request("GET", f"/commits/{sha}")
        files = response.json().get("files", [])
        paths: list[str] = []
        for item in files:
            paths.append(str(item["filename"]))
            if item.get("previous_filename"):
                paths.append(str(item["previous_filename"]))
        slugs, outside = touched_widgets(paths)
        return WidgetCommit(
            sha=sha,
            parent_sha=parent_sha,
            message=message,
            trailers=commit_trailers(message),
            changed_slugs=slugs,
            outside_widgets=outside or len(files) >= _COMMIT_FILES_CAP,
        )

    async def _history(self, limit: int | None) -> list[dict[str, Any]]:
        """Newest first, the branch's commits, at most `limit` of them when given."""

        listed: list[dict[str, Any]] = []
        page = 1
        while limit is None or len(listed) < limit:
            response = await self._request(
                "GET",
                "/commits",
                params={"sha": self._branch, "per_page": _PAGE, "page": page},
                accept=(200, 404, 409),
            )
            if response.status_code != 200:
                return []
            batch = response.json()
            listed.extend(batch)
            if len(batch) < _PAGE:
                break
            page += 1
        return listed if limit is None else listed[:limit]

    async def commits_since(self, sha: str | None, *, limit: int) -> list[WidgetCommit]:
        if limit <= 0:
            return []
        if sha is None:
            window = list(reversed(await self._history(None)))[:limit]
        else:
            if not _COMMIT_SHA.fullmatch(sha):
                raise BranchMovedError(await self.head())
            response = await self._request(
                "GET",
                f"/compare/{sha}...{self._branch}",
                params={"per_page": min(limit, 250)},
                accept=(200, 404),
            )
            body = response.json() if response.status_code == 200 else {}
            if response.status_code == 404 or body.get("status") not in {"ahead", "identical"}:
                raise BranchMovedError(await self.head())
            window = list(body.get("commits", []))[:limit]
        described = []
        for item in window:
            parents = item.get("parents") or []
            described.append(
                await self._described(
                    str(item["sha"]),
                    str(parents[0]["sha"]) if parents else None,
                    str(item["commit"]["message"]),
                )
            )
        return described

    async def find_revision(self, revision_id: UUID, *, limit: int) -> str | None:
        if limit <= 0:
            return None
        for item in await self._history(limit):
            trailers = commit_trailers(str(item["commit"]["message"]))
            if trailers.get("Veetbot-Revision") == str(revision_id):
                return str(item["sha"])
        return None
