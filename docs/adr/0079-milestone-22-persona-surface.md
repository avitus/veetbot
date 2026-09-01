# ADR-0079: Milestone 22 persona surface and curated belief promotion

- Status: Proposed
- Date: 2026-09-01
- Related: Sections 18, 21, and 22 of the engineering plan; ADR-0012,
  ADR-0014, ADR-0045, ADR-0049, ADR-0061, ADR-0069, ADR-0070, ADR-0077
- Detailed design: `docs/plan/persona-surface.md`

## Context

The agent's instruction text is a hardcoded constant with no editing surface
anywhere — no route, no configuration knob, no client field. The owner wants
the agent to carry something akin to a personal system prompt: standing text
that captures core beliefs and truths about the owner, present on every model
call, written primarily by the owner and absorbing, over time, the strongest
memory formations that are relevant across all queries.

The corpus has carried this feature since ADR-0014 decision 5 named an
optional persona/identity surface layered over `AgentSpec.instructions` and
deferred it. ADR-0069 and ADR-0070 kept it deferred; ADR-0077's Milestone 21
excluded persona editing again; roadmap item B6 holds it today. The readiness
review records the one unresolved design question in the memory area: the
persona surface has no statement of how a formed belief reaches the
instruction text, or whether it may. The owner authorized this milestone in
conversation on 2026-09-01, choosing curated promotion over automatic
promotion and a full edit stack — CLI, HTTP, and the native client. That
authorization is the input for this ADR; the state, map, readiness review,
and gates are updated before implementation begins.

The tension the design must resolve is a trust boundary. Agent instructions
render at `TRUSTED_CONFIGURATION` and are followed; memory renders enveloped
at `MEMORY` trust under the invariant that memory is data, never
instructions. Moving an inferred belief into instruction text is a trust
escalation the corpus deliberately left undecided.

## Proposed decisions

1. **The persona surface is parallel Milestone 22.** It does not move the
   sequential verified ceiling past Milestone 12 or reorder Milestones 13
   through 15. The persona surface leaves roadmap item B6; the semantic arm,
   external provider, learned memory policy, entity graph, session-history
   sources, and belief merge remain there, each still on benchmark evidence.
2. **The entry condition is adjusted on the record, not ignored.** B6 asks
   for Milestone 16 and 21 benchmark evidence per item. The Milestone 16
   benchmark measures retrieval arms; no benchmark can measure a surface
   whose primary content the owner types by hand. The owner therefore enters
   the user-authored half on authorization alone, and binds the promotion
   half to ADR-0069 decision 2: the one retrieval-visible behavior this
   milestone adds — snapshot exclusion of promoted beliefs — re-records the
   deterministic benchmark baseline in the change that implements it.
3. **The persona is a separate persisted, versioned, principal-scoped
   document rendered as its own Region A prefix row at
   `TRUSTED_CONFIGURATION` — not literal edits to `AgentSpec.instructions`.**
   Editing the agent's spec mints a content-addressed agent version, churns
   session pins, and gives per-entry provenance no home. ADR-0014's "layered
   over `AgentSpec.instructions`" is satisfied by assembly-time layering; its
   rejection of files as source of truth stands. The document is a list of
   entries, each carrying provenance — owner-typed or affirmed from a named
   belief — and sensitivity; an empty persona renders no bytes at all.
4. **A formed belief reaches instruction text only through explicit human
   affirmation.** The consolidation pipeline may nominate its strongest
   universally relevant beliefs; the owner affirms or declines each one.
   Affirmation is an act of human authorship that copies the canonical
   statement across the trust boundary under the owner's eyes, exactly as
   typing it would be. Memory itself stays data: nominated-but-unaffirmed
   text never renders in the persona row, and no pipeline stage, tool call,
   or model output writes persona text. This closes the readiness review's
   open question without weakening the memory-trust invariant.
5. **Every persona entry carries `USER` authority.** Owner-typed text is
   `USER` by authorship; promoted text is `USER` because affirmation is a
   direct owner statement about the belief.
6. **Nomination is governed and closed.** Only the governed consolidation
   service nominates, over active, direct, durable, confidence-qualified,
   corroborated, sensitivity-bounded beliefs; never hypotheses, never a
   never-auto-store category, never text that fails the injection or secret
   scans. A declined nomination is durable — the belief is never nominated
   again, including after re-derivation. The outstanding set is bounded.
7. **Affirmation links, never consumes.** The source belief remains in the
   store, marked promoted and linked to its persona entry; promotion never
   supersedes or deletes it. Removing the entry clears the marker. Later
   supersession of the source flags the entry stale for owner review and
   never auto-edits `USER`-authority text.
8. **A promoted belief leaves the session-open snapshot.** While its persona
   entry stands it never occupies a snapshot slot — the persona already
   carries it at higher trust and the forty-item snapshot is scarce — and it
   regains eligibility when the entry is removed. In-turn recall and
   explicit search are unchanged; hiding the belief from search would be
   dishonest.
9. **The prefix row sits directly after agent instructions,** capped at
   2,000 tokens and thirty entries, never yielding; an over-cap persona
   fails session open naming the class. The prefix ceiling rises from
   15,000 to 17,000 so it remains exactly the sum of the Region A class
   caps, the same move skills made. A session pins one persona revision at
   open; a mid-session edit persists immediately and takes effect at the
   next context plan as one epoch rotation with reason `persona_changed`,
   the same behavior an agent-instruction edit has today.
10. **The HTTP surface is distinct from the read-only memory API.** Six
    routes under `/v1/persona` carry exactly `persona.read` or
    `persona.write` and mount only under `AGENT_PERSONA_API_ENABLED`,
    default off. Nothing under `/v1/memories` is added or made non-GET;
    Milestone 17's no-write gate stays intact structurally, and a Milestone
    22 gate walks both routers to prove it. Milestone 17's exclusion of
    memory writes over HTTP is narrowed on the record: belief edit,
    retraction, and deletion over HTTP remain excluded; the persona document
    and nomination resolutions are a different resource, and affirmation's
    one memory-store side effect — the promoted marker and link — flows
    through the governed lifecycle service, never through the memory API.
    Writes carry `expected_version`; a stale revision is the existing
    `conflict` error. No new error codes.
11. **Content safety is enforced at every write surface.** CLI, HTTP, and
    affirmation refuse credential-shaped material before persistence — the
    system prompt must not contain secrets — and persona text is
    injection-scanned at load with `[BLOCKED]` replacement, extending
    ADR-0014 decision 3 to the new always-injected surface. Entries carry
    sensitivity and are filtered by the session surface's ceiling at session
    open, so byte-stability holds within an epoch.
12. **The surfaces are the CLI, the routes, and a native editor.**
    `agent persona show`, `edit`, `history`, `nominations`, `affirm`, and
    `decline` on the CLI; the six routes; an Apple-client editor and
    nomination review verified by native tests under ADR-0049 decision 9.
13. **Fourteen gates own the workstream in a new `persona` area,** all
    declared by `docs/plan/persona-surface.md` and registered before the
    first production-code change.

## Consequences

- The owner finally has a place to state who they are and what they hold
  true, and the agent reads it as instruction text on every call rather than
  as discountable memory data.
- The strongest formations can reach that text, but only through the owner's
  explicit act, so the memory-trust invariant survives contact with the
  feature that most tempts weakening it.
- The frozen prefix gains a row and the ceiling moves to 17,000; the
  fifty-turn stability gate keeps its meaning because an empty persona
  renders zero bytes and existing sessions reproduce today's prefix exactly.
- A new write surface, scope pair, feature flag, table pair, migration, CLI
  group, and client editor must all be built and gated; the persona API is
  default-off like every other optional surface.
- The snapshot exclusion touches retrieval, so the deterministic benchmark
  baseline is re-recorded in the implementing change under ADR-0069
  decision 2.

## Alternatives considered

- **A non-milestone plan section (the ADR-0075 precedent):** rejected. That
  precedent justified itself on adding no route, no scope, no flag, no store,
  and no gates; this feature adds all five, so it takes a milestone number
  like B11's email half and B5's calendar half did.
- **Editing `AgentSpec.instructions` directly:** rejected. Every edit forks a
  content-addressed agent version and rotates pins for what is conceptually
  per-principal identity text, and a flat string gives provenance no home.
- **Files as the source of truth (`SOUL.md` on disk):** re-rejected;
  ADR-0014 already decided the store is the database.
- **Automatic promotion at a confidence threshold:** rejected. It moves
  `MEMORY`-trust content into trusted configuration with no human in the
  loop, which is the exact weakening the invariant exists to prevent, and
  weakening a security requirement requires the explicit approval the owner
  declined to give.
- **A pinned band inside the memory snapshot instead of a trusted row:**
  rejected as the whole answer. It preserves trust labels but the content
  stays enveloped data the model may discount, which fails the feature's
  purpose; the snapshot's durable reserve already provides that weaker
  layer.
- **Waiting for per-item benchmark evidence:** rejected by the owner and
  recorded as decision 2's adjustment; the user-authored half is not a
  retrieval arm and cannot produce such evidence even in principle.
