#!/usr/bin/env python3
"""Verify and repair ``file.md:LINE`` citations across the corpus.

The specifications cite each other by line number. A line number is correct
only until the cited file is next edited, and an insertion anywhere above the
target silently moves it. This script makes that drift detectable, and in the
common case repairable, without asking anyone to remember.

The mechanism is a generated ledger, ``docs/status/citation-ledger.yaml``,
which records for every citation the excerpt the cited line held when the
citation was last verified.

  check  (default)  Recompute each cited line and compare it to the ledger.
                    A mismatch, a missing entry, a blank target, or a target
                    past end-of-file is an error. A line number written as
                    prose - "line 1408", "lines 659 to 661" - is also an
                    error, because the ledger cannot see that form and it
                    therefore drifts silently forever.
  --update          Re-resolve drifted citations by content: search the target
                    file for the recorded excerpt, and if it appears exactly
                    once, rewrite the line number in the citing file and
                    re-record it. Ambiguous or vanished excerpts are reported
                    and left alone, because those need a human to decide what
                    the citation now means.

Only live documents are scanned. Architecture decision records, the changelog,
and existing entries in the review-questions file are records at a point in
time and are deliberately not repaired.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "status" / "citation-ledger.yaml"

# Documents whose citations are maintained. Everything else is a historical
# record and is left as written.
LIVE_GLOBS = ("docs/plan/*.md",)

# Documents a citation may point into. Order matters: the first glob to
# supply a basename wins, which keeps `index.md` pointing at docs/index.md.
TARGET_GLOBS = (
    "docs/plan/*.md",
    "docs/*.md",
    "docs/status/*.md",
    "docs/adr/*.md",
)

CITE = re.compile(r"`?([a-z0-9][a-z0-9-]*\.md):(\d+)(?:-(\d+))?`?")

# The two ways of naming a line that CITE cannot see. Both were in the corpus
# and both were stale: the checker repairs `file.md:NNN` on every run, so the
# forms it does not match are the only ones that can rot. A reference may span
# one line break - "while line\n2202" - so these match across a single newline
# but never across a blank line.
_GAP = r"(?:[ \t]+|[ \t]*\n[ \t]*)"
BARE = re.compile(rf"\b[Ll]ines?{_GAP}\d+")
LOOSE = re.compile(rf"\b[a-z0-9][a-z0-9-]*\.md{_GAP}\d+")

EXCERPT_CHARS = 60

errors: list[str] = []
notes: list[str] = []


def excerpt_of(lines: list[str], lo: int, hi: int) -> str:
    """The comparable text of a cited span: first non-blank line, normalized."""
    for n in range(lo, hi + 1):
        if n - 1 < len(lines) and lines[n - 1].strip():
            return " ".join(lines[n - 1].split())[:EXCERPT_CHARS]
    return ""


def target_index() -> dict[str, Path]:
    idx: dict[str, Path] = {}
    for pattern in TARGET_GLOBS:
        for p in sorted(ROOT.glob(pattern)):
            idx.setdefault(p.name, p)
    return idx


def live_files() -> list[Path]:
    out: list[Path] = []
    for pattern in LIVE_GLOBS:
        out.extend(sorted(ROOT.glob(pattern)))
    return out


def collect() -> list[dict]:
    """Every citation in every live document, with its resolved target."""
    idx = target_index()
    found: list[dict] = []
    for src in live_files():
        lines = src.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            for m in CITE.finditer(line):
                name, lo = m.group(1), int(m.group(2))
                hi = int(m.group(3)) if m.group(3) else lo
                tgt = idx.get(name)
                if tgt is None:
                    errors.append(
                        f"{src.relative_to(ROOT)}:{i} cites {name}, "
                        f"which is not a document in this corpus"
                    )
                    continue
                found.append(
                    {
                        "source": str(src.relative_to(ROOT)),
                        "source_line": i,
                        "target": str(tgt.relative_to(ROOT)),
                        "target_line": lo,
                        "target_end": hi,
                        "_path": tgt,
                        "_span": m.span(),
                        "_raw": m.group(0),
                    }
                )
    return found


def span_of(c: dict) -> str:
    """A citation's target as written: ``LINE`` or ``LO-HI``."""
    if c["target_end"] != c["target_line"]:
        return f"{c['target_line']}-{c['target_end']}"
    return str(c["target_line"])


def key(c: dict) -> str:
    return f"{c['source']}#{c['target']}:{span_of(c)}"


def load_ledger() -> dict[str, str]:
    if not LEDGER.is_file():
        return {}
    data = yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}
    return {e["cite"]: e["excerpt"] for e in data.get("citations", [])}


def as_yaml_scalar(text: str) -> str:
    """``text`` as a one-line double-quoted YAML scalar, correctly escaped."""
    return yaml.safe_dump(
        text, default_style='"', allow_unicode=True, width=10**9
    ).rstrip("\n")


def write_ledger(entries: list[dict]) -> None:
    body = [
        "# Generated by scripts/check_citations.py --update. Do not edit by hand.",
        "#",
        "# One entry per line-number citation in a live document. `excerpt` is the",
        "# text the cited line held when the citation was last verified; it is what",
        "# lets --update find the line again after an edit moves it.",
        "",
        "citations:",
    ]
    # Serialize both scalars with the YAML writer rather than by hand. An
    # excerpt is arbitrary document text and regularly contains backslashes -
    # `\d`, `\s` - which are not valid escapes inside a double-quoted YAML
    # scalar, so quote-doubling alone produces a ledger that will not load.
    for e in entries:
        body.append(f"  - cite: {as_yaml_scalar(e['cite'])}")
        body.append(f"    excerpt: {as_yaml_scalar(e['excerpt'])}")
    LEDGER.write_text("\n".join(body) + "\n", encoding="utf-8")


def find_excerpt(path: Path, excerpt: str) -> list[int]:
    hits = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if " ".join(line.split())[:EXCERPT_CHARS] == excerpt:
            hits.append(n)
    return hits


def check_bare_references() -> None:
    """Reject line numbers written as prose rather than as citations.

    ``file.md:LINE`` is checked on every run and repaired by ``--update``.
    A line number written any other way is invisible to that machinery, so it
    is never checked, never repaired, and wrong from the first edit after it
    was written. Nothing here can be auto-repaired - the reference has to be
    re-resolved by content, which is a human decision - so this is an error
    and not a note.
    """
    for src in live_files():
        text = src.read_text(encoding="utf-8")
        rel = src.relative_to(ROOT)
        for rx in (BARE, LOOSE):
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                shown = " ".join(m.group(0).split())
                errors.append(
                    f'{rel}:{line} names a line as "{shown}", which the '
                    f"ledger cannot check. Write it as `file.md:LINE` "
                    f"(or `file.md:LO-HI`) and re-resolve it by content."
                )


def run_check(update: bool) -> None:
    ledger = load_ledger()
    cites = collect()
    fresh: dict[str, str] = {}
    # Repairs are scoped to one line of one file. The same citation string can
    # appear on several lines, so a file-wide replace would rewrite the wrong
    # one; each repair carries the character span the match occupied.
    repairs: dict[str, list[tuple[int, tuple[int, int], str]]] = {}

    for c in cites:
        lines = c["_path"].read_text(encoding="utf-8").splitlines()
        k = key(c)

        # `target_end` is `target_line` for a single-line citation, so this
        # covers both forms and catches a range whose end runs off the file
        # even though its first line is real.
        if c["target_end"] > len(lines):
            errors.append(
                f"{c['source']}:{c['source_line']} cites {c['target']}:"
                f"{span_of(c)}, past the end of a {len(lines)}-line file"
            )
            if k in ledger:
                fresh[k] = ledger[k]
            continue

        current = excerpt_of(lines, c["target_line"], c["target_end"])
        recorded = ledger.get(k)

        if not current and recorded is None:
            # Nothing to compare against and nothing at the target. This is
            # what an unrecorded citation into whitespace looks like.
            errors.append(
                f"{c['source']}:{c['source_line']} cites {c['target']}:"
                f"{c['target_line']}, which is blank"
            )
            continue

        if recorded is None:
            # A citation the ledger has never seen. On --update it is adopted
            # at its current target; on check it is an error, because adopting
            # silently would defeat the point.
            if update:
                fresh[k] = current
            else:
                errors.append(
                    f"{c['source']}:{c['source_line']} cites {c['target']}:"
                    f"{c['target_line']} and is not in the ledger; "
                    f"run scripts/check_citations.py --update"
                )
            continue

        if recorded == current:
            fresh[k] = current
            continue

        # Drift. Find where the cited text went.
        hits = find_excerpt(c["_path"], recorded)
        where = f"{c['source']}:{c['source_line']} -> {c['target']}:{c['target_line']}"
        if len(hits) != 1:
            errors.append(
                f"{where} no longer holds the text it cited, and that text "
                f"{'is gone from' if not hits else f'appears {len(hits)} times in'} "
                f"{c['target']}. Repoint it by hand."
            )
            fresh[k] = recorded
            continue

        new_line = hits[0]
        if not update:
            errors.append(
                f"{where} has drifted; the cited text is now at line {new_line}. "
                f"Run scripts/check_citations.py --update"
            )
            fresh[k] = recorded
            continue

        if c["target_end"] == c["target_line"]:
            span = str(new_line)
        else:
            span = f"{new_line}-{new_line + (c['target_end'] - c['target_line'])}"
        new_raw = re.sub(r":\d+(-\d+)?", f":{span}", c["_raw"], count=1)
        repairs.setdefault(c["source"], []).append((c["source_line"], c["_span"], new_raw))
        notes.append(f"repaired {where} -> line {span}")
        fresh[f"{c['source']}#{c['target']}:{span}"] = recorded

    for source, items in repairs.items():
        p = ROOT / source
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        # Apply right-to-left within each line so earlier spans stay valid.
        for source_line, (lo, hi), new_raw in sorted(items, reverse=True):
            line = lines[source_line - 1]
            lines[source_line - 1] = line[:lo] + new_raw + line[hi:]
        p.write_text("".join(lines), encoding="utf-8")

    if update:
        write_ledger([{"cite": k, "excerpt": v} for k, v in sorted(fresh.items())])
        notes.append(f"ledger rewritten with {len(fresh)} citations")
    else:
        notes.append(f"{len(cites)} citations checked")


def main() -> None:
    update = "--update" in sys.argv
    run_check(update)
    check_bare_references()
    for n in notes:
        print("note:", n)
    if errors:
        print(f"\ncitation check FAILED with {len(errors)} error(s):")
        for e in errors:
            print("  -", e)
        raise SystemExit(1)
    print("All citations resolve to the text they were recorded against.")


if __name__ == "__main__":
    main()
