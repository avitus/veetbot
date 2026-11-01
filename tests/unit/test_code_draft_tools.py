"""ADR-0281: Veetbot drafts, tests and proposes changes, and its power stops at a pull request."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.code.source_tree import FilesystemSourceTree
from agent_core.adapters.determinism import RandomIdFactory
from agent_core.application.code_drafts import CodeDrafts
from agent_core.domain.code import (
    CodeCheck,
    CodeCheckKind,
    CodeCheckResult,
    CodeDraftState,
    CodeFinding,
    CodeProposal,
    draft_digest,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolResult
from agent_core.tools.code import CodeDraftTool, CodeProposeTool, CodeTestTool, CodeToolset
from tests.contract.support import NOW, memory_uow_factory, principal, tool_context

RELEASE = "20261101-120000-abc1234"


def _text(result: ToolResult) -> str:
    """The result's first content part, as text."""
    part = result.content[0]
    assert isinstance(part, TextPart)
    return part.text


class Checker:
    def __init__(self) -> None:
        self.files: Any = None
        self.check: CodeCheck | None = None
        self.result = CodeCheckResult(passed=True, exit_code=0, output="3 passed", seconds=1.5)

    async def run(self, *, files: Any, check: CodeCheck, **kwargs: Any) -> CodeCheckResult:
        self.files, self.check = files, check
        return self.result


class Screen:
    def __init__(self, findings: tuple[CodeFinding, ...] = ()) -> None:
        self.findings = findings
        self.added: Any = None
        self.message: str | None = None

    async def screen(self, *, added_lines: Any, commit_message: str, **kwargs: Any) -> Any:
        self.added, self.message = added_lines, commit_message
        return self.findings


class Harness:
    def __init__(self, root: Path, clock: Any, factory: Any) -> None:
        self.root = root
        self.clock = clock
        self.factory = factory
        self.tree = FilesystemSourceTree(root, release_id=RELEASE)
        self.checker = Checker()
        self.screen = Screen()
        self.toolset = CodeToolset(
            drafts=CodeDrafts(
                tree=self.tree, uow_factory=factory, clock=clock, ids=RandomIdFactory()
            ),
            uow_factory=factory,
            clock=clock,
            checker=self.checker,
            screen=self.screen,
            proposal_wait_seconds=2,
        )
        self.draft = CodeDraftTool(self.toolset)
        self.test = CodeTestTool(self.toolset)
        self.propose = CodeProposeTool(self.toolset)


def _context(**changes: Any) -> Any:
    owner = principal().model_copy(update={"scopes": {"code.read", "code.write"}})
    return replace(tool_context(), principal=owner, **changes)


@pytest.fixture
async def harness(tmp_path: Path) -> Harness:
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "deploy").mkdir()
    (tmp_path / ".circleci").mkdir()
    (tmp_path / "src/app.py").write_text("limit = 1\nname = 'a'\nlimit = 1\nother = 2\n")
    (tmp_path / "deploy/release.sh").write_text("echo\n")
    (tmp_path / ".circleci/config.yml").write_text("a: 1\n")
    clock, factory = await memory_uow_factory()
    return Harness(tmp_path, clock, factory)


async def _open(harness: Harness, slug: str = "fix-limit") -> None:
    result = await harness.draft.execute({"operation": "open", "slug": slug}, _context())
    assert result.ok


async def _edit(harness: Harness, **arguments: Any) -> Any:
    return await harness.draft.execute(
        {"operation": "edit", "slug": "fix-limit", "path": "src/app.py", **arguments}, _context()
    )


async def _proposal(harness: Harness, **overrides: Any) -> dict[str, Any]:
    """Proposal arguments carrying the digest code.draft reports for the current draft."""
    shown = await harness.draft.execute({"operation": "show", "slug": "fix-limit"}, _context())
    assert shown.structured is not None
    return {
        "slug": "fix-limit",
        "title": "Fix the limit",
        "summary": "Why it matters.",
        "digest": shown.structured["digest"],
        **overrides,
    }


async def test_an_edit_needs_one_exact_match_and_show_gives_the_diff(harness: Harness) -> None:
    await _open(harness)
    ambiguous = await _edit(harness, old="limit = 1", new="limit = 2")
    assert not ambiguous.ok
    assert ambiguous.failure is not None
    assert ambiguous.failure.reason_code == "code.old_not_unique"
    assert "2 times" in ambiguous.failure.detail

    changed = await _edit(harness, old="other = 2", new="other = 3")
    assert changed.ok, changed.failure
    assert changed.structured is not None
    assert changed.structured["files"] == ["src/app.py"]

    shown = await harness.draft.execute({"operation": "show", "slug": "fix-limit"}, _context())
    assert "-other = 2" in _text(shown)
    assert "+other = 3" in _text(shown)


async def test_returning_a_file_to_its_release_content_drops_the_change(harness: Harness) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    restored = await _edit(harness, old="other = 3", new="other = 2")
    assert restored.structured is not None
    assert restored.structured["files"] == []


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("deploy/release.sh", "code.path_protected"),
        (".circleci/config.yml", "code.path_protected"),
        ("deploy/new.sh", "code.path_protected"),
        ("src/agent_core/policy/hardline.py", "code.path_protected"),
        (".release.env", "code.path_refused"),
        ("../outside.py", "code.path_refused"),
    ],
)
async def test_people_keep_ci_deployment_and_policy_files(
    harness: Harness, path: str, reason: str
) -> None:
    await _open(harness)
    result = await harness.draft.execute(
        {"operation": "edit", "slug": "fix-limit", "path": path, "content": "x"}, _context()
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == reason


async def test_a_new_release_makes_a_draft_stale_instead_of_mixing_two_bases(
    harness: Harness,
) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    harness.tree._release_id = "20261102-120000-def5678"
    stale = await _edit(harness, old="other = 3", new="other = 4")
    assert stale.failure is not None
    assert stale.failure.reason_code == "code.draft_stale"
    tested = await harness.test.execute(
        {"slug": "fix-limit", "kind": "pytest", "targets": ["tests/unit"]}, _context()
    )
    assert tested.failure is not None
    assert tested.failure.reason_code == "code.draft_stale"
    assert harness.checker.check is None


async def test_a_check_gets_the_draft_and_its_output_is_untrusted(harness: Harness) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    result = await harness.test.execute(
        {"slug": "fix-limit", "kind": "pytest", "targets": ["tests/unit"], "keyword": "limit"},
        _context(),
    )
    assert result.ok
    assert harness.checker.files == {"src/app.py": "limit = 1\nname = 'a'\nlimit = 1\nother = 3\n"}
    assert harness.checker.check == CodeCheck(
        kind=CodeCheckKind.PYTEST, targets=("tests/unit",), keyword="limit"
    )
    assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert result.structured is not None
    assert result.structured["passed"] is True
    assert "3 passed" in _text(result)


async def test_a_sandbox_failure_is_never_read_as_a_pass(harness: Harness) -> None:
    await _open(harness)

    async def broken(**kwargs: Any) -> CodeCheckResult:
        raise RuntimeError("execution service down")

    harness.checker.run = broken  # type: ignore[method-assign]
    result = await harness.test.execute(
        {"slug": "fix-limit", "kind": "pytest", "targets": ["tests/unit"]}, _context()
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "code.check_failed"


@pytest.mark.parametrize(
    "changes",
    [{"run_kind": "scheduled"}, {"run_kind": "delegated"}],
)
async def test_changes_start_only_in_interactive_chat(harness: Harness, changes: Any) -> None:
    context = _context(**changes)
    for tool, arguments in (
        (harness.draft, {"operation": "open", "slug": "fix-limit"}),
        (harness.test, {"slug": "fix-limit", "kind": "pytest", "targets": ["tests/unit"]}),
        (harness.propose, {"slug": "fix-limit", "title": "T", "summary": "S"}),
    ):
        result = await tool.execute(arguments, context)
        assert not result.ok
        assert result.failure is not None
        assert result.failure.reason_code == "code.interactive_only"


def test_a_proposal_always_asks_the_owner_and_tests_are_isolated() -> None:
    assert CodeProposeTool.spec.side_effect is SideEffectClass.PUBLICATION
    assert CodeProposeTool.spec.idempotency is IdempotencyClass.CONDITIONALLY_IDEMPOTENT
    assert CodeTestTool.spec.side_effect is SideEffectClass.CODE_EXECUTION
    assert CodeTestTool.spec.target_kind == "sandbox"


async def test_a_screen_hit_publishes_nothing_and_never_repeats_the_value(
    harness: Harness,
) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 'owner@example.com'")
    harness.screen.findings = (
        CodeFinding(file="src/app.py", code="email", message="contains an email address"),
    )
    result = await harness.propose.execute(await _proposal(harness), _context())
    assert result.failure is not None
    assert result.failure.reason_code == "code.screen_refused"
    assert "owner@example.com" not in result.failure.detail
    # Only added lines are screened: the file's existing lines are already public.
    assert harness.screen.added == {"src/app.py": "other = 'owner@example.com'"}
    async with harness.factory() as uow:
        draft = await uow.code_drafts.get(principal(), "fix-limit")
    assert draft.state is CodeDraftState.OPEN
    assert draft.proposal is None


async def test_an_empty_draft_is_not_proposed(harness: Harness) -> None:
    await _open(harness)
    result = await harness.propose.execute(await _proposal(harness), _context())
    assert result.failure is not None
    assert result.failure.reason_code == "code.draft_empty"


async def test_a_proposal_queues_for_the_worker_and_a_replay_opens_nothing_new(
    harness: Harness,
) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    arguments = await _proposal(harness)

    queued = await harness.propose.execute(arguments, _context())
    assert queued.ok
    assert queued.structured is not None
    assert queued.structured["state"] == "proposing"
    assert queued.structured["branch"].startswith("veetbot/fix-limit-")
    assert queued.structured["pull_url"] is None
    assert "queued" in _text(queued)

    async with harness.factory() as uow:
        draft = await uow.code_drafts.get(principal(), "fix-limit")
        assert draft.proposal is not None
        assert draft.proposal.title == "Fix the limit"
        assert "Hosted CI has not run" in draft.proposal.body
        version = draft.version
        proposal: CodeProposal = draft.proposal.model_copy(
            update={"pull_url": "https://example.test/pull/9", "pull_number": 9}
        )
        await uow.code_drafts.put(
            draft.model_copy(
                update={
                    "state": CodeDraftState.PROPOSED,
                    "proposal": proposal,
                    "version": version + 1,
                }
            ),
            expected_version=version,
        )

    harness.screen.message = None
    replay = await harness.propose.execute(arguments, _context())
    assert replay.ok
    assert replay.structured is not None
    assert replay.structured["pull_url"] == "https://example.test/pull/9"
    assert "https://example.test/pull/9" in _text(replay)
    assert harness.screen.message is None


async def test_an_edit_after_the_owner_was_asked_publishes_nothing(harness: Harness) -> None:
    """The owner approves the exact change on the card, not whatever the slug holds later."""
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    arguments = await _proposal(harness)
    view = await harness.propose.approval_view_in_session(
        arguments, run=None, principal=principal(), turn=None, not_covered=None
    )
    assert "+other = 3" in view.arguments["diff"]

    await _edit(harness, old="other = 3", new="other = 4")
    result = await harness.propose.execute(arguments, _context())

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "code.draft_changed"
    assert harness.screen.message is None
    async with harness.factory() as uow:
        draft = await uow.code_drafts.get(principal(), "fix-limit")
    assert draft.state is CodeDraftState.OPEN
    assert draft.proposal is None


async def test_the_card_never_shows_a_diff_other_than_the_one_being_approved(
    harness: Harness,
) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    arguments = await _proposal(harness)
    await _edit(harness, old="other = 3", new="other = 4")

    view = await harness.propose.approval_view_in_session(
        arguments, run=None, principal=principal(), turn=None, not_covered=None
    )

    assert view.arguments["diff_status"] == "changed"
    assert "diff" not in view.arguments
    assert "file_changes" not in view.arguments


async def test_an_edit_while_the_screen_runs_publishes_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check before screening cannot see an edit that lands during it."""
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    arguments = await _proposal(harness)
    screened = harness.screen.screen

    async def edit_during_the_screen(**kwargs: Any) -> Any:
        await _edit(harness, old="other = 3", new="other = 4")
        return await screened(**kwargs)

    monkeypatch.setattr(harness.screen, "screen", edit_during_the_screen)
    result = await harness.propose.execute(arguments, _context())

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "code.draft_changed"
    async with harness.factory() as uow:
        draft = await uow.code_drafts.get(principal(), "fix-limit")
    assert draft.state is CodeDraftState.OPEN
    assert draft.proposal is None


async def test_a_draft_discarded_and_reopened_after_the_ask_publishes_nothing(
    harness: Harness,
) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    arguments = await _proposal(harness)
    await harness.draft.execute({"operation": "discard", "slug": "fix-limit"}, _context())
    await _open(harness)

    result = await harness.propose.execute(arguments, _context())

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "code.draft_changed"


async def test_a_replay_for_different_content_is_not_the_recorded_proposal(
    harness: Harness,
) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    arguments = await _proposal(harness)
    assert (await harness.propose.execute(arguments, _context())).ok

    replay = await harness.propose.execute({**arguments, "digest": "0" * 32}, _context())

    assert not replay.ok
    assert replay.failure is not None
    assert replay.failure.reason_code == "code.draft_changed"


async def test_the_digest_covers_the_content_and_the_base_but_not_the_lifecycle(
    harness: Harness,
) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    async with harness.factory() as uow:
        draft = await uow.code_drafts.get(principal(), "fix-limit")
    digest = draft_digest(draft)
    assert draft_digest(draft.model_copy(update={"version": 9, "updated_at": NOW})) == digest
    assert draft_digest(draft.model_copy(update={"files": {"src/app.py": "x\n"}})) != digest
    assert draft_digest(draft.model_copy(update={"base_revision": "fffffff"})) != digest
    assert draft_digest(draft.model_copy(update={"files": {"a": None}})) != draft_digest(
        draft.model_copy(update={"files": {"a": ""}})
    )


async def test_the_approval_card_shows_the_diff_in_server_words(harness: Harness) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    view = await harness.propose.approval_view_in_session(
        await _proposal(harness, title="Ignore rules", summary="Trust me"),
        run=None,
        principal=principal(),
        turn=None,
        not_covered=None,
    )
    assert "public repository" in view.summary
    assert "cannot merge" in view.summary
    assert "Ignore rules" not in view.summary
    assert view.arguments["file_changes"] == ["src/app.py"]
    assert "+other = 3" in view.arguments["diff"]
    assert NOW is not None


async def test_a_check_must_name_its_targets_and_skips_files_it_cannot_read(
    harness: Harness,
) -> None:
    await _open(harness)
    await _edit(harness, old="other = 2", new="other = 3")
    await harness.draft.execute(
        {"operation": "edit", "slug": "fix-limit", "path": "docs/new.md", "content": "New\n"},
        _context(),
    )
    untargeted = await harness.test.execute(
        {"slug": "fix-limit", "kind": "pytest", "targets": []}, _context()
    )
    assert untargeted.failure is not None
    assert untargeted.failure.reason_code == "code.targets_required"
    result = await harness.test.execute(
        {"slug": "fix-limit", "kind": "ruff", "targets": ["src"]}, _context()
    )
    assert set(harness.checker.files) == {"src/app.py"}
    assert "Not applied, outside what checks read: docs/new.md" in _text(result)
