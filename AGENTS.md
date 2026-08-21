# AGENTS.md

Operating contract for coding agents working in this repository. This file is a
router, not a copy of the plan. Read it fully before making changes.

## Project mission

Build the modular, general-purpose AI agent platform defined by the canonical
engineering plan, `docs/plan/engineering-plan.md`. The plan is normative:
implement it milestone by milestone without changing its requirements.

## Required reading order

Read, in this order, before starting an assignment:

1. `AGENTS.md` (this file)
2. `docs/status/project-state.yaml` — current phase and authorized work
3. `docs/plan/current-milestone.md` — the work currently authorized
4. The relevant sections of `docs/plan/engineering-plan.md`
5. The detailed-design document that expands those sections — see the routing
   table below. The plan states the requirement; the spec states the mechanism.
6. Relevant ADRs in `docs/adr/` (index at `docs/adr/index.md`)
7. Existing code and tests related to the assignment

Additionally, read `docs/plan/milestone-map.md` whenever the change could move
a gate, and `docs/plan/readiness.md` before concluding anything is undesigned.

## Reading lanes

The order above is lane A, the default. Two narrower lanes exist. The floor is
set by what the diff can invalidate, never by its size; `reading_lane_errors`
in `scripts/architecture_checks.py` derives the minimum lane from changed paths.

- **Lane A — the full order.** New behavior, and any change touching policy,
  ports, memory, or execution code, specs under `docs/plan/`, gate or contract
  tests, evals, migrations, scripts, security files, CI, the Makefile, project
  state, or this contract.
- **Lane B — repair.** Fixing behavior an existing gate or regression test
  already observes: steps 1, 2, and 7, plus the design document that owns the
  subject. Any other change under `src/`, `tests/`, `clients/`, or `deploy/`
  is at least lane B.
- **Lane C — local.** Changes that cannot alter observable behavior (comments,
  formatting, docstrings, prose outside the plan): steps 1 and 7.

The lane is a floor, not a ceiling: escalate as the diff grows. Declare the
lane in the completion report and a `Reading-Lane: A|B|C` git trailer; CI
validates the floor, and no trailer means lane A.

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
| Knowledge documents, ingestion, passage retrieval | `knowledge-documents.md` |
| Public-web search and page extraction | `web-access.md` |
| Authenticated browser automation | `browser-automation.md` |
| Scheduled runs and recurrence | `scheduling.md` |
| Devices, surfaces, and the Section 29 seam | `multi-device-and-surfaces.md` |
| Which milestone each gate belongs to | `milestone-map.md` |
| What the corpus does and does not cover | `readiness.md` |

## Authority and conflicts

- The **engineering plan** holds the normative requirements and acceptance
  criteria; **project state** (`docs/status/project-state.yaml`) determines
  what work is authorized; **code and tests** describe actual behavior.
- Do **not** silently modify requirements to match an implementation; propose
  divergence explicitly. An architectural conflict with the plan requires a
  **proposed ADR** in `docs/adr/` (index at `docs/adr/index.md`).
- Security requirements and acceptance criteria must **not** be weakened without explicit human approval.

## Scope control

- Work only on the **active** milestone or an explicitly authorized one (see project state).
- Do not begin later milestones speculatively.
- Milestones 0 through 9 are complete; 10 and 11 await hosted review. Milestones
  12 through 15 — notifications and device identity, subagents and delegation,
  inbound surfaces and pairing, operational hardening — are authorized in that
  order (ADR-0061); model routing and the plan's roadmap items are not.
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

The specifications cite each other by line number through the citation ledger.
After editing a document that others cite into, run `make citations-fix` and
review the diff; a citation whose text is gone or now ambiguous is reported
rather than guessed, and needs a human. Write every line reference as
`file.md:LINE` or `file.md:LO-HI`; a prose form like "line 1408" is invisible
to the ledger, and `make docs-check` rejects it in a specification.

This section and `make check` must grow formatting, linting, type checking,
and tests as tooling lands. Do not claim a command works unless it exists here.

## Test-driven development

Behavior changes in this repository use a strict red-green-refactor loop.

- Derive behavior from governing documentation before reading or changing code.
- Add the smallest test for that documented behavior. Run the new or changed test first and record the expected failure.
  Environment, import, and fixture errors are not valid red tests.
- Implement the smallest coherent change that passes without weakening a
  requirement. Rerun the focused test, its partition, and every risk-relevant
  repository check; refactor only while tests remain green.

Do not weaken, delete, skip, or rewrite a failing test merely to make the suite
pass. If the documented requirement is wrong or conflicts with another
requirement, stop and propose the documentation or ADR change explicitly.
Every production bug fix begins with a regression test that reproduces the
failure. New adapters must begin with their shared contract suite, and new
public surfaces require boundary-level happy-path, validation, authorization,
failure, and retry coverage.

## Pull request review gate

GitHub mergeability does not make a PR ready under this contract. Use only the
CodeRabbit GitHub PR integration; never run local CodeRabbit CLI reviews (the
local service is continually rate-limited and is not authoritative). Loop:

1. Wait for CodeRabbit to finish reviewing the current head commit.
2. Address every CodeRabbit comment of any severity or placement — inline,
   summary, outside-diff, nitpick, suggestion, or trivial. Fix each valid
   finding; answer inapplicable ones with concrete evidence and resolve the
   conversation.
3. Push, wait for the review of the new head commit, and repeat until
   CodeRabbit reports no findings and every conversation is resolved.
4. Confirm all required CI checks pass on that same final head commit.

Never call a PR ready, mergeable, approved, or complete while CodeRabbit is
queued, running, has unresolved comments, or has not reviewed the latest push.

## Documentation update rules

- Update the **smallest** relevant documentation surface when behavior changes.
- Update `docs/status/project-state.yaml` when project status changes; move a
  completed milestone's evidence to `docs/status/verification-history.yaml`.
- Update the current architecture documentation when an implementation changes it.
- Add an **ADR** for material architectural decisions.
- Add **verification evidence** before marking any acceptance criterion complete.
- Never edit generated files under `site/` or `dist/` (they are regenerated).
- Never store private reasoning, secrets, raw credentials, sensitive tool output, or temporary debugging transcripts in project documentation.

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
10. Temporary notes, raw transcripts, secrets, credentials, and private reasoning never belong in durable documentation.

## Completion report

End every coding assignment with a report covering:

- Files changed
- Behavior implemented
- Reading lane declared (and the diff-derived minimum)
- Tests and checks run (with outcomes)
- Red test command and expected failure
- Documentation updated
- Acceptance criteria completed (with evidence)
- Known limitations
- Deviations from the engineering plan
- ADRs created or proposed

## Do not

- Do not start implementation work during a documentation-only assignment, and
  do not edit `archive/` or the generated `site/` and `dist/` outputs.
