#!/usr/bin/env python3
"""Build the documentation.

Produces two outputs from the canonical Markdown/YAML sources:

  * ``site/``                              the navigable MkDocs site
  * ``dist/engineering-documentation.html``  a single self-contained HTML document

Canonical sources under ``docs/`` are never modified by this script.
Cross-platform (macOS/Linux); no shell-specific behaviour.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs-manifest.yaml"


def fail(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def require_tool(name: str, hint: str) -> str:
    path = shutil.which(name)
    if not path:
        fail(f"required dependency '{name}' was not found on PATH. {hint}")
    return path


def load_manifest() -> dict:
    if not MANIFEST.is_file():
        fail(f"manifest not found: {MANIFEST}")
    try:
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(f"manifest is not valid YAML: {exc}")
    if not isinstance(data, dict):
        fail("manifest must be a YAML mapping")
    for key in ("title", "output", "sources"):
        if key not in data:
            fail(f"manifest is missing required key: {key}")
    if not isinstance(data["sources"], list) or not data["sources"]:
        fail("manifest 'sources' must be a non-empty list")
    return data


def strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:]
    return text


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_site(mkdocs: str) -> None:
    print("Building MkDocs site (strict) -> site/ ...")
    subprocess.run([mkdocs, "build", "--strict", "--site-dir", "site"], cwd=ROOT, check=True)


def build_single_html(pandoc: str, manifest: dict) -> Path:
    sources: list[Path] = []
    missing: list[str] = []
    for rel in manifest["sources"]:
        if str(rel).startswith(("site/", "dist/")):
            fail(f"manifest source must not be a generated file: {rel}")
        p = ROOT / rel
        (sources if p.is_file() else missing).append(p if p.is_file() else rel)
    if missing:
        fail("manifest sources missing: " + ", ".join(map(str, missing)))

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = [f"*Generated {date} · commit {git_commit()}*", ""]
    for i, p in enumerate(sources):
        body = strip_front_matter(p.read_text(encoding="utf-8")).strip("\n")
        if i:
            parts.append("\n* * *\n")
        parts.append(body)
    combined = "\n".join(parts) + "\n"

    out = ROOT / manifest["output"]
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkstemp(suffix=".md")[1])
    tmp.write_text(combined, encoding="utf-8")
    try:
        cmd = [
            pandoc, str(tmp), "-f", "gfm", "-o", str(out),
            "--standalone",                 # self-contained (no external resources in sources)
            "--wrap=none",                  # do not hard-wrap the generated HTML source
            "--toc", "--toc-depth=3",
            "--highlight-style=pygments",
            "--metadata", f"title={manifest['title']}",
            # deliberately NOT --number-sections: the plan already carries visible numbering
        ]
        print(f"Building single HTML -> {out.relative_to(ROOT)} ...")
        subprocess.run(cmd, cwd=ROOT, check=True)
    finally:
        tmp.unlink()
    return out


def main() -> None:
    manifest = load_manifest()
    mkdocs = require_tool("mkdocs", "Install docs dependencies with `uv sync` or `pip install mkdocs mkdocs-material`.")
    pandoc = require_tool("pandoc", "Install system Pandoc, e.g. `apt-get install pandoc` or `brew install pandoc`.")
    build_site(mkdocs)
    out = build_single_html(pandoc, manifest)
    print(f"OK: built site/index.html and {out.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        fail(f"command failed (exit {exc.returncode}): {' '.join(map(str, exc.cmd))}")
