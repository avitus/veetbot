# ADR-0031: The ORM surface and the migration conventions

- Status: Accepted
- Date: 2026-07-27
- Related: Milestones 0 and 2, Sections 2.2 (technology choices), 5
  (dependency rules), 12.2 (transaction discipline), 15 (database
  schema), 23 (coding standards), 24 (definition of done), 25 (local
  development), ADR-0003 (event log and projections), ADR-0004 (the
  Postgres run queue), ADR-0024 (the composition root), ADR-0025 (`make
  check`)
- Detailed design: `docs/plan/event-log-and-persistence.md`

## Context

The readiness review found Milestone 2 ready with two partial bullets,
and both were about writing rather than architecture.

**The SQLAlchemy adapter had no ORM surface.** The schema is specified
table by table across Section 15 and the persistence spec's additions.
No document said whether mappings are declarative or imperative, where
the session factory's boundary sits relative to the unit of work, or
what a repository method body looks like against it. ADR-0024 fixed the
one property that could be checked without any of that — a factory is
constructed in phase 3 and a session never is, and no `AsyncSession`
exists at module scope — and left the rest to whoever wrote the first
repository.

**Alembic had no authoring conventions.** Section 15 says to create
migrations "for at least these tables". Section 23 rule 16 says to add
a migration for every schema change. Section 24 makes two of them
conditions of every milestone: migrations upgrade from a clean database,
and migrations upgrade from the previous revision. Section 25 lists
running them as a step and the toolchain spec gives it `make migrate`.
Between all of that there was a directory name, a Makefile target, and
no statement of what a migration looks like when someone writes one.

Two of those Section 24 criteria had no gate observing them, which is
the same class of defect the milestone map found when it counted gates:
an acceptance criterion that no check evaluates is a sentence, not a
criterion.

ADR-0024 decision 6 has a related residue. The composition root asserts
the schema revision and refuses to start on a mismatch. Where the
expected revision comes from was left open, and the two available
answers are not equivalent — one of them is a check and the other only
looks like one.

Neither bullet blocked the milestone. Both are the kind of decision an
implementer makes correctly on the first attempt and expensively on the
third, and both compound: the first six migrations set the conventions
for the rest whether or not anybody decided them, and the first
repository sets the mapping shape for the twenty after it.

## Decision

1.  **Both gaps are closed inside
    `docs/plan/event-log-and-persistence.md` rather than in a
    twentieth specification.** That document already owns the schema,
    the `gate.event.*` area, and `gate.structure.txn_hygiene`. A new
    spec would need either a fourteenth gate area or a gate area owned
    by two documents, and the readiness review calls both gaps tooling
    rather than architecture.
2.  **Row classes are separate declarative types, confined to
    `adapters/persistence/`.** This is forced by rules already written,
    not chosen here. Declarative mapping of a domain type puts
    SQLAlchemy inside `domain` and fails rule 1 on the import walk.
    Imperative mapping avoids the import and fails rule 7 instead: a
    mapped class carries instrumentation, so the domain object *is* the
    ORM object and every repository return value violates the rule at
    the moment the signature check has nothing left to reject.
    Pydantic's `BaseModel` and SQLAlchemy's declarative base also carry
    conflicting metaclasses, so the runtime and the rules agree.
3.  **Translation is two hand-written functions per table** — a
    `to_domain` and a `values` — in `mappers.py` beside the row
    classes. Not a generic mapper, and not `from_attributes`, which is
    the ORM mode rule 1's own note excludes.
4.  **A repository is constructed with a live session, never with a
    factory**, and repository methods do not commit. The caller that
    owns the unit of work opens the session and builds the repositories
    over it, which turns the transaction boundary into a construction
    site rather than an emergent property of which methods happened to
    commit.
5.  **A repository returns a domain type, a `domain` read model, a
    scalar, or `None`, under a concrete return annotation.** A
    projection query that names no domain aggregate gets a small
    Pydantic read model rather than a tuple or a `dict`, because the
    check enforcing rule 7 resolves signatures and cannot reject what a
    signature does not name.
6.  **The revision graph is linear.** No merge revisions, no `alembic
    merge`; a branch is resolved by rebasing `down_revision` before it
    lands. Two of the plan's own statements stop being well-defined
    otherwise: `upgrade head` names *a* head, and "upgrade from the
    previous revision" has no referent where a revision has two
    predecessors.
7.  **Structure and data are separate revisions**, with `downgrade()`
    written for structural revisions and raising `NotImplementedError`
    for data ones. Downgrade is written for the round-trip check, not
    as an operational promise; rolling a deployed schema backwards is a
    restore from backup.
8.  **Autogenerate drafts and a person edits**, kept honest by an
    empty-diff round trip rather than by review. The four things
    autogenerate misses here — server defaults and backfill, partial
    and conditional indexes, enum value additions, and data — are the
    four this schema is full of.
9.  **The expected schema revision is a module-level constant in the
    persistence adapter**, compared at startup against the single row
    of `alembic_version`. Reading the head out of the migrations
    directory at runtime is rejected: code and migrations ship
    together, so the computed head always agrees with the code, and the
    assertion is vacuous in exactly the case it exists to catch.
10. **Test schema is created by running the migrations**, never by
    `metadata.create_all`. A suite that creates its schema the fast way
    exercises no migration until deployment, and Section 24's two
    criteria become statements nothing evaluates.
11. **Five gates are added**, four of them closing criteria that
    already existed with nothing observing them:
    `gate.structure.migration_graph` at Milestone 0,
    `gate.event.migration_clean`, `gate.event.migration_stepwise`,
    `gate.event.revision_pinned`, and `gate.structure.orm_confined` at
    Milestone 2.
12. **The graph walk registers at Milestone 0.** Milestone 0 already
    requires that an empty Alembic migration runs, which is a graph
    with one node, and a walk that only begins once there are twelve
    revisions has already missed the branch it exists to prevent. This
    follows ADR-0024's precedent, where the transaction-hygiene *check*
    is a Milestone 0 deliverable and its *gate* is a Milestone 2
    criterion.

## Consequences

- Milestone 2's readiness gap closes. The three milestones from 0 to 2
  are ready with nothing outstanding, which is the first consecutive
  run of three in the map.
- The registry grows from one hundred and thirty-eight entries to one
  hundred and forty-three, and the kind split becomes seventy-seven
  case, seventeen property, seven corpus, and forty-two structural. The
  count of gates green before Milestone 2 becomes forty-one, thirteen
  of them against a repository with no agent in it.
- Two of Section 24's per-milestone criteria become decidable for the
  first time. They were written as conditions of *every* milestone and
  were, until now, evaluated by nothing.
- Roughly twenty tables acquire two translation functions each, all of
  them boring and all of them needing a test. That is the cost, and it
  is paid for by a schema and a wire contract that move independently,
  and by an upcaster that has a function to live in.
- The in-memory adapter tier survives. A port whose return types are
  the domain's has two implementations; a port whose return types are
  rows has one, and the contract suite ADR-0024 built to hold both
  honest would become a test of the only adapter that could satisfy it.
- ADR-0024 decision 6 gains its mechanism, and
  `bootstrap-and-composition.md` points at it rather than restating it.
- Two mis-numbered cross-references were found while grounding this and
  are corrected: the unit-of-work sentence is Section 2.2, not Section
  3, and the boundary tests are required by Section 5, not Section 3.

## Alternatives considered

- **A twentieth specification for the persistence adapter**: rejected.
  It would need a fourteenth gate area or a shared one, it would split
  ownership of the schema from ownership of the migrations that build
  it, and the material is two sections long.
- **Imperative mapping of the domain types**, which avoids the
  forbidden import and is the shape SQLAlchemy documents for exactly
  this purpose: rejected because it converts a loud failure into a
  silent one. The import walk passes and rule 7 becomes unenforceable
  in the same change.
- **A generic row-to-model mapper driven by field names**: rejected. It
  turns a column rename into a missing key at runtime rather than a
  type error at check time, and it is precisely the mechanism that
  makes a schema change leak into an API payload without anyone
  writing a line.
- **Repositories that own a session factory and open a session per
  method**: rejected. Every method becomes its own transaction, the
  append path's three-statements-one-transaction shape is unwriteable,
  and Section 12.2's persist-commit-call-commit sequence cannot be
  expressed by a caller at all.
- **Computing the expected revision from `ScriptDirectory` at
  runtime**: rejected as a check that cannot fail in production. It is
  worth naming rather than dismissing, because it is the version that
  requires no maintenance, and that is exactly why it is tempting.
- **Hand-numbered migration file prefixes** (`0007_`, `0008_`):
  rejected. Two revisions written the same week both become `0007` in
  two branches, and the file listing then shows an order the graph does
  not have. A name that cannot express order cannot express the wrong
  order.
- **Allowing merge revisions and resolving branches with `alembic
  merge`**: rejected. It is the cheaper local move and it makes the
  two plan statements above undefined, which is the wrong trade for a
  system whose deployment story is "run migrations, then start".
- **Deferring both bullets to the implementer, as the readiness review
  allows**: rejected on the review's own argument. Neither blocks the
  milestone, and both are decisions made correctly the first time and
  expensively the third.
