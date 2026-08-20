# ADR-0059: Reading lanes and the state-file split

- Status: Accepted (directed by the repository owner, 2026-08-19)
- Date: 2026-08-19
- Related: `AGENTS.md` (the operating contract), ADR-0027 and ADR-0028 (gate
  registry), `docs/plan/milestone-map.md` (the census)
- Detailed design: `AGENTS.md`, `scripts/architecture_checks.py`,
  `scripts/gate_registry.py`

## Context

The operating contract required every assignment, regardless of shape, to
complete the same nine-step reading order. A one-line repair guarded by an
existing regression test paid the same reading toll as a new subsystem, which
is the pressure that makes agents skip steps rather than follow them.

Separately, `docs/status/project-state.yaml` had grown to 1,128 lines, of
which roughly 92 percent was history: verification evidence for nine
completed milestones and a ten-pass corpus-audit narrative. Step 2 of the
reading order — nominally the cheapest — was in practice one of the most
expensive, and the duplicated audit narrative had drifted from the derived
gate census (it said 172 registry entries in three places while the registry
held 200). The milestone map's own gate-table intro paragraph carried the
same defect: spelled-out declaration arithmetic (187 declarations, 184
entries across fifteen specs) that no check reconciled, while the true
figures were 203, 200, and sixteen.

Two related recommendations from the same review were considered and revised
on evidence rather than adopted:

1. **Replacing line-number citations with heading anchors and deleting the
   repair tooling.** The citation ledger fingerprints every cited line and
   `make citations-fix` repoints moved citations mechanically; anchor
   citations would trade line-level drift detection for section-level
   existence checks. That is a weakening of verification, so the citation
   system stays as it is.
2. **Deriving every count at build time.** The milestone map's census table
   is already derived from `evals/gates/*.yaml` and reconciled by
   `make docs-check`; this decision extends that reconciliation to the
   gate-table intro paragraph rather than rebuilding the mechanism.

## Decision

1. **Three reading lanes.** The reading order in `AGENTS.md` becomes lane A,
   the default. Lane B (repair: an existing gate or regression test already
   observes the behavior) reads steps 1, 2, and 7 plus the owning design
   document. Lane C (changes that cannot alter observable behavior) reads
   steps 1 and 7. The floor is set by what the diff can invalidate, never by
   its size.
2. **The floor is derived, not declared.** `minimum_reading_lane` and
   `reading_lane_errors` in `scripts/architecture_checks.py` map changed
   paths to the minimum lane: policy, ports, memory, and execution code,
   specs, gates, contracts, evals, migrations, scripts, security files, CI,
   the Makefile, project state, and the contract itself are lane A; any
   other change under `src/`, `tests/`, `clients/`, or `deploy/` is at least
   lane B; everything else may be lane C. A declared lane below the derived
   minimum is an error. The lane is a floor, not a ceiling; the completion
   report declares the lane used, and the same declaration reaches CI as a
   `Reading-Lane: A|B|C` git trailer. `scripts/check_reading_lane.py` runs
   first in the static CI job: it takes the newest trailer in the pushed
   range (absence means lane A, which every diff permits) and fails the job
   when the declaration sits below the derived floor. The range's base is
   CircleCI's `pipeline.git.base_revision` when supplied, then `origin/dev`,
   `origin/main`, or the parent commit.
3. **Former steps 6 and 7 become triggers.** `milestone-map.md` is read when
   the change could move a gate; `readiness.md` is read before concluding
   anything is undesigned. Both remain lane A obligations through the
   trigger, not through position in a linear list.
4. **The state file carries status, not history.** Verification evidence for
   completed milestones moves to `docs/status/verification-history.yaml`; a
   milestone's evidence moves there when the state file records it complete,
   and the in-progress milestone's evidence stays in `project-state.yaml`.
   The corpus-audit narrative moves verbatim to
   `docs/status/corpus-audit-log.md`, where each pass's figures are
   historical records and are not updated. The `readiness` block in the
   state file shrinks to a current, short summary plus pointers.
5. **The gate-table arithmetic is reconciled at build time.**
   `gate_table_arithmetic_errors` in `scripts/gate_registry.py` parses the
   digits in the milestone map's gate-table intro (subject specs, subject
   gates, declarations, registry entries, aliases) and fails `make
   docs-check` when they disagree with the registry, or when the paragraph
   stops stating them. Live census-bearing prose elsewhere either matches
   the derived figures or is reworded to carry no count.

## Consequences

- A repair guarded by a red regression test starts from the failing test and
  the owning spec rather than from an 1,100-line state file, and cannot
  under-read: the check names the exact path that raises the floor.
- `docs/status/project-state.yaml` is roughly 355 lines and every line of it
  is current; step 2 costs what it should.
- The 172-versus-200 census self-contradiction is structurally impossible to
  reintroduce in the state file (the essay is gone) and mechanically checked
  in the milestone map (the arithmetic paragraph is reconciled).
- The completion report gains one field: the reading lane used and the
  diff-derived minimum.
- The citation ledger and `make citations-fix` are explicitly retained; this
  ADR records the revisit so the anchor proposal is not re-litigated without
  new evidence.

## Verification

- `tests/unit/test_reading_lanes.py` — lane floors, rejection below the
  minimum, unknown-lane rejection, per-path escalation reporting, trailer
  parsing, base resolution, and range checking against real git repositories.
- `tests/unit/test_toolchain.py` — the static CI job runs
  `scripts.check_reading_lane` with `READING_LANE_BASE` plumbed from the
  pipeline.
- `tests/gates/test_gate_registry.py` — the arithmetic check reports stale
  digits, accepts reconciled digits, requires the figures to be stated, and
  passes against the live corpus.
- `scripts/check_docs.py` enforces both through `registry_errors` and keeps
  `AGENTS.md` within its 200-line, 12 KB budget.
