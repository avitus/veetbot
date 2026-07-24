# Copilot instructions

Follow the repository's `AGENTS.md` as the operating contract. In particular:

- Read `docs/status/project-state.yaml` and `docs/plan/current-milestone.md`
  before proposing changes; the canonical requirements are in
  `docs/plan/engineering-plan.md`.
- Work only on the currently authorized milestone; do not start later milestones
  or unrelated refactors.
- Do not weaken security requirements or acceptance criteria. Material
  architectural changes require an ADR in `docs/adr/`.
- Verify with `make docs-check` (and `make check`). Do not edit generated files
  under `site/` or `dist/`, or the archived source under `archive/`.
- Never put secrets, credentials, private reasoning, or debugging transcripts in
  documentation.
