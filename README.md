# Modular General-Purpose AI Agent

This repository is building the modular, general-purpose AI agent platform defined
by the canonical engineering plan at
[`docs/plan/engineering-plan.md`](docs/plan/engineering-plan.md).

Implementation has not started; the project is at **Milestone 0**
(pre-implementation). Coding agents should begin with
[`AGENTS.md`](AGENTS.md).

## Documentation

Markdown and YAML are the canonical, editable sources. HTML under `site/` and
`dist/` is **generated** and must never be edited by hand.

Canonical files:

- [`docs/plan/engineering-plan.md`](docs/plan/engineering-plan.md) — the normative plan.
- [`docs/plan/current-milestone.md`](docs/plan/current-milestone.md) — currently authorized work.
- [`docs/status/project-state.yaml`](docs/status/project-state.yaml) — machine-readable project state.
- [`docs/adr/`](docs/adr/) — architecture decision records.
- `archive/` — the original Word document (archival only).

### Prerequisites

- Python 3.12+
- [Pandoc](https://pandoc.org) as a system dependency (used to build the single
  standalone HTML document). Install it from your package manager, e.g.
  `apt-get install pandoc` or `brew install pandoc`.

### Install documentation dependencies

Using [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv sync
```

Or using pip in a virtual environment:

```bash
python -m venv .venv && . .venv/bin/activate
pip install mkdocs mkdocs-material pymdown-extensions PyYAML
```

### Build and serve

```bash
make docs         # build the MkDocs site (site/) and the single HTML (dist/…)
make docs-serve   # serve the site locally with live reload
make docs-check   # validate the docs and build them in strict mode
make check        # currently runs docs-check
```

With uv, prefix make commands with `uv run` (e.g. `uv run make docs`).

### Generated outputs

- `site/` — the navigable documentation site (open `site/index.html`).
- `dist/engineering-documentation.html` — a single self-contained HTML document.

These are generated; do not edit them, and do not commit them (they are
git-ignored).

## Status

Documentation and coding-agent bootstrap files only. No agent product code has
been written yet. See [`docs/changelog.md`](docs/changelog.md).
