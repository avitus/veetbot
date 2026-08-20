"""Reading-lane floors derived from changed paths (AGENTS.md fast path)."""

import subprocess
from pathlib import Path

from scripts.architecture_checks import minimum_reading_lane, reading_lane_errors
from scripts.check_reading_lane import check, declared_lane, resolve_base


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo_with_lane_commit(tmp_path: Path, path: str, message: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return repo, base


def test_authority_surfaces_require_lane_a() -> None:
    for path in (
        "src/agent_core/policy/engine.py",
        "src/agent_core/ports/tools.py",
        "src/agent_core/memory/formation.py",
        "src/agent_core/execution/__init__.py",
        "tests/gates/test_policy_m4.py",
        "tests/contract/test_tool_contract.py",
        "docs/plan/tool-system.md",
        "docs/status/project-state.yaml",
        "evals/gates/memory.yaml",
        "migrations/versions/a3f19c2b7d04_repository_foundation.py",
        "scripts/check_docs.py",
        "security/secret-allowlist.yaml",
        ".circleci/config.yml",
        "AGENTS.md",
        "Makefile",
    ):
        assert minimum_reading_lane([path]) == "A", path


def test_other_source_and_test_changes_require_lane_b() -> None:
    assert minimum_reading_lane(["src/agent_core/tools/executor.py"]) == "B"
    assert minimum_reading_lane(["tests/unit/test_toolchain.py"]) == "B"
    assert minimum_reading_lane(["clients/apple/Veetbot/Views/ChatView.swift"]) == "B"


def test_docs_only_changes_permit_lane_c() -> None:
    assert minimum_reading_lane(["README.md", "docs/changelog.md"]) == "C"
    assert minimum_reading_lane([]) == "C"


def test_declared_lane_below_minimum_is_rejected() -> None:
    assert reading_lane_errors("C", ["src/agent_core/tools/executor.py"]) == [
        "declared reading lane C is below the minimum B set by src/agent_core/tools/executor.py"
    ]


def test_declared_lane_at_or_above_minimum_is_accepted() -> None:
    assert reading_lane_errors("B", ["src/agent_core/tools/executor.py"]) == []
    assert reading_lane_errors("A", ["README.md"]) == []


def test_unknown_lane_is_rejected() -> None:
    assert reading_lane_errors("D", ["README.md"]) == ["unknown reading lane D; declare A, B, or C"]


def test_mixed_diff_reports_each_escalating_path() -> None:
    errors = reading_lane_errors(
        "C",
        [
            "README.md",
            "src/agent_core/ports/tools.py",
            "src/agent_core/tools/executor.py",
        ],
    )
    assert errors == [
        "declared reading lane C is below the minimum A set by src/agent_core/ports/tools.py",
        "declared reading lane C is below the minimum B set by src/agent_core/tools/executor.py",
    ]


def test_declared_lane_takes_the_newest_trailer_and_defaults_to_a() -> None:
    newest_first = ["fix\n\nReading-Lane: c\n", "feat\n\nReading-Lane: B\n"]
    assert declared_lane(newest_first) == "C"
    assert declared_lane(["fix without a trailer\n"]) == "A"
    assert declared_lane(["prose mentioning Reading-Lane: B mid-line\n"]) == "A"
    assert declared_lane([]) == "A"


def test_check_rejects_an_underdeclared_commit_range(tmp_path: Path) -> None:
    repo, base = _repo_with_lane_commit(
        tmp_path, "src/agent_core/policy/engine.py", "fix\n\nReading-Lane: C"
    )
    declared, minimum, errors = check(repo, base)
    assert (declared, minimum) == ("C", "A")
    assert errors == [
        "declared reading lane C is below the minimum A set by src/agent_core/policy/engine.py"
    ]


def test_check_defaults_an_undeclared_range_to_the_full_order(tmp_path: Path) -> None:
    repo, base = _repo_with_lane_commit(tmp_path, "src/agent_core/policy/engine.py", "fix")
    assert check(repo, base) == ("A", "A", [])


def test_check_accepts_a_true_local_lane(tmp_path: Path) -> None:
    repo, base = _repo_with_lane_commit(tmp_path, "docs/notes.md", "docs\n\nReading-Lane: C")
    assert check(repo, base) == ("C", "C", [])


def test_resolve_base_prefers_remote_then_previous_commit(tmp_path: Path) -> None:
    repo, base = _repo_with_lane_commit(tmp_path, "docs/notes.md", "docs")
    assert resolve_base(repo, "explicit-rev") == "explicit-rev"
    _git(repo, "update-ref", "refs/remotes/origin/dev", base)
    assert resolve_base(repo, None) == "origin/dev"
    _git(repo, "update-ref", "refs/remotes/origin/dev", _git(repo, "rev-parse", "HEAD"))
    assert resolve_base(repo, None) == "HEAD~1"
