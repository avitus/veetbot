"""Reading-lane floors derived from changed paths (AGENTS.md fast path)."""

import subprocess
from pathlib import Path

import pytest

from scripts import check_reading_lane
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


def _commit(repo: Path, path: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write("changed\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


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


def test_each_commit_answers_for_its_own_diff(tmp_path: Path) -> None:
    # PR 135: lane-A commits that each declared A, under a newest commit whose
    # clients/-only diff correctly declared B. The promotion must pass.
    repo, base = _repo_with_lane_commit(
        tmp_path, "migrations/versions/b1_people.py", "feat\n\nReading-Lane: A"
    )
    _commit(repo, "docs/plan/people-and-relationships.md", "docs\n\nReading-Lane: A")
    _commit(repo, "clients/apple/Veetbot/Views/PeopleView.swift", "fix\n\nReading-Lane: B")

    assert check(repo, base) == ("B", "A", [])


def test_a_newest_declaration_covering_the_range_repairs_an_earlier_commit(
    tmp_path: Path,
) -> None:
    repo, base = _repo_with_lane_commit(
        tmp_path, "tests/gates/test_policy_m4.py", "fix\n\nReading-Lane: B"
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", "chore: declare the range\n\nReading-Lane: A")

    assert check(repo, base) == ("A", "A", [])


def test_check_names_each_commit_below_its_own_floor(tmp_path: Path) -> None:
    repo, base = _repo_with_lane_commit(
        tmp_path, "src/agent_core/policy/engine.py", "fix\n\nReading-Lane: B"
    )
    underdeclared = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "clients/apple/Veetbot/Views/ChatView.swift", "fix\n\nReading-Lane: B")

    assert check(repo, base) == (
        "B",
        "A",
        [
            f"{underdeclared[:12]}: declared reading lane B is below the minimum A "
            "set by src/agent_core/policy/engine.py"
        ],
    )


def test_a_merge_answers_only_for_the_paths_it_changed_itself(tmp_path: Path) -> None:
    repo, base = _repo_with_lane_commit(
        tmp_path, "clients/apple/Veetbot/Views/ChatView.swift", "fix\n\nReading-Lane: B"
    )
    _git(repo, "checkout", "-q", "-b", "side", base)
    _commit(repo, "scripts/check_docs.py", "feat\n\nReading-Lane: A")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side\n\nReading-Lane: B")

    assert check(repo, base) == ("B", "A", [])


def test_a_merge_answers_for_a_path_it_changed_itself(tmp_path: Path) -> None:
    repo, base = _repo_with_lane_commit(
        tmp_path, "clients/apple/Veetbot/Views/ChatView.swift", "fix\n\nReading-Lane: B"
    )
    _git(repo, "checkout", "-q", "-b", "side", base)
    _commit(repo, "clients/apple/Veetbot/Views/MemoryView.swift", "fix\n\nReading-Lane: B")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    (repo / "Makefile").write_text("check:\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "merge side\n\nReading-Lane: B")
    merge = _git(repo, "rev-parse", "HEAD")

    assert check(repo, base) == (
        "B",
        "A",
        [f"{merge[:12]}: declared reading lane B is below the minimum A set by Makefile"],
    )


def test_a_path_git_quotes_keeps_its_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Git quotes a non-ASCII path unless -z is given; the floor needs the path.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.quotePath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    repo, base = _repo_with_lane_commit(tmp_path, "scripts/naïve.py", "fix\n\nReading-Lane: C")

    assert check(repo, base) == (
        "C",
        "A",
        ["declared reading lane C is below the minimum A set by scripts/naïve.py"],
    )


def test_main_names_the_basis_of_its_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("READING_LANE_BASE", raising=False)
    monkeypatch.setattr(check_reading_lane, "check", lambda root, base: ("B", "A", []))
    assert check_reading_lane.main([]) == 0
    assert "every commit's declaration covers its own diff" in capsys.readouterr().out

    error = "0123456789ab: declared reading lane B is below the minimum A set by Makefile"
    monkeypatch.setattr(check_reading_lane, "check", lambda root, base: ("B", "A", [error]))
    assert check_reading_lane.main([]) == 1
    assert (
        "repair: a new commit declaring Reading-Lane: A covers the whole range"
        in capsys.readouterr().out
    )


def test_single_commit_repository_classifies_its_own_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    target = repo / "src" / "agent_core" / "policy" / "engine.py"
    target.parent.mkdir(parents=True)
    target.write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "root\n\nReading-Lane: C")

    declared, minimum, errors = check(repo, None)

    assert (declared, minimum) == ("C", "A")
    assert errors == [
        "declared reading lane C is below the minimum A set by src/agent_core/policy/engine.py"
    ]


def test_rename_away_from_an_authority_file_keeps_the_floor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "AGENTS.md").write_text("contract\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "AGENTS.md", "README.md")
    _git(repo, "commit", "-qm", "rename\n\nReading-Lane: C")

    declared, minimum, errors = check(repo, base)

    assert (declared, minimum) == ("C", "A")
    assert errors == ["declared reading lane C is below the minimum A set by AGENTS.md"]


def test_resolve_base_prefers_remote_then_previous_commit(tmp_path: Path) -> None:
    repo, base = _repo_with_lane_commit(tmp_path, "docs/notes.md", "docs")
    assert resolve_base(repo, "explicit-rev") == "explicit-rev"
    _git(repo, "update-ref", "refs/remotes/origin/dev", base)
    assert resolve_base(repo, None) == "origin/dev"
    _git(repo, "update-ref", "refs/remotes/origin/dev", _git(repo, "rev-parse", "HEAD"))
    assert resolve_base(repo, None) == "HEAD~1"
