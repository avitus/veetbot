"""Validate declared reading lanes against the diff-derived minimum.

Run as a module from the repository root; CI runs it in the static job:

    READING_LANE_BASE="<previous revision>" uv run python -m scripts.check_reading_lane

A commit declares its lane with a ``Reading-Lane: A|B|C`` git trailer; no
declaration means lane A, the full reading order, which every diff permits.
The checked range passes when every commit's declaration covers that commit's
own diff, where a merge answers only for the paths it changed relative to
every parent. It also passes when the newest declaration in the range covers
every path the range changed, which is how a new commit repairs one that fell
short. The base revision is the first of ``--base``, ``READING_LANE_BASE``
(CircleCI's ``pipeline.git.base_revision``), ``origin/dev``, ``origin/main``,
then ``HEAD~1``; floors come from ``reading_lane_errors``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from scripts.architecture_checks import READING_LANES, minimum_reading_lane, reading_lane_errors

__all__ = ["check", "declared_lane", "main", "resolve_base"]

_TRAILER = re.compile(r"^Reading-Lane:\s*([A-Ca-c])\s*$", re.MULTILINE)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _paths(output: str) -> list[str]:
    """Split ``-z`` output, whose paths git leaves unquoted."""

    return [path for path in output.split("\0") if path]


def _resolves(root: Path, revision: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def declared_lane(messages: Iterable[str]) -> str:
    """Return the newest declared lane; absence means lane A."""

    for message in messages:
        match = _TRAILER.search(message)
        if match is not None:
            return match.group(1).upper()
    return "A"


def resolve_base(root: Path, explicit: str | None) -> str | None:
    """Pick the base revision the checked range starts from."""

    if explicit:
        return explicit
    head = _git(root, "rev-parse", "HEAD").strip()
    for candidate in ("origin/dev", "origin/main"):
        if not _resolves(root, candidate):
            continue
        merge_base = _git(root, "merge-base", candidate, "HEAD").strip()
        if merge_base != head:
            return candidate
    if _resolves(root, "HEAD~1"):
        return "HEAD~1"
    return None


def _commit_errors(root: Path, commits: Sequence[tuple[str, str]]) -> list[str]:
    """Judge each commit's own declaration against its own diff."""

    errors: list[str] = []
    for commit, message in commits:
        # -c lists only the paths a merge changed relative to every parent, so
        # a merge answers for its own resolution, not for the work it merged.
        paths = _paths(
            _git(
                root,
                "diff-tree",
                "-z",
                "-c",
                "-r",
                "--root",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                commit,
            )
        )
        for error in reading_lane_errors(declared_lane([message]), paths):
            # A single commit's report reads as it did before commits were named.
            errors.append(error if len(commits) == 1 else f"{commit[:12]}: {error}")
    return errors


def check(root: Path, base: str | None) -> tuple[str, str, list[str]]:
    """Return the newest declared lane, the derived minimum, and any errors."""

    resolved = resolve_base(root, base)
    if resolved is None:
        # A repository whose only commit is HEAD: classify that commit's tree.
        paths = _paths(
            _git(root, "diff-tree", "-z", "--no-commit-id", "--name-only", "-r", "--root", "HEAD")
        )
        declared = declared_lane([_git(root, "log", "-1", "--format=%B")])
        return declared, minimum_reading_lane(paths), reading_lane_errors(declared, paths)
    # --no-renames keeps both sides of a rename, so a renamed-away
    # authority file still sets the floor.
    paths = _paths(_git(root, "diff", "-z", "--name-only", "--no-renames", f"{resolved}...HEAD"))
    fields = _git(root, "log", "--format=%x00%H%x00%B", f"{resolved}..HEAD").split("\0")[1:]
    commits = list(zip(fields[::2], fields[1::2], strict=True))
    declared = declared_lane(message for _, message in commits)
    minimum = minimum_reading_lane(paths)
    if not reading_lane_errors(declared, paths):
        return declared, minimum, []
    return declared, minimum, _commit_errors(root, commits)


def _explicit_base(argument: str | None, environment: str) -> str | None:
    """Normalize the base: empty and all-zero revisions mean unset."""

    for value in (argument, environment):
        candidate = (value or "").strip()
        if candidate and not re.fullmatch(r"0+", candidate):
            return candidate
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the declared reading lanes.")
    parser.add_argument("--base", default=None, help="base revision of the checked range")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    base = _explicit_base(args.base, os.environ.get("READING_LANE_BASE", ""))
    declared, minimum, errors = check(root, base)
    print(f"reading lane: declared {declared}, derived minimum {minimum}")
    for error in errors:
        print(f"  - {error}")
    if errors:
        print(f"repair: a new commit declaring Reading-Lane: {minimum} covers the whole range")
    elif READING_LANES[declared] < READING_LANES[minimum]:
        print("every commit's declaration covers its own diff")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
