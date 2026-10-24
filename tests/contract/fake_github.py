"""An in-memory GitHub REST remote for the widget repository adapter (Milestone 35).

It serves the Git Data, commits, compare and App-token routes the adapter calls,
over an httpx transport, with the server-side rules that matter: a ref update
without `force` must be a fast-forward, and an installation token is required.
Faults are injected per request: an outage answers 503, and a lost response
applies the request and then drops the connection. Every request is logged so
tests can assert what reached the remote.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

OWNER, NAME = "avitus", "veetbot"
APP_ID, INSTALLATION_ID = 123456, 7890
_REPO = f"/repos/{OWNER}/{NAME}"


def app_private_key() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _sha(kind: str, document: object) -> str:
    raw = json.dumps([kind, document], sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class LoggedRequest:
    method: str
    path: str
    body: Any


@dataclass
class _Commit:
    tree: str
    parents: list[str]
    message: str


@dataclass
class FakeGitHubRemote:
    private_key_pem: bytes
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    blobs: dict[str, str] = field(default_factory=dict)
    trees: dict[str, dict[str, str]] = field(default_factory=dict)
    commits: dict[str, _Commit] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    requests: list[LoggedRequest] = field(default_factory=list)
    tokens_issued: int = 0
    outage: bool = False
    _faults: list[tuple[str, re.Pattern[str], str]] = field(default_factory=list)
    _tokens: dict[str, datetime] = field(default_factory=dict)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def fail_next(self, method: str, path: str, fault: str) -> None:
        """`fault` is `unavailable` (503, nothing applied) or `lost` (applied, no response)."""

        self._faults.append((method, re.compile(path), fault))

    # Object model -------------------------------------------------------------

    def _blob(self, text: str) -> str:
        sha = _sha("blob", text)
        self.blobs[sha] = text
        return sha

    def _tree(self, entries: Mapping[str, str]) -> str:
        sha = _sha("tree", sorted(entries.items()))
        self.trees[sha] = dict(entries)
        return sha

    def _commit(self, tree: str, parents: list[str], message: str) -> str:
        sha = _sha("commit", [tree, parents, message, len(self.commits)])
        self.commits[sha] = _Commit(tree, list(parents), message)
        return sha

    def files_at(self, commit_sha: str) -> dict[str, str]:
        tree = self.trees[self.commits[commit_sha].tree]
        return {path: self.blobs[blob] for path, blob in tree.items()}

    def commit_as_person(
        self, changes: Mapping[str, str | None], *, message: str, branch: str = "cockpit/widgets"
    ) -> str:
        """A human commit straight onto the branch; None deletes a path."""

        head = self.refs[f"refs/heads/{branch}"]
        entries = dict(self.trees[self.commits[head].tree])
        for path, text in changes.items():
            if text is None:
                entries.pop(path, None)
            else:
                entries[path] = self._blob(text)
        sha = self._commit(self._tree(entries), [head], message)
        self.refs[f"refs/heads/{branch}"] = sha
        return sha

    def _ancestry(self, sha: str) -> list[str]:
        """Newest first, following first parents."""

        chain: list[str] = []
        current: str | None = sha
        while current is not None:
            chain.append(current)
            parents = self.commits[current].parents
            current = parents[0] if parents else None
        return chain

    def _changed(self, sha: str) -> list[dict[str, str]]:
        commit = self.commits[sha]
        after = self.trees[commit.tree]
        before = self.trees[self.commits[commit.parents[0]].tree] if commit.parents else {}
        files = []
        for path in sorted(set(before) | set(after)):
            if path not in after:
                files.append({"filename": path, "status": "removed"})
            elif path not in before:
                files.append({"filename": path, "status": "added"})
            elif before[path] != after[path]:
                files.append({"filename": path, "status": "modified"})
        return files

    def _public_commit(self, sha: str, *, files: bool = False) -> dict[str, Any]:
        commit = self.commits[sha]
        body: dict[str, Any] = {
            "sha": sha,
            "commit": {"message": commit.message, "tree": {"sha": commit.tree}},
            "parents": [{"sha": parent} for parent in commit.parents],
        }
        if files:
            body["files"] = self._changed(sha)
        return body

    # Transport ----------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append(LoggedRequest(request.method, path, body))
        fault = next(
            (
                item
                for item in self._faults
                if item[0] == request.method and item[1].fullmatch(path)
            ),
            None,
        )
        if fault is not None:
            self._faults.remove(fault)
        if self.outage or (fault is not None and fault[2] == "unavailable"):
            return httpx.Response(503, json={"message": "Service Unavailable"})
        response = self._route(request, path, body)
        if fault is not None and fault[2] == "lost":
            raise httpx.ReadError("connection reset after the request was applied", request=request)
        return response

    def _authorized(self, request: httpx.Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        expiry = self._tokens.get(token)
        return scheme == "Bearer" and expiry is not None and expiry > self.now()

    def _verify_app_jwt(self, request: httpx.Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or token.count(".") != 2:
            return False
        header, payload, signature = token.split(".")
        key = serialization.load_pem_private_key(self.private_key_pem, password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        try:
            key.public_key().verify(
                _b64url_decode(signature),
                f"{header}.{payload}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:
            return False
        claims = json.loads(_b64url_decode(payload))
        instant = self.now().timestamp()
        return bool(
            json.loads(_b64url_decode(header)) == {"alg": "RS256", "typ": "JWT"}
            and claims.get("iss") == str(APP_ID)
            and claims["iat"] <= instant < claims["exp"]
            and claims["exp"] - claims["iat"] <= 600
        )

    def _route(self, request: httpx.Request, path: str, body: Any) -> httpx.Response:
        if path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            if request.method != "POST" or not self._verify_app_jwt(request):
                return httpx.Response(401, json={"message": "Bad credentials"})
            self.tokens_issued += 1
            token = f"ghs_fake{self.tokens_issued}"
            expires = self.now() + timedelta(hours=1)
            self._tokens[token] = expires
            return httpx.Response(
                201, json={"token": token, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
            )
        if not self._authorized(request):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if not path.startswith(_REPO + "/"):
            return httpx.Response(404, json={"message": "Not Found"})
        route = path.removeprefix(_REPO)
        method = request.method
        if method == "GET" and route.startswith("/git/ref/"):
            ref = "refs/" + route.removeprefix("/git/ref/")
            if ref not in self.refs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"ref": ref, "object": {"sha": self.refs[ref]}})
        if method == "POST" and route == "/git/refs":
            if body["ref"] in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[body["ref"]] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})
        if method == "PATCH" and route.startswith("/git/refs/"):
            ref = "refs/" + route.removeprefix("/git/refs/")
            current = self.refs.get(ref)
            if current is None:
                return httpx.Response(422, json={"message": "Reference does not exist"})
            target = body["sha"]
            if not body.get("force") and current not in self._ancestry(target):
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.refs[ref] = target
            return httpx.Response(200, json={"ref": ref, "object": {"sha": target}})
        if method == "GET" and route.startswith("/git/commits/"):
            sha = route.removeprefix("/git/commits/")
            if sha not in self.commits:
                return httpx.Response(404, json={"message": "Not Found"})
            commit = self.commits[sha]
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "tree": {"sha": commit.tree},
                    "parents": [{"sha": parent} for parent in commit.parents],
                    "message": commit.message,
                },
            )
        if method == "POST" and route == "/git/commits":
            if body["tree"] not in self.trees or any(
                p not in self.commits for p in body["parents"]
            ):
                return httpx.Response(422, json={"message": "Invalid tree or parent"})
            sha = self._commit(body["tree"], body["parents"], body["message"])
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and route == "/git/trees":
            entries: dict[str, str] = {}
            if body.get("base_tree") is not None:
                if body["base_tree"] not in self.trees:
                    return httpx.Response(422, json={"message": "Invalid base tree"})
                entries = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if entry.get("sha", "") is None:
                    entries.pop(entry["path"], None)
                elif "content" in entry:
                    entries[entry["path"]] = self._blob(entry["content"])
                else:
                    entries[entry["path"]] = entry["sha"]
            return httpx.Response(201, json={"sha": self._tree(entries)})
        if method == "GET" and route.startswith("/git/trees/"):
            sha = route.removeprefix("/git/trees/")
            if sha not in self.trees:
                return httpx.Response(404, json={"message": "Not Found"})
            listing = [
                {"path": path, "type": "blob", "mode": "100644", "sha": blob}
                for path, blob in sorted(self.trees[sha].items())
            ]
            return httpx.Response(200, json={"sha": sha, "tree": listing, "truncated": False})
        if method == "GET" and route.startswith("/git/blobs/"):
            sha = route.removeprefix("/git/blobs/")
            if sha not in self.blobs:
                return httpx.Response(404, json={"message": "Not Found"})
            content = base64.b64encode(self.blobs[sha].encode("utf-8")).decode("ascii")
            return httpx.Response(200, json={"sha": sha, "content": content, "encoding": "base64"})
        if method == "GET" and route == "/commits":
            head = self.refs.get(f"refs/heads/{request.url.params['sha']}")
            if head is None:
                return httpx.Response(404, json={"message": "Not Found"})
            per_page = int(request.url.params.get("per_page", "30"))
            page = int(request.url.params.get("page", "1"))
            chain = self._ancestry(head)[(page - 1) * per_page : page * per_page]
            return httpx.Response(200, json=[self._public_commit(sha) for sha in chain])
        if method == "GET" and route.startswith("/commits/"):
            sha = route.removeprefix("/commits/")
            if sha not in self.commits:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self._public_commit(sha, files=True))
        if method == "GET" and route.startswith("/compare/"):
            base, _, head_name = route.removeprefix("/compare/").partition("...")
            head = self.refs.get(f"refs/heads/{head_name}")
            if head is None or base not in self.commits:
                return httpx.Response(404, json={"message": "Not Found"})
            chain = self._ancestry(head)
            if base not in chain:
                return httpx.Response(200, json={"status": "diverged", "commits": []})
            ahead = list(reversed(chain[: chain.index(base)]))
            per_page = int(request.url.params.get("per_page", "250"))
            page = int(request.url.params.get("page", "1"))
            window = ahead[(page - 1) * per_page : page * per_page]
            return httpx.Response(
                200,
                json={
                    "status": "ahead" if ahead else "identical",
                    "ahead_by": len(ahead),
                    "commits": [self._public_commit(sha) for sha in window],
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})
