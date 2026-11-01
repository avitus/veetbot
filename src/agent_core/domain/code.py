"""Veetbot's own source as something it can read, change and propose (ADR-0281)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

RUN_INSPECT_TOOL_NAME = "run.inspect"
CODE_READ_TOOL_NAME = "code.read"
CODE_DRAFT_TOOL_NAME = "code.draft"
CODE_TEST_TOOL_NAME = "code.test"
CODE_PROPOSE_TOOL_NAME = "code.propose"
CODE_WRITE_TOOL_NAMES = (CODE_DRAFT_TOOL_NAME, CODE_TEST_TOOL_NAME, CODE_PROPOSE_TOOL_NAME)
CODE_TOOL_NAMES = (
    CODE_READ_TOOL_NAME,
    CODE_DRAFT_TOOL_NAME,
    CODE_TEST_TOOL_NAME,
    CODE_PROPOSE_TOOL_NAME,
)

# What the sandbox's checks read; execution/code_checks.py enforces the same list.
CHECKED_ROOTS = frozenset({"tests", "src", "cockpit", "execution", "scripts"})
MAXIMUM_DRAFT_FILES = 40
MAXIMUM_DRAFT_BYTES = 1024 * 1024
BRANCH_PREFIX = "veetbot/"
BASE_BRANCH = "dev"
# These decide what runs with credentials or what the platform allows, so a
# person changes them. ADR-0281.
PROTECTED_PREFIXES = (".circleci/", ".github/", "deploy/", "policy/", "src/agent_core/policy/")


def path_is_protected(path: str) -> bool:
    return path.startswith(PROTECTED_PREFIXES)


class CodeDraftState(StrEnum):
    OPEN = "open"
    PROPOSING = "proposing"
    PROPOSED = "proposed"
    DISCARDED = "discarded"


class CodeProposal(BaseModel):
    """What the owner approved, and the outbox worker's progress opening it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    body: str = Field(max_length=8000)
    branch: str = Field(min_length=1, max_length=120)
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime
    last_error: str | None = Field(default=None, max_length=500)
    commit_sha: str | None = None
    pull_number: int | None = None
    pull_url: str | None = None


class CodeDraft(BaseModel):
    """One owner-scoped set of whole-file changes against a running release.

    A value of None in `files` deletes the path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str
    principal_id: str
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,47}$")
    base_release: str
    base_revision: str
    files: dict[str, str | None] = Field(default_factory=dict)
    state: CodeDraftState = CodeDraftState.OPEN
    version: int = Field(default=1, ge=1)
    proposal: CodeProposal | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _bounded(self) -> CodeDraft:
        """Hold a draft to its limits on files and bytes."""
        if len(self.files) > MAXIMUM_DRAFT_FILES:
            raise ValueError(f"A draft changes at most {MAXIMUM_DRAFT_FILES} files.")
        size = sum(len((content or "").encode()) for content in self.files.values())
        if size > MAXIMUM_DRAFT_BYTES:
            raise ValueError("A draft holds at most 1 MiB of changed files.")
        return self


def content_digest(base_release: str, base_revision: str, files: Mapping[str, str | None]) -> str:
    """Name a change by its content and base, so an approval binds exactly what it showed."""
    canonical = json.dumps(
        {"release": base_release, "revision": base_revision, "files": dict(files)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def draft_digest(draft: CodeDraft) -> str:
    """The digest of a draft's content; its state and version do not change it."""
    return content_digest(draft.base_release, draft.base_revision, draft.files)


class CodeCheckKind(StrEnum):
    PYTEST = "pytest"
    RUFF = "ruff"
    MYPY = "mypy"
    COCKPIT = "cockpit"


class CodeCheck(BaseModel):
    """A closed command: the model picks a kind and repository paths, never an argument."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CodeCheckKind
    targets: tuple[str, ...] = ()
    keyword: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,80}$")


class CodeCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    exit_code: int | None
    timed_out: bool = False
    output: str = ""
    seconds: float = 0.0


class CodeFinding(BaseModel):
    """A screen hit that names the rule and the file, never the matched value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    code: str
    message: str


class CodeReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_sha: str
    pull_number: int
    pull_url: str
