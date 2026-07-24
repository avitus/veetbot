---
title: Changelog
---

# Changelog

## 2026-07-24 — Memory retrieval & ranking spec

- Added `docs/plan/memory-retrieval-and-ranking.md`, the read-path design for
  long-term memory: the three retrieval moments forced by the prompt-stability
  invariant (frozen session snapshot, in-turn recall, child-run recall), query
  formation from working state, the hard scope filter, multi-arm recall fused by
  reciprocal rank, the explicit ranking function, supersession collapse, the safety
  pass, byte-stable rendering, retrieval traces, and the usage-feedback loop back
  into formation. Recorded as ADR-0019.
- Wired the spec and ADR-0019 into the MkDocs navigation and the ADR index, and
  added read-path pointers from Milestone 9 and the formation spec.
- Three open questions are recorded for decision: the session snapshot token
  budget, whether project-scoped beliefs may surface cross-project, and whether
  retrieval traces become a user-facing surface.
- No product implementation was performed.

## 2026-07-24 — Memory formation & consolidation spec

- Added `docs/plan/memory-formation-and-consolidation.md`, the detailed write-path
  design for long-term memory (formation pipeline, conflict resolution with
  bi-temporal supersession, data model, governance, evaluation, and build
  sequence). Recorded as ADR-0018.
- Wired the spec into the MkDocs navigation and the ADR index, and added a pointer from Milestone 9 in the engineering plan.
- Resolved two design decisions in the spec and ADR-0018: memory formation is **fully autonomous from the start** (safety via deterministic eligibility gates, the untrusted-content write ban, and after-the-fact review), and the **builtin consolidation path is built to parity before any external provider**.
- Resolved the remaining formation questions: a **tiered memory model** (a continuous confidence lifecycle plus an explicit working/episodic/semantic/archival hierarchy), the **user model is a projection** over user-scoped beliefs, and **re-derivation is opt-in** per principal.
- No product implementation was performed.

## 2026-07-20 — Documentation system established

- Archived the source Word document to
  `archive/Modular_General_Purpose_AI_Agent_Engineering_Plan.docx` and recorded
  its SHA-256 checksum in `archive/README.md`. Preserved the prior Word revisions
  (v1.0 through v2.3) under `archive/versions/`; the canonical archived document is
  a copy of v2.3.
- Converted the complete engineering plan to canonical Markdown at
  `docs/plan/engineering-plan.md` (Pandoc `docx` → `gfm`, then deterministic
  cleanup: single level-one title, fenced code blocks with language hints,
  normalized tables, and removal of the static Word table of contents and
  title-page artifacts).
- Relocated three security controls — non-bypassable hardline rules, tiered
  credential scrubbing with fail-closed passthrough, and default-deny pairing for
  untrusted inbound surfaces — from the "Revision summary" list to their correct
  home in Section 22, "Security baseline". No requirement text was changed; only
  placement was corrected. The archived `.docx` retains the original placement.
- Created coding-agent instruction files: `AGENTS.md`, `CLAUDE.md`, and
  `.github/copilot-instructions.md`.
- Created machine-readable project state at `docs/status/project-state.yaml`
  (current milestone: 0) and a concise `docs/plan/current-milestone.md`.
- Wired the existing `docs/adr/` records (ADR-0007 through ADR-0017) into the
  documentation site.
- Created the documentation build system: the MkDocs site (`mkdocs.yml`), a
  single-file HTML build (`docs-manifest.yaml`, `scripts/build_docs.py`),
  documentation validation (`scripts/check_docs.py`), `Makefile` targets, and a
  CI workflow (`.github/workflows/docs.yml`).

No product implementation was performed. Milestone 0 has not been started.
