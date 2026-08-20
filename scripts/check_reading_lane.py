"""Validate the declared reading lane against the diff-derived minimum.

Run as a module from the repository root; CI runs it in the static job:

    READING_LANE_BASE="<previous revision>" uv run python -m scripts.check_reading_lane

The lane is declared with a ``Reading-Lane: A|B|C`` git trailer on a commit in
the checked range; the newest declaration wins, and no declaration means lane
A, the full reading order, which every diff permits. The base revision is the
first of ``--base``, ``READING_LANE_BASE`` (CircleCI's
``pipeline.git.base_revision``), ``origin/dev``, ``origin/main``, then
``HEAD~1``; the floor comes from ``reading_lane_errors`` over the paths
changed since that base.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from scripts.architecture_checks import minimum_reading_lane, reading_lane_errors

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


def check(root: Path, base: str | None) -> tuple[str, str, list[str]]:
    """Return the declared lane, the derived minimum, and any errors."""

    resolved = resolve_base(root, base)
    if resolved is None:
        paths: list[str] = []
        messages = [_git(root, "log", "-1", "--format=%B")]
    else:
        paths = [
            line
            for line in _git(root, "diff", "--name-only", f"{resolved}...HEAD").splitlines()
            if line
        ]
        messages = [
            message
            for message in _git(root, "log", "--format=%x00%B", f"{resolved}..HEAD").split("\x00")
            if message.strip()
        ]
    declared = declared_lane(messages)
    return declared, minimum_reading_lane(paths), reading_lane_errors(declared, paths)


def _explicit_base(argument: str | None, environment: str) -> str | None:
    """Normalize the base: empty and all-zero revisions mean unset."""

    for value in (argument, environment):
        candidate = (value or "").strip()
        if candidate and not re.fullmatch(r"0+", candidate):
            return candidate
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the declared reading lane.")
    parser.add_argument("--base", default=None, help="base revision of the checked range")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    base = _explicit_base(args.base, os.environ.get("READING_LANE_BASE", ""))
    declared, minimum, errors = check(root, base)
    print(f"reading lane: declared {declared}, derived minimum {minimum}")
    for error in errors:
        print(f"  - {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
