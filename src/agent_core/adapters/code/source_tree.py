"""Serve only the tracked source of a release directory (ADR-0281).

A release keeps private files beside its source, such as `.release.env` and the
virtual environment, and it has no `.git` to say what is tracked. So this serves
an allowlist of top-level names, and refuses dotted, generated and key-shaped
names at every depth, and every symbolic link.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path, PurePosixPath

from agent_core.ports.code import (
    SourceEntry,
    SourceNotTextError,
    SourcePathRefusedError,
    SourcePatternInvalidError,
    SourceSearch,
)

_TOP_DIRECTORIES = frozenset(
    {
        ".circleci",
        ".github",
        "clients",
        "cockpit",
        "deploy",
        "docs",
        "evals",
        "execution",
        "migrations",
        "nginx",
        "samples",
        "scripts",
        "security",
        "src",
        "tests",
        "website",
    }
)
_TOP_FILES = frozenset(
    {"AGENTS.md", "CLAUDE.md", "Makefile", "README.md", "mkdocs.yml", "pyproject.toml"}
)
_REFUSED_DIRECTORIES = frozenset(
    {"__pycache__", "archive", "build", "dist", "node_modules", "site", "venv"}
)
_REFUSED_SUFFIXES = (".env", ".key", ".p12", ".p8", ".pem", ".pyc", ".pyo", ".so")
_MAXIMUM_FILE_BYTES = 2 * 1024 * 1024
_MAXIMUM_LINE_CHARACTERS = 200
_MAXIMUM_SEARCH_OUTPUT = 512 * 1024
_SEARCH_SECONDS = 10.0


def _segments_allowed(parts: tuple[str, ...]) -> bool:
    for index, part in enumerate(parts):
        is_top = index == 0
        if part in _REFUSED_DIRECTORIES or part.endswith(_REFUSED_SUFFIXES):
            return False
        if part.startswith(".") and not (is_top and part in _TOP_DIRECTORIES):
            return False
    if not parts:
        return True
    return parts[0] in _TOP_DIRECTORIES or (len(parts) == 1 and parts[0] in _TOP_FILES)


class FilesystemSourceTree:
    def __init__(self, root: Path, *, release_id: str | None) -> None:
        self._root = root.resolve()
        self._release_id = release_id

    @property
    def release_id(self) -> str | None:
        return self._release_id

    def _checked(self, path: str) -> tuple[Path, tuple[str, ...]]:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or not _segments_allowed(pure.parts):
            raise SourcePathRefusedError(path)
        current = self._root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise SourcePathRefusedError(path)
        return current, pure.parts

    def allows(self, path: str) -> bool:
        try:
            _, parts = self._checked(path)
        except SourcePathRefusedError:
            return False
        return bool(parts)

    async def list_directory(self, path: str) -> list[SourceEntry]:
        directory, parts = self._checked(path)
        if not directory.is_dir():
            raise SourcePathRefusedError(path)
        entries: list[SourceEntry] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if child.is_symlink() or not _segments_allowed((*parts, child.name)):
                continue
            entries.append(
                SourceEntry(
                    name=child.name,
                    is_directory=child.is_dir(),
                    size_bytes=None if child.is_dir() else child.stat().st_size,
                )
            )
        return entries

    async def read_text(self, path: str) -> str:
        file, parts = self._checked(path)
        if not parts or not file.is_file():
            raise SourcePathRefusedError(path)
        if file.stat().st_size > _MAXIMUM_FILE_BYTES:
            raise SourceNotTextError(path)
        content = file.read_bytes()
        if b"\x00" in content:
            raise SourceNotTextError(path)
        try:
            return content.decode()
        except UnicodeDecodeError as exc:
            raise SourceNotTextError(path) from exc

    async def search(
        self, pattern: str, *, path: str = "", fixed: bool = False, maximum_bytes: int
    ) -> SourceSearch:
        _, parts = self._checked(path)
        targets = (
            [path]
            if parts
            else sorted(
                name
                for name in (*_TOP_DIRECTORIES, *_TOP_FILES)
                if (self._root / name).exists() and not (self._root / name).is_symlink()
            )
        )
        process = await asyncio.create_subprocess_exec(
            "grep",
            "-rnI",
            "-F" if fixed else "-E",
            *(f"--exclude-dir={name}" for name in sorted(_REFUSED_DIRECTORIES)),
            "--exclude-dir=.?*",
            *(f"--exclude=*{suffix}" for suffix in _REFUSED_SUFFIXES),
            "-e",
            pattern,
            "--",
            *targets,
            cwd=self._root,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdout is not None
        try:
            async with asyncio.timeout(_SEARCH_SECONDS):
                output = await process.stdout.read(_MAXIMUM_SEARCH_OUTPUT)
                overflowed = len(output) >= _MAXIMUM_SEARCH_OUTPUT
                if overflowed:
                    process.kill()
                code = await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return SourceSearch(lines=(), truncated=True)
        if code == 2 and not output:
            raise SourcePatternInvalidError(pattern)
        lines: list[str] = []
        used = 0
        truncated = overflowed
        for raw in output.decode(errors="replace").splitlines():
            name = raw.split(":", 1)[0]
            if not _segments_allowed(PurePosixPath(name).parts):
                continue
            line = raw[:_MAXIMUM_LINE_CHARACTERS]
            used += len(line.encode()) + 1
            if used > maximum_bytes:
                truncated = True
                break
            lines.append(line)
        return SourceSearch(lines=tuple(lines), truncated=truncated)
