"""Versioned M33 input manifests, independent of Git commit identity.

The fixed exclusions cover documentation, generated outputs and local tooling.
Every other file (including new paths, deployment scripts and migrations) is an
input. Operators cannot supply globs. Effective secrets-free configuration is
bound separately; credentials and runtime environment files are never recorded.
"""

import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field

from agent_core.domain.general_recall_release import Digest, RecallReleaseIdentity, ReleaseValue
from agent_core.memory.general_recall_evidence import _read_bounded

# This is a reviewed contract, not .gitignore or an operator-controlled list.
_EXCLUDED_ROOTS = frozenset(
    {
        ".git",
        ".chunk",
        ".venv",
        "venv",
        ".agent",
        "docs",
        "archive",
        "superpowers",
        "site",
        "dist",
        "build",
        "test-results",
    }
)
_EXCLUDED_NAMES = frozenset(
    {
        "__pycache__",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
        ".hypothesis",
        "node_modules",
        ".next",
        ".build",
        "DerivedData",
        "xcuserdata",
        ".DS_Store",
    }
)
_EXCLUDED_FILES = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "docs-manifest.yaml",
        "mkdocs.yml",
        ".env",
        ".release.env",
        ".owner-scopes.env",
    }
)
_EXCLUDED_TREES = ("evals/observations", "website/out")


class InputFile(ReleaseValue):
    sha256: Digest
    executable: bool


class RecallInputManifest(ReleaseValue):
    version: Literal["recall-evaluated-inputs@1"] = "recall-evaluated-inputs@1"
    files: dict[str, InputFile] = Field(min_length=1, max_length=20000)
    dependencies: dict[str, Digest] = Field(min_length=1)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class RecallDeploymentBinding(ReleaseValue):
    version: Literal["recall-deployment-binding@1"]
    identity: RecallReleaseIdentity
    deployment_build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    evidence_sha256: Digest
    inputs: RecallInputManifest


def input_path(name: str) -> bool:
    """Select the same complete path set in a Git tree and its unpacked release."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or str(path) != name:
        raise ValueError("Invalid recall input path")
    return not (
        path.parts[0] in _EXCLUDED_ROOTS
        or any(part in _EXCLUDED_NAMES or part.endswith(".egg-info") for part in path.parts)
        or name in _EXCLUDED_FILES
        or any(name == tree or name.startswith(tree + "/") for tree in _EXCLUDED_TREES)
        or path.suffix in {".pyc", ".pyo"}
    )


def input_dependencies(identity: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in identity.items()
        if key not in {"build_ref", "evaluated_inputs_sha256"}
    }


def input_stamps(root: Path) -> dict[str, tuple[int, ...]]:
    """Inventory includes names and mutation metadata; never follow links."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Recall input root must be a real directory")
    files: dict[str, tuple[int, ...]] = {}

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            name = path.relative_to(root).as_posix()
            if not input_path(name):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("Recall inputs cannot contain symlinks")
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                files[name] = (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            else:
                raise ValueError("Recall inputs must be regular files")
            if len(files) > 20000:
                raise ValueError("Too many recall input files")

    visit(root)
    return files


def capture_inputs(root: Path, identity: Mapping[str, Any]) -> RecallInputManifest:
    """Capture local bytes, never take a supplied file list as proof of coverage."""
    before = input_stamps(root)
    files = {
        name: InputFile(
            sha256=hashlib.sha256(_read_bounded(root / name)).hexdigest(),
            executable=bool(stamp[2] & 0o111),
        )
        for name, stamp in before.items()
    }
    if input_stamps(root) != before:
        raise ValueError("Recall input changed during hashing")
    return RecallInputManifest(files=files, dependencies=input_dependencies(identity))
