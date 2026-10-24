"""The fake widget repository: an in-memory linear branch with the real path rules.

It enforces what the GitHub adapter must: writes stay inside one widget's
directory, every write is a fast-forward from the expected head, and nothing
outside `cockpit/widgets/` is ever written by the platform.
`commit_as_person` stands in for a human commit, which may touch anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from agent_core.domain.cockpit import (
    WIDGET_BRANCH_DIRECTORY,
    WIDGET_BRANCH_README,
    commit_trailers,
    is_widget_file,
    is_widget_slug,
)
from agent_core.ports.cockpit import BranchMovedError, WidgetCommit, WidgetPathRefusedError


@dataclass(frozen=True, slots=True)
class _Commit:
    sha: str
    parent_sha: str | None
    message: str
    tree: Mapping[str, str]
    changed_slugs: tuple[str, ...]
    outside_widgets: bool


def _directory(slug: str) -> str:
    return f"{WIDGET_BRANCH_DIRECTORY}/{slug}/"


BRANCH_ROOT_MESSAGE = "cockpit: create the widget branch\n"


def touched_widgets(paths: list[str]) -> tuple[tuple[str, ...], bool]:
    """The widget slugs a commit's paths touch, and whether any path lies outside them."""

    slugs: set[str] = set()
    outside = False
    prefix = f"{WIDGET_BRANCH_DIRECTORY}/"
    for path in paths:
        if path.startswith(prefix) and "/" in path.removeprefix(prefix):
            slugs.add(path.removeprefix(prefix).split("/", 1)[0])
        elif path != f"{prefix}README.md":
            outside = True
    return tuple(sorted(slugs)), outside


class FakeWidgetRepository:
    def __init__(self) -> None:
        self._commits: dict[str, _Commit] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()

    def _head(self) -> str | None:
        return self._order[-1] if self._order else None

    def _commit(self, tree: Mapping[str, str], message: str, changed: list[str]) -> str:
        parent = self._head()
        document = json.dumps(
            [len(self._order), parent, message, sorted(tree.items())], separators=(",", ":")
        )
        sha = hashlib.sha1(document.encode("utf-8"), usedforsecurity=False).hexdigest()
        slugs, outside = touched_widgets(changed)
        self._commits[sha] = _Commit(sha, parent, message, dict(tree), slugs, outside)
        self._order.append(sha)
        return sha

    def _tree(self, sha: str | None) -> dict[str, str]:
        return {} if sha is None or sha not in self._commits else dict(self._commits[sha].tree)

    def _expect(self, parent_sha: str) -> None:
        head = self._head()
        if head is None or head != parent_sha:
            raise BranchMovedError(head)

    async def head(self) -> str | None:
        async with self._lock:
            return self._head()

    async def ensure_branch(self) -> str:
        async with self._lock:
            head = self._head()
            if head is not None:
                return head
            readme = f"{WIDGET_BRANCH_DIRECTORY}/README.md"
            return self._commit({readme: WIDGET_BRANCH_README}, BRANCH_ROOT_MESSAGE, [])

    async def write_widget(
        self,
        slug: str,
        files: Mapping[str, str],
        *,
        parent_sha: str,
        message: str,
    ) -> str:
        if not is_widget_slug(slug) or not files or not all(is_widget_file(name) for name in files):
            raise WidgetPathRefusedError(f"a write must stay inside {_directory('<slug>')}")
        async with self._lock:
            self._expect(parent_sha)
            prefix = _directory(slug)
            tree = {
                path: text
                for path, text in self._tree(parent_sha).items()
                if not path.startswith(prefix)
            }
            written = [f"{prefix}{name}" for name in files]
            tree.update({f"{prefix}{name}": text for name, text in files.items()})
            return self._commit(tree, message, written)

    async def remove_widget(self, slug: str, *, parent_sha: str, message: str) -> str:
        if not is_widget_slug(slug):
            raise WidgetPathRefusedError(f"a removal must name one {_directory('<slug>')}")
        async with self._lock:
            self._expect(parent_sha)
            prefix = _directory(slug)
            tree = self._tree(parent_sha)
            removed = [path for path in tree if path.startswith(prefix)]
            if not removed:
                raise WidgetPathRefusedError(f"{prefix} is not on the branch")
            for path in removed:
                del tree[path]
            return self._commit(tree, message, removed)

    async def read_widget(self, slug: str, *, at: str) -> dict[str, str] | None:
        async with self._lock:
            prefix = _directory(slug)
            files = {
                path.removeprefix(prefix): text
                for path, text in self._tree(at).items()
                if path.startswith(prefix)
            }
            return files or None

    def _public(self, sha: str) -> WidgetCommit:
        commit = self._commits[sha]
        return WidgetCommit(
            sha=commit.sha,
            parent_sha=commit.parent_sha,
            message=commit.message,
            trailers=commit_trailers(commit.message),
            changed_slugs=commit.changed_slugs,
            outside_widgets=commit.outside_widgets,
        )

    async def commits_since(self, sha: str | None, *, limit: int) -> list[WidgetCommit]:
        async with self._lock:
            if sha is not None and sha not in self._commits:
                raise BranchMovedError(self._head())
            start = 0 if sha is None else self._order.index(sha) + 1
            return [self._public(item) for item in self._order[start : start + limit]]

    async def find_revision(self, revision_id: UUID, *, limit: int) -> str | None:
        async with self._lock:
            for sha in reversed(self._order[-limit:] if limit > 0 else []):
                if commit_trailers(self._commits[sha].message).get("Veetbot-Revision") == str(
                    revision_id
                ):
                    return sha
            return None

    def commit_as_person(self, changes: Mapping[str, str | None], *, message: str) -> str:
        """Record a human commit, which may touch any path; None deletes a file."""

        tree = self._tree(self._head())
        for path, text in changes.items():
            if text is None:
                tree.pop(path, None)
            else:
                tree[path] = text
        return self._commit(tree, message, list(changes))

    def tree(self, sha: str) -> dict[str, str]:
        return self._tree(sha)
