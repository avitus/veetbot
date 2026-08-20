"""Load and validate the canonical hard-gate registry."""

from __future__ import annotations

import ast
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

GATE_ID = re.compile(
    r"^gate\.(structure|runtime|tool|builtin|model|policy|event|context|memory|"
    r"harness|api|sandbox|skill|knowledge|web|browser)\.[a-z0-9]+(?:_[a-z0-9]+)*$"
)
MAP_ROW = re.compile(
    r"^\s*(?:\d+\s+)?(gate\.[a-z0-9_.]+)\s+"
    r"(case|property|corpus|structural)\s+(\d+)(?:\s|$)",
    re.MULTILINE,
)
REQUIRED_FIELDS = {"id", "milestone", "kind", "spec", "statement", "check"}

# declared count, aliases owned elsewhere
DECLARING_SPECS: dict[str, tuple[int, int]] = {
    "runtime-loop.md": (14, 2),
    "tool-system.md": (16, 1),
    "builtin-tools.md": (15, 0),
    "model-gateway.md": (12, 0),
    "policy-and-approvals.md": (13, 0),
    "event-log-and-persistence.md": (14, 0),
    "context-engine.md": (6, 0),
    "memory-formation-and-consolidation.md": (16, 0),
    "memory-retrieval-and-ranking.md": (9, 0),
    "evaluation-harness.md": (11, 0),
    "http-api-and-streaming.md": (10, 0),
    "sandbox-isolation.md": (13, 0),
    "skills.md": (16, 0),
    "knowledge-documents.md": (12, 0),
    "web-access.md": (7, 0),
    "browser-automation.md": (10, 0),
    "milestone-map.md": (7, 0),
}


@dataclass(frozen=True, slots=True)
class GateEntry:
    """One normalized gate registry entry."""

    id: str
    milestone: int
    kind: str
    spec: str
    statement: str
    check: str
    optional: bool = False
    corpus: str | None = None
    minimum_members: int | None = None


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = re.sub(r"[^\w\s-]", "", normalized).strip().lower()
    return re.sub(r"[-\s]+", "-", normalized)


def load_registry(root: Path) -> tuple[list[GateEntry], list[str]]:
    """Load all area files, reporting schema failures without stopping early."""

    entries: list[GateEntry] = []
    errors: list[str] = []
    for path in sorted((root / "evals" / "gates").glob("*.yaml")):
        try:
            loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{path.relative_to(root)} cannot be loaded: {exc}")
            continue
        if not isinstance(loaded, list):
            errors.append(f"{path.relative_to(root)} must contain a list")
            continue
        for index, raw in enumerate(loaded, 1):
            where = f"{path.relative_to(root)} entry {index}"
            if not isinstance(raw, dict):
                errors.append(f"{where} must be a mapping")
                continue
            missing = REQUIRED_FIELDS - raw.keys()
            if missing:
                errors.append(f"{where} is missing fields: {', '.join(sorted(missing))}")
                continue
            try:
                entry = GateEntry(
                    id=str(raw["id"]),
                    milestone=int(raw["milestone"]),
                    kind=str(raw["kind"]),
                    spec=str(raw["spec"]),
                    statement=str(raw["statement"]),
                    check=str(raw["check"]),
                    optional=bool(raw.get("optional", False)),
                    corpus=None if raw.get("corpus") is None else str(raw["corpus"]),
                    minimum_members=(
                        None if raw.get("minimum_members") is None else int(raw["minimum_members"])
                    ),
                )
            except (TypeError, ValueError) as exc:
                errors.append(f"{where} has invalid field types: {exc}")
                continue
            parts = entry.id.split(".")
            if len(parts) < 3:
                errors.append(f"{where} has a malformed identifier: {entry.id}")
            elif parts[1] != path.stem:
                errors.append(f"{where} belongs in {parts[1]}.yaml")
            entries.append(entry)
    if not entries:
        errors.append("evals/gates/*.yaml contains no gate entries")
    return entries, errors


def map_entries(root: Path) -> dict[str, tuple[str, int]]:
    """Read the identifier, kind, and milestone columns from the milestone map."""

    path = root / "docs" / "plan" / "milestone-map.md"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    return {gate_id: (kind, int(milestone)) for gate_id, kind, milestone in MAP_ROW.findall(text)}


def hard_gate_items(path: Path) -> list[str]:
    """Return top-level numbered items from a document's hard-gates section."""

    text = path.read_text(encoding="utf-8")
    match = re.search(r"^## Hard gates\s*$", text, re.MULTILINE)
    if match is None:
        return []
    body_start = match.end()
    next_heading = re.search(r"^## (?!#)", text[body_start:], re.MULTILINE)
    body = (
        text[body_start : body_start + next_heading.start()] if next_heading else text[body_start:]
    )
    starts = list(re.finditer(r"^\d+\.\s+", body, re.MULTILINE))
    return [
        body[start.start() : starts[index + 1].start() if index + 1 < len(starts) else len(body)]
        for index, start in enumerate(starts)
    ]


def _check_resolves(root: Path, reference: str) -> bool:
    path_text, separator, symbol = reference.partition("::")
    path = root / path_text
    if not separator or not symbol or not path.is_file():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return False
    first = symbol.split(".")[0]
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == first
        for node in tree.body
    )


_GATE_TABLE_FIGURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subject_specs", re.compile(r"(\d+)\s+subject\s+specifications\s+declare")),
    ("subject_gates", re.compile(r"declare\s+(\d+)\s+gates")),
    ("declarations", re.compile(r"(\d+)\s+declarations")),
    ("entries", re.compile(r"(\d+)\s+registry\s+entries")),
    ("aliases", re.compile(r"the\s+(\d+)\s+aliases")),
)


def gate_table_arithmetic_errors(map_text: str, derived: dict[str, int]) -> list[str]:
    """Reconcile the gate-table intro paragraph's digits against the registry."""

    heading = re.search(r"^## The gate table\s*$", map_text, re.MULTILINE)
    if heading is None:
        return ["gate table section is missing from the milestone map"]
    intro = map_text[heading.end() :].partition("```")[0]
    errors: list[str] = []
    for name, pattern in _GATE_TABLE_FIGURES:
        label = name.replace("_", " ")
        match = pattern.search(intro)
        if match is None:
            errors.append(f"gate table intro does not state its {label} figure")
        elif int(match.group(1)) != derived[name]:
            errors.append(
                f"gate table intro says {match.group(1)} {label}; registry derives {derived[name]}"
            )
    return errors


def registry_errors(root: Path, current_milestone: int = 0) -> list[str]:
    """Validate the registry and all six Milestone 0 map invariants."""

    entries, errors = load_registry(root)
    by_id = {entry.id: entry for entry in entries}
    duplicate_ids = sorted(
        gate_id for gate_id, count in Counter(e.id for e in entries).items() if count > 1
    )
    if duplicate_ids:
        errors.append(f"duplicate gate identifiers: {', '.join(duplicate_ids)}")

    map_path = root / "docs" / "plan" / "milestone-map.md"
    if not map_path.is_file():
        errors.append("docs/plan/milestone-map.md is missing")
        return errors
    map_text = map_path.read_text(encoding="utf-8")
    expected = {
        gate_id: (kind, int(milestone)) for gate_id, kind, milestone in MAP_ROW.findall(map_text)
    }
    if set(by_id) != set(expected):
        missing = sorted(set(expected) - set(by_id))
        extra = sorted(set(by_id) - set(expected))
        if missing:
            errors.append(f"registry is missing map identifiers: {', '.join(missing)}")
        if extra:
            errors.append(f"registry has identifiers absent from map: {', '.join(extra)}")

    for gate_id, entry in by_id.items():
        if GATE_ID.fullmatch(gate_id) is None:
            errors.append(f"invalid gate identifier: {gate_id}")
        if entry.kind not in {"case", "property", "corpus", "structural"}:
            errors.append(f"{gate_id} has invalid kind {entry.kind}")
        if entry.milestone < 0 or entry.milestone > 10:
            errors.append(f"{gate_id} has invalid milestone {entry.milestone}")
        mapped = expected.get(gate_id)
        if mapped is not None and mapped != (entry.kind, entry.milestone):
            errors.append(
                f"{gate_id} disagrees with milestone map: "
                f"registry={entry.kind}/{entry.milestone}, map={mapped[0]}/{mapped[1]}"
            )
        if not entry.statement.strip():
            errors.append(f"{gate_id} has an empty statement")
        if entry.statement == f"{gate_id} is defined by its declaring specification.":
            errors.append(f"{gate_id} has a placeholder statement")
        if not _check_resolves(root, entry.check):
            errors.append(f"{gate_id} check does not resolve: {entry.check}")
        if entry.milestone <= current_milestone:
            if entry.check == "tests/gates/pending.py::pending_gate":
                errors.append(f"active gate {gate_id} still points at the pending check")
            if entry.optional:
                errors.append(f"active gate {gate_id} may not be optional")
            if entry.kind == "corpus":
                if entry.corpus is None or entry.minimum_members is None:
                    errors.append(f"active corpus gate {gate_id} lacks corpus metadata")
                elif entry.minimum_members <= 0:
                    errors.append(f"active corpus gate {gate_id} has no positive minimum")
                elif not (root / entry.corpus).is_dir():
                    errors.append(f"active corpus gate {gate_id} corpus is not a directory")

    spec_counts = Counter(entry.spec.split("#", 1)[0].rsplit("/", 1)[-1] for entry in entries)
    for filename, (declared, aliases) in DECLARING_SPECS.items():
        path = root / "docs" / "plan" / filename
        items = hard_gate_items(path)
        if len(items) != declared:
            errors.append(f"{filename} declares {len(items)} hard gates; expected {declared}")
        expected_owned = declared - aliases
        if spec_counts[filename] != expected_owned:
            errors.append(
                f"{filename} owns {spec_counts[filename]} registry entries; "
                f"expected {expected_owned}"
            )
        for number, item in enumerate(items, 1):
            tokens = re.findall(r"\*\*M(\d+)\.\*\*", item)
            if len(tokens) != 1:
                errors.append(
                    f"{filename} hard gate {number} must carry exactly one milestone token"
                )

    for entry in entries:
        path_text, separator, anchor = entry.spec.partition("#")
        path = root / path_text
        if not separator or not path.is_file():
            errors.append(f"{entry.id} has an invalid spec link: {entry.spec}")
            continue
        headings = {
            _slugify(match.group(1))
            for match in re.finditer(
                r"^#{1,6}\s+(.*\S)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE
            )
        }
        if anchor not in headings:
            errors.append(f"{entry.id} spec anchor does not resolve: {entry.spec}")

    _before, separator, after = map_text.partition("## The census")
    if not separator:
        errors.append("docs/plan/milestone-map.md has no 'The census' section")
        return errors
    census_section = after.partition("## ")[0]
    written = {
        int(milestone): (int(new), int(cumulative))
        for milestone, new, cumulative in re.findall(
            r"^(\d+)\s+(\d+)\s+(\d+)\s+", census_section, re.MULTILINE
        )
    }
    counts = Counter(entry.milestone for entry in entries)
    cumulative = 0
    derived: dict[int, tuple[int, int]] = {}
    for milestone in range(11):
        cumulative += counts[milestone]
        derived[milestone] = (counts[milestone], cumulative)
    if written != derived:
        errors.append(
            f"milestone census disagrees with registry: written={written}, derived={derived}"
        )

    subject_declared = {
        filename: declared
        for filename, (declared, _aliases) in DECLARING_SPECS.items()
        if filename != "milestone-map.md"
    }
    plan_declared = sum(
        1 for entry in entries if entry.spec.startswith("docs/plan/engineering-plan.md")
    )
    errors.extend(
        gate_table_arithmetic_errors(
            map_text,
            {
                "subject_specs": len(subject_declared),
                "subject_gates": sum(subject_declared.values()),
                "declarations": sum(declared for declared, _ in DECLARING_SPECS.values())
                + plan_declared,
                "entries": len(entries),
                "aliases": sum(aliases for _, aliases in DECLARING_SPECS.values()),
            },
        )
    )
    return errors
