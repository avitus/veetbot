---
title: Home
---

# Modular General-Purpose AI Agent — Documentation

This repository is building the modular, general-purpose AI agent platform
defined by the [engineering plan](plan/engineering-plan.md). This site is the
navigable documentation for that project.

## Authoritative sources

Markdown and YAML are canonical. HTML under `site/` and `dist/` is **generated**
and must never be edited by hand.

- [Engineering plan](plan/engineering-plan.md) — the canonical, normative plan.
- [Current milestone](plan/current-milestone.md) — the work currently authorized.
- [Project state](status/index.md) — machine-readable execution state.
- [Architecture decisions](adr/index.md) — approved ADRs.
- [Changelog](changelog.md) — notable documentation changes.

Archive provenance: the plan was converted from an archived Word document at
`archive/Modular_General_Purpose_AI_Agent_Engineering_Plan.docx`, retained only
as an archival record.

## Current status

Milestones 0 and 1 are complete. Milestone 1's in-memory vertical slice, all 28
of its hard gates, full local suite, required CLI demonstration, and hosted
CircleCI workflow passed. See the [current milestone](plan/current-milestone.md)
and [project state](status/index.md) pages for recorded evidence.

## For coding agents

Start with `AGENTS.md` in the repository root — it is the operating contract.
Then read, in order: the project state, the current-milestone page, and the
relevant sections of the engineering plan.

## For humans

Use the navigation and search here to read the plan. A single self-contained HTML
version of the full documentation is produced by `make docs` and written to
`dist/engineering-documentation.html`; it opens locally without any other file.

## How the documentation is organized

- `docs/plan/` — the canonical engineering plan and the current-milestone pointer.
- `docs/architecture.md` — the implemented module boundaries and their canonical sources.
- `docs/events.md` — the implementation status of the event surface.
- `docs/status/` — machine-readable project state.
- `docs/adr/` — architecture decision records.
- `archive/` — the original Word document (archival only).
- `site/`, `dist/` — generated outputs; do not edit.
