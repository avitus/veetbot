"""Sandbox-only entry point behind code.test: one closed check over a draft (ADR-0281).

Reads `{"files": {path: content|null}, "check": {kind, targets, keyword}}` on stdin,
copies the baked source tree into the writable working directory, applies the
draft there, runs one fixed command per kind without a shell, and writes one JSON
report `{passed, exit_code, timed_out, output, seconds}` to stdout. Nothing the
request names ever becomes an option: a target is an existing repository path, a
keyword is one identifier.

What it cannot do: the sandbox has no network, PostgreSQL, Docker, browser engine,
`.git`, or installed `agent` command. Tests that need one fail or skip here, so a
pass says less than hosted CI, and a failure in such a test says nothing about the
draft. tests/native never runs, because it drives a real desktop. All of tests/unit
does not finish inside the deadline, so name the tests a draft concerns.

This file runs as a script in isolated mode, so it imports only the standard
library.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

_SOURCE_ROOT = Path("/source")
_ROOTS = frozenset({"tests", "src", "cockpit", "execution", "scripts"})
_DEFAULT_TARGETS = {
    "pytest": ("tests/unit",),
    "ruff": ("src", "tests", "scripts"),
    "mypy": ("src",),
}
_KEYWORD = re.compile(r"[A-Za-z0-9_]{1,80}")
_COCKPIT_TEST_SUFFIX = ".test.mjs"
# These start a browser engine, which this image does not hold.
_COCKPIT_BROWSER_TESTS = frozenset({"builder.test.mjs", "realm.test.mjs"})
_MAXIMUM_REQUEST_BYTES = 8 * 1024 * 1024
_MAXIMUM_FILES = 40
_MAXIMUM_TARGETS = 40
_MAXIMUM_PATH_CHARACTERS = 512
_MAXIMUM_OUTPUT_CHARACTERS = 6000
_MAXIMUM_OUTPUT_BYTES = _MAXIMUM_OUTPUT_CHARACTERS * 4
# Inside the adapter's 300 seconds, so a slow run reports what it printed
# instead of being killed with nothing to say.
_DEFAULT_TIMEOUT_SECONDS = 270
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
_REAP_SECONDS = 2.0


class _RefusedError(ValueError):
    """The request is outside the closed set; nothing further runs."""


def _components(path: object, label: str) -> tuple[str, ...]:
    """Split a repository path, refusing anything but plain names under an allowed root."""
    if (
        not isinstance(path, str)
        or not path
        or len(path) > _MAXIMUM_PATH_CHARACTERS
        or path.startswith(("/", "-"))
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise _RefusedError(f"Refused {label} {path!r}: not a repository path.")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} or part.startswith("-") for part in parts):
        raise _RefusedError(f"Refused {label} {path!r}: not a plain repository path.")
    if parts[0] not in _ROOTS:
        raise _RefusedError(
            f"Refused {label} {path!r}: only {', '.join(sorted(_ROOTS))} can be used."
        )
    return parts


def _parse_request(raw: bytes) -> tuple[dict[str, str | None], str, tuple[str, ...], str | None]:
    if len(raw) > _MAXIMUM_REQUEST_BYTES:
        raise _RefusedError("Refused request: larger than the limit.")
    try:
        request = json.loads(raw)
    except ValueError as error:
        raise _RefusedError("Refused request: not JSON.") from error
    if not isinstance(request, dict) or set(request) != {"files", "check"}:
        raise _RefusedError("Refused request: expected files and check.")
    files, check = request["files"], request["check"]
    if not isinstance(files, dict) or len(files) > _MAXIMUM_FILES:
        raise _RefusedError("Refused files: expected a bounded object.")
    if not isinstance(check, dict) or not set(check) <= {"kind", "targets", "keyword"}:
        raise _RefusedError("Refused check: unknown field.")
    kind = check.get("kind")
    if kind not in {*_DEFAULT_TARGETS, "cockpit"}:
        raise _RefusedError(f"Refused check kind {kind!r}.")
    targets = check.get("targets", [])
    if not isinstance(targets, list) or len(targets) > _MAXIMUM_TARGETS:
        raise _RefusedError("Refused targets: expected a bounded list.")
    keyword = check.get("keyword")
    if keyword is not None and (
        kind != "pytest" or not isinstance(keyword, str) or not _KEYWORD.fullmatch(keyword)
    ):
        raise _RefusedError("Refused keyword: only pytest takes one, and it is one identifier.")
    for path, content in files.items():
        # A file sits below a root; the root itself is a directory.
        if len(_components(path, "file")) < 2:
            raise _RefusedError(f"Refused file {path!r}: not a file below an allowed root.")
        if content is not None and not isinstance(content, str):
            raise _RefusedError(f"Refused file {path!r}: content is not text.")
    for target in targets:
        _components(target, "target")
    return files, kind, tuple(targets), keyword


def _copy_source(source: Path, workspace: Path) -> None:
    """Copy the baked tree, keeping symbolic links as links so nothing resolves through them."""
    with os.scandir(source) as entries:
        for entry in entries:
            destination = workspace / entry.name
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), destination)
            elif entry.is_dir(follow_symlinks=False):
                shutil.copytree(entry.path, destination, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(entry.path, destination)


def _apply_file(workspace: Path, path: str, content: str | None) -> None:
    """Write or delete one path by walking real directories, never through a link."""
    parts = path.split("/")
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            if content is not None:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=directory)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        name = parts[-1]
        if content is None:
            mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise _RefusedError(f"Refused file {path!r}: not a regular file.")
            os.unlink(name, dir_fd=directory)
            return
        encoded = content.encode()
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o644,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
    finally:
        os.close(directory)


def _apply_files(workspace: Path, files: Mapping[str, str | None]) -> None:
    for path, content in files.items():
        try:
            _apply_file(workspace, path, content)
        except FileNotFoundError:
            # Deleting what the release never held changes nothing.
            if content is not None:
                raise _RefusedError(f"Refused file {path!r}: cannot be written.") from None
        except (OSError, UnicodeError) as error:
            raise _RefusedError(f"Refused file {path!r}: cannot be written.") from error


def _require_real_path(workspace: Path, parts: Sequence[str], target: str) -> None:
    current = workspace
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError as error:
            raise _RefusedError(f"Refused target {target!r}: no such path.") from error
        if stat.S_ISLNK(mode):
            raise _RefusedError(f"Refused target {target!r}: it passes through a symbolic link.")


def _validated_targets(workspace: Path, kind: str, targets: Sequence[str]) -> tuple[str, ...]:
    for target in targets:
        parts = _components(target, "target")
        _require_real_path(workspace, parts, target)
        if kind == "pytest" and parts[:2] == ("tests", "native"):
            raise _RefusedError(f"Refused target {target!r}: native tests drive a real desktop.")
    if targets:
        return tuple(targets)
    if kind == "pytest":
        return _DEFAULT_TARGETS[kind]
    # The tree may legitimately lack one of the broader defaults.
    return tuple(item for item in _DEFAULT_TARGETS[kind] if (workspace / item).is_dir())


def _cockpit_tests(workspace: Path, targets: Sequence[str]) -> tuple[str, ...]:
    """Name Cockpit tests relative to cockpit/, excluding those that need a browser."""
    for target in targets:
        parts = _components(target, "target")
        _require_real_path(workspace, parts, target)
        if (
            len(parts) != 3
            or parts[:2] != ("cockpit", "tests")
            or not parts[2].endswith(_COCKPIT_TEST_SUFFIX)
        ):
            raise _RefusedError(f"Refused target {target!r}: not a Cockpit test file.")
        if parts[2] in _COCKPIT_BROWSER_TESTS:
            raise _RefusedError(f"Refused target {target!r}: it needs a browser engine.")
    if targets:
        return tuple(f"tests/{target.rsplit('/', 1)[-1]}" for target in targets)
    return tuple(
        f"tests/{path.name}"
        for path in sorted((workspace / "cockpit/tests").glob(f"*{_COCKPIT_TEST_SUFFIX}"))
        if path.name not in _COCKPIT_BROWSER_TESTS and not path.is_symlink()
    )


def _child_environment(workspace: Path) -> dict[str, str]:
    """Build, never filter: a check sees these values and nothing from its parent."""
    temporary = workspace / ".tmp"
    temporary.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", _DEFAULT_PATH),
        "HOME": str(workspace),
        # The sandbox's /tmp is small and no-exec; tests write helper programs.
        "TMPDIR": str(temporary),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(workspace / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


class _Tail:
    """Keep only the end of a child's output, so a loud test cannot exhaust memory."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def collect(self, descriptor: int) -> None:
        while chunk := os.read(descriptor, 65536):
            with self._lock:
                self._buffer += chunk
                del self._buffer[:-_MAXIMUM_OUTPUT_BYTES]

    def text(self) -> str:
        with self._lock:
            return bytes(self._buffer).decode("utf-8", errors="replace")


def _execute(
    argv: Sequence[str], *, cwd: Path, environment: Mapping[str, str], timeout: float
) -> tuple[int | None, bool, str]:
    """Run one fixed command in its own session; the whole session dies with it."""
    process = subprocess.Popen(  # noqa: S603 - a closed argv and never a shell.
        argv,
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    tail = _Tail()
    reader = threading.Thread(target=tail.collect, args=(process.stdout.fileno(),), daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=max(timeout, 0.001))
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Also ends anything a finished test left behind holding the pipe open.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    reader.join(_REAP_SECONDS)
    return (None if timed_out else process.returncode), timed_out, tail.text()


def _commands(
    workspace: Path, kind: str, targets: Sequence[str], keyword: str | None
) -> tuple[Path, tuple[tuple[str, ...], ...]]:
    """The closed set: a working directory and the commands to run there, in order."""
    python = sys.executable
    if kind == "pytest":
        selection = ("-k", keyword) if keyword is not None else ()
        chosen = _validated_targets(workspace, kind, targets)
        return workspace, (
            (
                python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--ignore=tests/native",
                *selection,
                *chosen,
            ),
        )
    if kind == "ruff":
        chosen = _validated_targets(workspace, kind, targets)
        return workspace, ((python, "-m", "ruff", "check", "--no-cache", *chosen),)
    if kind == "mypy":
        chosen = _validated_targets(workspace, kind, targets)
        return workspace, ((python, "-m", "mypy", "--cache-dir=/dev/null", *chosen),)
    tests = _cockpit_tests(workspace, targets)
    if not tests:
        raise _RefusedError("Refused check: no Cockpit test can run here.")
    node = shutil.which("node", path=os.environ.get("PATH", _DEFAULT_PATH))
    if node is None:
        raise _RefusedError("Refused check: this sandbox holds no Node runtime.")
    # As `npm test` does: tests import the kit the draft's sources build into dist.
    return workspace / "cockpit", (
        (node, "scripts/build-runtime.mjs"),
        (node, "--test", *tests),
    )


def run_check(
    raw: bytes, *, source: Path, workspace: Path, timeout_seconds: float, clock: Callable[[], float]
) -> dict[str, Any]:
    """Validate, prepare the workspace, then run the commands until one fails."""
    started = clock()
    files, kind, targets, keyword = _parse_request(raw)
    _copy_source(source, workspace)
    _apply_files(workspace, files)
    cwd, commands = _commands(workspace, kind, targets, keyword)
    environment = _child_environment(workspace)
    exit_code: int | None = None
    timed_out = False
    output = ""
    for command in commands:
        remaining = timeout_seconds - (clock() - started)
        exit_code, timed_out, text = _execute(
            command, cwd=cwd, environment=environment, timeout=remaining
        )
        output = f"{output}{text}"
        if exit_code != 0:
            break
    return {
        "passed": exit_code == 0 and not timed_out,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output": output[-_MAXIMUM_OUTPUT_CHARACTERS:],
        "seconds": round(clock() - started, 3),
    }


def main(argv: Sequence[str] | None = None, *, source_root: Path = _SOURCE_ROOT) -> None:
    """Answer on stdout and exit 0 only for a pass, so the adapter can compare the two."""
    parser = argparse.ArgumentParser(description="Run one closed check over a draft.")
    parser.add_argument("--source", type=Path, default=source_root)
    parser.add_argument("--timeout-seconds", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    options = parser.parse_args(argv)
    started = time.perf_counter()
    try:
        report = run_check(
            sys.stdin.buffer.read(_MAXIMUM_REQUEST_BYTES + 1),
            source=options.source,
            workspace=Path.cwd(),
            timeout_seconds=options.timeout_seconds,
            clock=time.perf_counter,
        )
    except (ValueError, OSError, shutil.Error) as error:
        report = {
            "passed": False,
            "exit_code": None,
            "timed_out": False,
            "output": str(error)[:_MAXIMUM_OUTPUT_CHARACTERS],
            "seconds": round(time.perf_counter() - started, 3),
        }
    sys.stdout.buffer.write(json.dumps(report, allow_nan=False).encode() + b"\n")
    sys.stdout.buffer.flush()
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
