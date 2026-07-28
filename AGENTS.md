# AGENTS.md

Operating contract for coding agents working in this repository. This file is a
router, not a copy of the plan. Read it fully before making changes.

## Project mission

This repository is building the modular, general-purpose AI agent platform
defined by the canonical engineering plan:

- `docs/plan/engineering-plan.md`

The plan is normative. Your job is to implement it milestone by milestone,
without changing its requirements to suit your implementation.

## Required reading order

Read, in this order, before starting an assignment:

1. `AGENTS.md` (this file)
2. `docs/status/project-state.yaml` — current phase and authorized milestones
3. `docs/plan/current-milestone.md` — the work currently authorized
4. The relevant sections of `docs/plan/engineering-plan.md`
5. The detailed-design document that expands those sections — see the routing
   table below. The plan states the requirement; the spec states the mechanism.
6. `docs/plan/milestone-map.md` — which gates the active milestone must make
   true, and the milestone every gate in the corpus belongs to
7. `docs/plan/readiness.md` — what the corpus does and does not cover for that
   milestone, including what it names as designed nowhere
8. Relevant ADRs in `docs/adr/` (index at `docs/adr/index.md`)
9. Existing code and tests related to the assignment

## Where each subject is designed

| Subject | Document under `docs/plan/` |
| --- | --- |
| Startup, configuration, the composition root, the CLI | `bootstrap-and-composition.md` |
| Makefile targets, compose, CI, local development | `development-toolchain.md` |
| The run loop, cancellation, checkpoints, resume | `runtime-loop.md` |
| Tool registration, validation, execution, MCP | `tool-system.md` |
| The builtin tools and their classification | `builtin-tools.md` |
| Provider adapters, streaming, usage, cost | `model-gateway.md` |
| Policy decisions, hardline rules, approvals | `policy-and-approvals.md` |
| Events, projections, persistence, the queue | `event-log-and-persistence.md` |
| Context assembly, budgeting, working state | `context-engine.md` |
| Memory formation, tiers, consolidation | `memory-formation-and-consolidation.md` |
| Memory retrieval, ranking, the recall trace | `memory-retrieval-and-ranking.md` |
| Evaluation cases, gates, the harness | `evaluation-harness.md` |
| HTTP routes, error codes, scopes, the event stream | `http-api-and-streaming.md` |
| Isolated execution, egress, the artifact store | `sandbox-isolation.md` |
| Skill packages, the catalog, the authoring loop | `skills.md` |
| Which milestone each gate belongs to | `milestone-map.md` |
| What the corpus does and does not cover | `readiness.md` |

## Authority and conflicts

- The **engineering plan** contains the normative requirements and acceptance
  criteria.
- **Project state** (`docs/status/project-state.yaml`) determines what work is
  currently authorized.
- **Code and tests** describe actual current behavior.
- Do **not** silently modify requirements to match an implementation. If the
  implementation must diverge, propose the change explicitly.
- An architectural conflict with the plan requires a **proposed ADR** in
  `docs/adr/` (see the ADR overview at `docs/adr/index.md`).
- Security requirements and acceptance criteria must **not** be weakened without
  explicit human approval.

## Scope control

- Work only on the **active** milestone or an explicitly authorized one
  (see project state; currently Milestones 0 and 1).
- Do not begin later milestones speculatively.
- Milestones 0 through 9 are implementable from the documentation as it
  stands. Milestone 10 has a design for its skill-authoring half and no
  acceptance criteria of its own; do not begin it.
- Avoid unrelated refactors.
- Do not introduce a major dependency without documenting the decision (an ADR or
  a note in the relevant doc).
- Prefer the smallest coherent implementation that satisfies the active
  acceptance criteria.

## Verification

Run the checks that exist in the repository today:

```bash
make docs-check     # validates documentation and builds it in strict mode
make check          # runs docs-check; will grow as tooling is added
make citations-fix  # repoints line-number citations an edit has moved
```

The specifications cite each other by line number, and an insertion
anywhere above a cited line moves it. `make docs-check` fails when a
citation no longer holds the text it was recorded against. After editing
a document that others cite into, run `make citations-fix` and review the
diff; a citation whose text is gone or now ambiguous is reported rather
than guessed, and needs a human.

Write every line reference as `file.md:LINE` or `file.md:LO-HI`. A line
named in prose - "line 1408", "lines 659 to 661", "tool-system.md 1102" -
is invisible to the ledger, so it is never checked and never repaired.
`make docs-check` rejects that form in a specification.

As implementation tooling is added in later milestones, this section and
`make check` must also require formatting, linting, type checking, and tests.
Do not claim a command works unless it exists in this repository.

## Documentation update rules

- Update the **smallest** relevant documentation surface when behavior changes.
- Update `docs/status/project-state.yaml` when project status changes.
- Update the current architecture documentation when an implementation changes it.
- Add an **ADR** for material architectural decisions.
- Add **verification evidence** before marking any acceptance criterion complete.
- Never edit generated files under `site/` or `dist/` (they are regenerated).
- Never store private reasoning, secrets, raw credentials, sensitive tool output,
  or temporary debugging transcripts in project documentation.

## Documentation governance (canonical rules)

1. Markdown and YAML are canonical; HTML is generated.
2. Generated files must not be edited.
3. The engineering plan is normative.
4. Project state identifies currently authorized work.
5. Code and tests document actual behavior.
6. Requirements must not be weakened to match an implementation.
7. Architectural changes require an ADR.
8. Status changes require evidence.
9. Documentation changes ship alongside the corresponding code changes.
10. Temporary notes, raw transcripts, secrets, credentials, and private reasoning
    never belong in durable documentation.

## Completion report

End every coding assignment with a report covering:

- Files changed
- Behavior implemented
- Tests and checks run (with outcomes)
- Documentation updated
- Acceptance criteria completed (with evidence)
- Known limitations
- Deviations from the engineering plan
- ADRs created or proposed

## Do not

- Do not start Milestone 0 or later implementation as part of a documentation-only
  assignment.
- Do not edit `archive/` (the archived Word source) or generated `site/` and
  `dist/` outputs.
