#!/usr/bin/env python3
"""Deterministic documentation validation.

Runs practical structural checks, then (unless ``--no-build`` is passed) a strict
MkDocs build and the single-HTML build via ``scripts/build_docs.py``. Exits
non-zero if any check fails. This is intentionally not a prose-comparison system;
it verifies structure, links, and the absence of conversion artifacts.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import yaml
from gate_registry import MAX_MILESTONE, load_registry, registry_errors

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "plan" / "engineering-plan.md"
AGENTS = ROOT / "AGENTS.md"
AGENTS_MAX_BYTES = 12 * 1024
AGENTS_MAX_LINES = 200

errors: list[str] = []
notes: list[str] = []


def err(m: str) -> None:
    errors.append(m)


def note(m: str) -> None:
    notes.append(m)


def slugify(value: str) -> str:
    """Match python-markdown's default toc slugify (used by MkDocs)."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def lines_without_code(text: str) -> list[str]:
    out, in_fence = [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out


def check_required_files() -> None:
    required = [
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "mkdocs.yml",
        "docs-manifest.yaml",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        ".env.example",
        "docker-compose.yml",
        "alembic.ini",
        "docs/security.md",
        "docs/architecture.md",
        "docs/events.md",
        "archive/README.md",
        "archive/Modular_General_Purpose_AI_Agent_Engineering_Plan.docx",
        "docs/index.md",
        "docs/changelog.md",
        "docs/plan/engineering-plan.md",
        "docs/plan/current-milestone.md",
        "docs/status/project-state.yaml",
        "docs/status/verification-history.yaml",
        "docs/status/corpus-audit-log.md",
        "docs/status/index.md",
        "docs/assets/stylesheets/extra.css",
        "scripts/build_docs.py",
        "scripts/check_docs.py",
        "scripts/check_citations.py",
        "docs/status/citation-ledger.yaml",
        ".github/copilot-instructions.md",
        ".circleci/config.yml",
        "evals/gates/structure.yaml",
        "evals/gates/harness.yaml",
        "migrations/versions/a3f19c2b7d04_repository_foundation.py",
    ]
    for rel in required:
        if not (ROOT / rel).exists():
            err(f"required file missing: {rel}")


def check_project_state() -> None:
    p = ROOT / "docs" / "status" / "project-state.yaml"
    if not p.is_file():
        return
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        err(f"project-state.yaml does not parse: {exc}")
        return
    milestones = (data or {}).get("milestones", {})
    for n in range(0, MAX_MILESTONE + 1):
        if str(n) not in milestones:
            err(f"project-state.yaml missing milestone key '{n}'")
    current = (data or {}).get("project", {}).get("current_milestone")
    if not isinstance(current, int) or not 0 <= current <= MAX_MILESTONE:
        err(
            "project-state.yaml current_milestone must be an integer from 0 to "
            f"{MAX_MILESTONE} (got {current!r})"
        )


def project_current_milestone() -> int:
    path = ROOT / "docs" / "status" / "project-state.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        current = data["project"]["current_milestone"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return 0
    return current if isinstance(current, int) and 0 <= current <= MAX_MILESTONE else 0


def read_front_matter(text: str):
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        return yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None


def check_plan() -> None:
    if not PLAN.is_file():
        err("engineering-plan.md missing")
        return
    text = PLAN.read_text(encoding="utf-8")

    fm = read_front_matter(text)
    if not isinstance(fm, dict) or not fm.get("canonical"):
        err("engineering-plan.md front matter is missing or not marked canonical")

    nocode = lines_without_code(text)
    joined = "\n".join(nocode)

    h1 = [line for line in nocode if re.match(r"# [^#]", line)]
    if len(h1) != 1:
        err(f"engineering-plan.md must have exactly one level-one heading (found {len(h1)})")

    for n in range(0, MAX_MILESTONE + 1):
        if not re.search(rf"^#+ Milestone {n}:", joined, re.M):
            err(f"engineering-plan.md missing 'Milestone {n}' section")

    if not re.search(r"^#+ 26\. First assignment for the coding agent", joined, re.M):
        err("engineering-plan.md missing 'First assignment for the coding agent' section")

    nums = sorted({int(m.group(1)) for m in re.finditer(r"^#+ (\d+)\. ", joined, re.M)})
    if not nums:
        err("engineering-plan.md has no numbered sections")
    else:
        mx = max(nums)
        gaps = [n for n in range(1, mx + 1) if n not in nums]
        if gaps:
            err(f"engineering-plan.md numbered sections are not contiguous; missing {gaps}")
        if mx < 26:
            err(f"engineering-plan.md has only {mx} numbered sections (expected at least 26)")
        note(f"numbered sections present: 1..{mx} ({len(nums)} total)")

    artifacts = [
        r"<w:",
        r"\bxmlns:",
        r"OOXML",
        r"Page \d+ of \d+",
        r"MODULAR GENERAL-PURPOSE AI AGENT\s*\|\s*ENGINEERING PLAN",
    ]
    for pat in artifacts:
        if re.search(pat, text, re.I):
            err(f"possible Word-conversion artifact in engineering-plan.md: /{pat}/")


def check_current_milestone_links() -> None:
    cm = ROOT / "docs" / "plan" / "current-milestone.md"
    if not cm.is_file() or not PLAN.is_file():
        return
    plan_nocode = "\n".join(lines_without_code(PLAN.read_text(encoding="utf-8")))
    slugs = {slugify(m.group(1)) for m in re.finditer(r"^#+\s+(.*\S)\s*$", plan_nocode, re.M)}
    found = False
    for m in re.finditer(r"\]\(engineering-plan\.md#([\w-]+)\)", cm.read_text(encoding="utf-8")):
        found = True
        anchor = m.group(1)
        if anchor not in slugs:
            err(f"current-milestone.md links to a heading that does not exist: #{anchor}")
    if not found:
        err("current-milestone.md has no links into the engineering plan")


def check_manifest() -> None:
    mf = ROOT / "docs-manifest.yaml"
    if not mf.is_file():
        return
    try:
        data = yaml.safe_load(mf.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        err(f"docs-manifest.yaml does not parse: {exc}")
        return
    for rel in (data or {}).get("sources", []):
        if str(rel).startswith(("site/", "dist/")):
            err(f"manifest source must not be a generated file: {rel}")
        elif not (ROOT / rel).is_file():
            err(f"manifest source does not exist: {rel}")


def check_no_root_docx_links() -> None:
    md_files = list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))
    for md in md_files:
        for m in re.finditer(r"\]\(([^)]*\.docx)\)", md.read_text(encoding="utf-8")):
            path = m.group(1)
            if not path.startswith("archive/"):
                err(f"{md.relative_to(ROOT)} links to a non-archive .docx path: {path}")


def check_agents_size() -> None:
    if not AGENTS.is_file():
        return
    data = AGENTS.read_bytes()
    line_count = AGENTS.read_text(encoding="utf-8").count("\n") + 1
    if len(data) > AGENTS_MAX_BYTES:
        err(f"AGENTS.md is too large: {len(data)} bytes (limit {AGENTS_MAX_BYTES})")
    if line_count > AGENTS_MAX_LINES:
        err(f"AGENTS.md is too long: {line_count} lines (limit {AGENTS_MAX_LINES})")
    note(f"AGENTS.md size: {len(data)} bytes, {line_count} lines")


def check_citations() -> None:
    """Line-number citations must still point at the text they cited.

    Delegated to scripts/check_citations.py, which owns the ledger. Run it
    with --update to repoint citations an edit has moved.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_citations.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    tail = [ln for ln in result.stdout.splitlines() if ln.startswith("  - ")]
    if result.returncode != 0:
        err(f"citation check failed with {len(tail)} drifted citation(s)")
        for ln in tail:
            err(f"  {ln.strip()[2:]}")
    else:
        for ln in result.stdout.splitlines():
            if ln.startswith("note: ") and "checked" in ln:
                note(ln[6:])


def check_gate_registry() -> None:
    """Reconcile declarations, map rows, checks, anchors, and census."""
    current_milestone = project_current_milestone()
    findings = registry_errors(ROOT, current_milestone=current_milestone)
    if findings:
        for finding in findings:
            err(f"gate registry: {finding}")
    else:
        entries, _ = load_registry(ROOT)
        active = sum(entry.milestone <= current_milestone for entry in entries)
        note(
            f"gate registry: {len(entries)} entries reconciled; {active} gates active through "
            f"Milestone {current_milestone}"
        )


def run_builds() -> None:
    print("Running strict MkDocs build and single-HTML build (scripts/build_docs.py) ...")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_docs.py")], cwd=ROOT)
    if result.returncode != 0:
        err("documentation build failed (see output above)")


def main() -> None:
    check_required_files()
    check_project_state()
    check_plan()
    check_current_milestone_links()
    check_manifest()
    check_no_root_docx_links()
    check_agents_size()
    check_citations()
    check_gate_registry()
    if "--no-build" not in sys.argv:
        run_builds()

    print("\n--- check_docs summary ---")
    for n in notes:
        print("note:", n)
    if errors:
        print(f"\nFAILED with {len(errors)} error(s):")
        for e in errors:
            print("  -", e)
        raise SystemExit(1)
    print("All documentation checks passed.")


if __name__ == "__main__":
    main()
