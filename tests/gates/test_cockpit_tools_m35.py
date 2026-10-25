"""A chat request yields a valid committed widget (Milestone 35, build step 7).

docs/plan/cockpit.md, the tools and the build sequence. The deterministic case
asks Chat for a widget; the scripted model reads the kit and writes a package
through `cockpit.widget.write`, the outbox commits it to the branch, and the
widget ends live with exactly those files. The package is the
`approvals-waiting` reference widget, whose real build the workspace's builder
tests prove, so the case's package is valid by more than the evaluation's own
manifest check.
"""

from __future__ import annotations

from pathlib import Path

from agent_core.evals.cases import EvalCase, load_cases
from agent_core.evals.fixtures import resolve_model_fixture
from agent_core.evals.runner import run_case

ROOT = Path(__file__).resolve().parents[2]
CASE = "cockpit_chat_writes_a_widget"
REFERENCE = ROOT / "cockpit/examples/widgets/approvals-waiting"


def _case() -> EvalCase:
    matches = [case for case in load_cases(ROOT / "tests/eval_cases") if case.name == CASE]
    assert matches, f"the {CASE} evaluation case exists"
    return matches[0]


def _reference() -> dict[str, str]:
    return {
        path.relative_to(REFERENCE).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(REFERENCE.rglob("*"))
        if path.is_file()
    }


def test_the_case_writes_the_reference_package_the_real_builder_proves() -> None:
    case = _case()
    script = resolve_model_fixture(ROOT / "evals/fixtures/models", case.model_fixture)
    writes = [
        call
        for turn in script.turns
        for call in turn.tool_calls
        if call.name == "cockpit.widget.write"
    ]
    assert len(writes) == 1
    arguments = writes[0].arguments
    assert isinstance(arguments, dict)
    assert arguments["operation"] == "create"
    assert arguments["slug"] == "approvals-waiting"
    assert arguments["files"] == _reference()


async def test_a_chat_request_yields_a_valid_committed_widget() -> None:
    result = await run_case(_case(), ROOT / "evals/fixtures/models")
    assert [widget.slug for widget in result.widgets] == ["approvals-waiting"]
    (widget,) = result.widgets
    assert widget.state == "live"
    assert widget.commit_sha is not None
    assert dict(widget.branch_files) == _reference()
