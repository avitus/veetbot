# ADR-0027: Gate milestones, the declaration form, and the milestone map

- Status: Accepted
- Date: 2026-07-25
- Related: Milestones 0 through 9, Sections 20 (evaluation), 21
  (implementation milestones), 26 (the first assignment), ADR-0004
  (checkpoints), ADR-0021 (tool execution pipeline), ADR-0022 (the gate
  registry), ADR-0023 (the run loop), ADR-0024 (composition root),
  ADR-0026 (builtin tools)
- Detailed design: `docs/plan/milestone-map.md`

## Context

ADR-0022 introduced a gate registry: one YAML file per spec area, each
entry naming an identifier, a kind, a milestone, a spec anchor, a
statement, and a check. Three rules govern it, and the first is that
every gate declared in a spec appears in the registry, verified by a
docs check that parses each spec's gate section, counts the declared
gates, and compares. That check is a Milestone 0 deliverable.

It cannot be written against the corpus as it stands, for three
reasons that only became visible once all twelve specifications
existed.

**The required field is mostly absent.** `milestone` is required on
every registry entry. Of the ten specs that declare gates, one tags
every gate individually, four state a milestone once for a whole
section, and five state none. Where a section-level statement exists it
is sometimes wrong for at least one gate in the section: the context
engine says Milestone 7 for all five of its gates while ADR-0024
already scheduled the first one's subject for Milestone 1, and the tool
system's ten gates sit under a heading with no milestone at all while
the same document's build order is tagged step by step.

**The section is unparseable.** The gate section is spelled three
ways — `## Hard gates`, `## Evaluation`, and `## Evaluation (gates the
milestone)`. Six specs declare gates as numbered items and four as
bullets. Two of the bullet specs interleave hard gates with tracked
metrics in one list and mark the difference in prose, mid-sentence, so
a count over that list is undefined until somebody reads every bullet
and decides.

**Three gates are declared twice.** The import-boundary walk, the
transaction-hygiene check, and checkpoint dispensability each appear in
two specs. A count check that does not know this double-counts, and an
implementer who does not know it writes the same assertion twice under
two names.

Two smaller reconciliations were blocked behind the same work. The tool
system's build order places persistence — schema additions, the
idempotency key, dedup on insert — at step 3, and says steps 1 through
5 are Milestone 1, while the engineering plan forbids PostgreSQL
persistence until the in-memory slice is complete. And the runtime loop
assigns `CancellationToken` and observation points 1 through 3 to
Milestone 1 while every writer it names for that token lives in a
supervisor task that Milestone 2 builds.

## Decision

1.  **A new document owns scheduling and owns no requirement about the
    agent.** `docs/plan/milestone-map.md` decides when each stated
    requirement must hold. If a gate's statement is wrong, the fix belongs
    in the spec that declares it and the map follows. The seven gates the
    map does declare, counted in decision 2, are the exception that proves
    the rule: they are checks over the scheduling record itself — that
    every declared gate has a milestone, that the registry reconciles —
    which no subject specification is in a position to make. The map owns
    no requirement about what the agent does.
2.  **All eighty-nine declared gates get a milestone**, derived from
    the build sequence or section milestone the declaring spec already
    states, producing eighty-six registry entries once the three
    double-declarations are resolved. The import-boundary walk the
    engineering plan declares makes eighty-seven, and the seven the map
    declares over the corpus make ninety-four.
3.  **A gate lands at the milestone that builds the last thing it
    observes.** This is stated as a rule so the next gate added has an
    answer before the argument starts, and it is what produced every
    assignment in the map.
4.  **One heading, one form, one suffix.** Every declaring spec spells
    the section `## Hard gates`, declares each gate as a numbered item
    with a bolded lead, and ends each item with `**M<n>.**`. The docs
    check reads the token, not the prose, and fails on an item without
    one.
5.  **Tracked metrics move to a sibling `## Tracked metrics`
    section.** Five specs gain one. Nine bullets carrying eleven
    metrics move out of two gate lists, leaving fourteen gates, and the
    separation is made on the specs' own words: *"A hard gate"*, *"The
    primary metric"*, *"not a metric to improve"*.
6.  **Registry `spec` anchors point at `#hard-gates`.** The two
    example entries in the harness are the only place the old anchor
    was written down.
7.  **Gate identifiers follow `gate.<area>.<slug>`** over ten areas,
    one of which — `structure` — exists so that a gate declared by two
    specs has a home belonging to neither.
8.  **A gate declared twice gets one owner and an explicit alias.**
    The non-owning spec keeps its sentence, because a reader of the
    tool system should learn that the import boundary is checked; what
    changes is that the sentence says it is the same gate. The map
    records the alias count per spec so the count check can subtract
    it. One of the three is owned by the engineering plan rather than
    by a detailed-design spec, because that is where it is declared.
9.  **`optional` is added to the registry for one gate.** The model
    gateway's live vendor smoke test may report skipped when its named
    precondition — a credential — is absent. Use is bounded to
    external credentials, and a second use is a design smell to be
    argued in review.
10. **The idempotency port is Milestone 1 and its index is Milestone
    2.** One port, two adapters, one contract suite, one declared gap:
    the in-memory adapter cannot tell the truth about two processes
    racing on a unique index, so it declares that gap rather than
    simulating it, and the concurrent-dedup gate moves to Milestone 2.
    This is ADR-0024's repository-versus-storage separation applied a
    second time.
11. **Milestone 1 cancellation is a lazily evaluated deadline plus a
    `SIGINT` handler.** Both need only `Clock` and the process; neither
    needs the queue, the lease, or the supervisor. `CancelReason` is
    split by dependency: `DEADLINE` is Milestone 1, `FENCED` is
    Milestone 2, and `REQUESTED` arrives twice — by poll at Milestone 2
    and by endpoint at Milestone 5.
12. **The census is generated and the written table is asserted
    against it.** A written distribution drifts; a derived one fails
    the build when it disagrees.

## Consequences

- The Milestone 0 docs check becomes writable. Its parse is four
  steps — find the heading, take the numbered items, take the trailing
  token, compare identifiers against the registry — and all four are
  true of the corpus after the mechanical edits this ADR authorizes.
- Ten specs are edited mechanically: five headings renamed, four gate
  lists converted from bullets to numbered items, nine metric bullets
  relocated, and seventy-five milestone tokens added. No sentence
  stating a requirement changes.
- Thirty-eight of ninety-four gates are green before Milestone 2, and
  eleven of those against a repository with no agent in it. That number
  is the argument for building the in-memory tier as real adapters
  rather than as test doubles, and it was not knowable before the
  gates were counted.
- Milestones 6 and 8 add no gates. Every invariant their work
  strengthens is registered against an earlier milestone. This is
  reported as a finding rather than fixed, and it is recorded as an
  open question, because inventing gates to fill a column is worse
  than naming the shape.
- The evaluation harness's own gate table is updated: its counts
  covered six specs when six existed, it gains rows for the engineering
  plan and the map, and its memory-formation count of seven becomes
  five once four of that spec's eight bullets are read as the metrics
  the spec calls them.
- Seven new hard gates are added, six of them at Milestone 0, and all
  seven are statements about documents and the registry rather than
  about the running system. They are the cheapest gates in the corpus
  and the ones that keep the other eighty-seven honest.

## Alternatives considered

- **Teaching the docs check all three headings and both forms**:
  rejected. It is possible and it makes the check the place where the
  corpus's inconsistency is preserved rather than removed, and a weak
  check whose parser has three branches is a check nobody trusts to
  fail correctly.
- **Putting the milestone only in the registry and not in the spec**:
  rejected, because the registry is then the only place a reader can
  learn when a stated invariant must hold, and the spec is where the
  invariant is stated. It also removes the redundancy the count check
  depends on: two records that must agree catch a dropped gate, and
  one record catches nothing.
- **A section-level milestone rather than a per-gate token**:
  rejected on evidence. Four specs already do it and two of the four
  have a gate that does not match the section — the context engine's
  determinism gate and, if the harness's section had one, its
  trajectory gate.
- **Deferring the whole reconciliation until the gates are
  implemented**: rejected for the reason Milestone 0 places structural
  checks against an almost-empty repository. A registry reconciled
  after ninety-four checks exist is reconciled against whatever was
  written, which is the situation in which the rule gets relaxed
  rather than obeyed.
- **Registering tool-system gate 5 at Milestone 8**, where its
  illustrating example lives: rejected. The rule that no external text
  reaches the model is the one that keeps prompt injection out, and
  deferring its assertion for seven milestones to match an example is
  the wrong half to follow.
- **Simulating concurrent dedup in the in-memory adapter**: rejected
  for the same reason ADR-0024 rejected an in-memory `RunQueue`. A
  simulation of a unique index passes its own test and teaches the
  wrong lesson about what the port guarantees.
- **No cancellation before Milestone 5**, which is Section 21's
  reading: rejected but recorded as an open question. It leaves three
  observation points unreachable for four milestones, and the code
  that must observe them is written at Milestone 1 either way.
- **Splitting the map into per-milestone documents**: rejected. The
  document's value is that one table shows every gate at once, which
  is what makes "Milestones 6 and 8 add no gates" visible at all.
