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
    r"harness|api|sandbox|skill|knowledge)\.[a-z0-9]+(?:_[a-z0-9]+)*$"
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
    "memory-formation-and-consolidation.md": (5, 0),
    "memory-retrieval-and-ranking.md": (9, 0),
    "evaluation-harness.md": (11, 0),
    "http-api-and-streaming.md": (10, 0),
    "sandbox-isolation.md": (13, 0),
    "skills.md": (16, 0),
    "knowledge-documents.md": (12, 0),
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
                )
            except (TypeError, ValueError) as exc:
                errors.append(f"{where} has invalid field types: {exc}")
                continue
            if entry.id.split(".")[1] != path.stem:
                errors.append(f"{where} belongs in {entry.id.split('.')[1]}.yaml")
            entries.append(entry)
    if not entries:
        errors.append("evals/gates/*.yaml contains no gate entries")
    return entries, errors


def map_entries(root: Path) -> dict[str, tuple[str, int]]:
    """Read the identifier, kind, and milestone columns from the milestone map."""

    text = (root / "docs" / "plan" / "milestone-map.md").read_text(encoding="utf-8")
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


def registry_errors(root: Path, current_milestone: int = 0) -> list[str]:
    """Validate the registry and all six Milestone 0 map invariants."""

    entries, errors = load_registry(root)
    by_id = {entry.id: entry for entry in entries}
    duplicate_ids = sorted(
        gate_id for gate_id, count in Counter(e.id for e in entries).items() if count > 1
    )
    if duplicate_ids:
        errors.append(f"duplicate gate identifiers: {', '.join(duplicate_ids)}")

    expected = map_entries(root)
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

    map_text = (root / "docs" / "plan" / "milestone-map.md").read_text(encoding="utf-8")
    census_section = map_text.split("## The census", 1)[1].split("## ", 1)[0]
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
    return errors
