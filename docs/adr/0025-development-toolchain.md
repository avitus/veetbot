# ADR-0025: The development toolchain and the meaning of `make check`

- Status: Accepted
- Date: 2026-07-25
- Related: Milestones 0, 1, 2, 3, Sections 2.2 (technology choices), 4
  (repository layout), 19 (observability), 20.4 (test categories), 21
  (Milestone 0), 22 (security baseline), 24 (definition of done), 25
  (local development), 26 (the first assignment), ADR-0001 (modular
  monolith), ADR-0022 (the gate registry), ADR-0024 (the composition
  root)
- Detailed design: `docs/plan/development-toolchain.md`
- CI-provider amendment: ADR-0035

## Context

Milestone 0 lists eleven deliverables. Two of them are specified
elsewhere in the corpus; the other nine are one line each. "Makefile"
is one word, and the eight targets it must provide appear in a fence
three subsections later with no statement of what any of them runs.
"Docker Compose with PostgreSQL" names no version, port, volume,
database, or credential. "CI pipeline" names no workflow file, and the
only CI shape anywhere in the corpus is four jobs in a specification
Milestone 0 does not cite. "Structured logging bootstrap" occurs once
in the entire documentation set.

This is not design work in the sense the ten mechanism specifications
are, and that is exactly why it was left undone. The cost of leaving
it undone is not a wrong guess but a different guess per session, and
the specific thing that drifts is the relationship between what a
developer runs locally and what blocks a merge.

Two statements in the corpus cannot both be satisfied by the obvious
reading. Section 21 gives Milestone 0 the acceptance criterion "CI
executes `make check`", which reads as CI being one command. The
evaluation harness specifies four CI jobs, ordered so the cheap ones
fail first, one of which requires PostgreSQL and one of which requires
a provider credential and is skipped without it. A `make check` that
contained all four would need a database and a credential, and would
fail on a fresh checkout, which contradicts Section 24's
definition-of-done item that `make check` succeeds.

A second collision sits underneath it. Section 21 requires both `make
test` and `make check` as separate targets. If `test` means the whole
suite and `check` contains `test`, then `check` inherits the database
requirement and the same contradiction returns by another route.

Three smaller ambiguities block Milestone 0 and Milestone 1 work
rather than merely leaving them underspecified. The six test
directories carry no selectors, so nothing says which of the four CI
jobs runs any of them.
Milestone 1 lists "Deterministic tests" while the harness's build
order places "cases 1 through 11" at the same milestone, with nothing
saying whether these are one deliverable or two. "Initial ADRs" has
three defensible readings: the six filenames in the tree, the eleven
deferred to their milestones by a note, or the twenty-five this corpus
has since accumulated.

## Decision

1.  **`make check` and CI are the same set of checks, partitioned
    rather than duplicated.** `make check` is `lint typecheck
    test-fast`, `test-fast` is `test-static` followed by
    `test-contract`, and those two are CI jobs 1 and 2 exactly. CI
    then runs the integration and live jobs, which need a database and
    a credential respectively. Both corpus statements are true
    afterward and neither was weakened.
2.  **CI never runs a command that is not a Makefile target.** The
    workflow file is a schedule and an environment, not a second
    definition of what the project checks. Six targets are added to
    Section 21's eight for this reason alone.
3.  **`make check` depends on `test-fast`, not on `test`.** `test`
    stays the broader local target it reads as; `check` stays runnable
    on a checkout with no Docker daemon, which is what Section 24
    requires of it.
4.  **Selection is by pytest marker, not by file list.** A gate
    registered under `static` is picked up by `make check` and by CI
    job 1 without either being edited. The contract selector is a
    negation, so an unmarked test runs rather than disappearing.
5.  **PostgreSQL 16 is pinned in a single-service compose file**, with
    a healthcheck that `make db-up` polls rather than sleeping
    against. `db-up` and `migrate` stay separate commands, matching
    Section 25 and ADR-0024's refusal to migrate from the composition
    root.
6.  **The compose credentials live in `.env.example` and the secret
    scanner scans that file.** They pass by an allowlist entry
    carrying a prose reason. A scanner that exempts the one file
    everyone copies is not a scanner.
7.  **One workflow file, four jobs, one Python version, no matrix.**
    The project pins `>=3.12` and runs one deployment; a matrix would
    test a configuration nothing runs. The live job runs on schedule
    and manual dispatch only, never on a pull request, because a fork
    cannot hold the credential and each run costs money.
8.  **`mkdocs build --strict` runs inside job 1** rather than in a job
    of its own, because it is a static check with no database and no
    fixtures.
9.  **Structured logging is structlog, configured in phase 1 of the
    composition root**, with two renderers keyed on deployment mode.
    Section 19's eight fields are bound as context variables at four
    named points, and `trace_id` is read from the active span rather
    than threaded through every call site.
10. **Redaction is a processor in the chain, not a convention.** Three
    match families, with content keys truncated to 200 characters
    rather than dropped, because a log line that says a tool returned
    something is useful and one carrying 40 KB of tool output is what
    Section 19 forbids. Section 20.4 already requires a secret
    redaction security test; this is the code it tests.
11. **Every test directory is assigned a marker.** Section 20.4 names
    five categories, the evaluation harness already named `resilience`
    as the sixth, and nothing said how a category is selected at the
    command line. The marker answers "can this run without Docker",
    which is the question the four-job split turns on. No category was
    added.
12. **"Deterministic tests" and "cases 1 through 11" are one
    deliverable.** Reading them as two is how a milestone acquires a
    second, informal test framework beside the specified one.
13. **Egress is blocked at Milestone 0 by an autouse pytest fixture**,
    exempting Unix sockets and loopback for the integration marker and
    lifted by the `live` marker. This is the Milestone 0 form of the
    harness's gate 7, and it is installed before the first adapter
    that could violate it.
14. **The ADR set is carried into the agent repository whole, not
    reauthored**, with numbering continuing from the highest number
    carried over. All three readings of "Initial ADRs" are satisfied
    at once and no decision acquires a second record.
15. **`docs-manifest.yaml` stays at four sources.** The single-file
    HTML is the plan-of-record bundle and the MkDocs site is the
    complete corpus.

## Consequences

- Milestone 0 becomes implementable from the corpus alone. Every one
  of its eleven deliverables now has either a specification in this
  document, a specification in `bootstrap-and-composition.md`, or an
  explicit statement of what it contains.
- A developer who runs `make check` and sees it pass knows exactly
  which CI jobs they have run and which they have not, because the
  Makefile names the integration target separately instead of folding
  it in silently.
- Fourteen Makefile targets exist where Section 21 named eight. The
  six additions are load-bearing: remove any one and a CI job has to
  inline a command, which breaks the property that makes the two
  definitions stay in agreement.
- The pull-request path costs roughly three minutes of CI when the
  database job is included and roughly one when it fails early, which
  is the ordering the harness asked for.
- Live tests never run on a pull request. This is a real coverage gap
  accepted deliberately: the alternative is either a credential
  reachable from forks or a job that fails for every external
  contributor.
- The combined HTML publication remains four documents while the
  corpus is thirty-seven. A reader handed that file cannot follow the
  cross-reference paragraphs the plan now carries, and is directed to
  the site instead.

## Alternatives considered

- **`make check` containing every check, including integration**:
  rejected because it contradicts Section 24's definition-of-done item
  directly. A target that a fresh checkout cannot run is not a
  definition of done; it is a definition of done on a machine with
  Docker.
- **CI defined independently of the Makefile**, the conventional
  arrangement: rejected because it is the specific drift this document
  exists to prevent. Two definitions of "the build is green" diverge
  on the first hurried fix to one of them, and the divergence is
  discovered by a merge that should not have been allowed.
- **A single CI job running `make check` and nothing else**: rejected
  because it satisfies Section 21 by ignoring the harness. The
  integration and live categories exist, and a CI that never runs them
  makes their gates decorative.
- **A Python version matrix**: rejected for now and recorded as an
  open question. One deployment, one version; a matrix tests a
  configuration nothing runs, at double the CI minutes.
- **Blocking egress with runner-level firewall rules**: rejected as
  disproportionate. Thirty lines in `conftest.py` produce a failure
  that names the host a test tried to reach, which is more useful than
  a connection timeout, and it works identically on a laptop.
- **Authoring fresh ADRs in the agent repository**: rejected. A second
  record of a decision already made is edited independently of the
  first, and the reader has no way to tell which one the code follows.
- **Widening `docs-manifest.yaml` to the full corpus now**: rejected
  until `scripts/build_docs.py` prefixes anchors per document.
  Thirty-seven documents share heading names like "Decisions", and the
  anchor generator resolves duplicates to the first occurrence, so the
  result would be a publication whose cross-references silently point
  at the wrong document.
