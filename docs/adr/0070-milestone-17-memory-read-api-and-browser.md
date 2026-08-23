# ADR-0070: Milestone 17 memory read API and native browser

- Status: Proposed
- Date: 2026-08-23
- Related: Sections 16, 20, and 21 of the engineering plan; ADR-0014,
  ADR-0018, ADR-0019, ADR-0045, ADR-0049, ADR-0050, ADR-0069
- Detailed design: `docs/plan/memory-read-api-and-browser.md`

## Context

The platform now forms beliefs automatically, ranks them, decays them, retires
them, and flags the conflicts among them, and the only way to see any of that
is a terminal. `agent memory list`, `get`, `formations`, `diagnose`, and
`trace` are a complete inspection surface for an operator standing at the host;
they are no surface at all for the person whose beliefs these are, who reaches
the platform through the native client. A memory subsystem that carries
forty-nine registered gates and, since Milestone 16, a measured quality number,
is still a subsystem its owner cannot browse.

That gap is deliberate rather than accidental. ADR-0045 decision 1 closed the
HTTP route set "until a later milestone explicitly designs a remote management
API," and ADR-0049 decision 8 forbade expanding the server to feed the Apple
client, its consequence naming the price of an exception: an authorized
contract and a security review of its own. Milestone 16 kept the closure
standing by listing "an HTTP memory surface" among the things it does not
build. None of those is a judgment that memory should stay unreadable. Each is
a refusal to open the surface by accident, on a client's schedule, without the
document that says what it exposes.

This ADR is that document, for the read half only. It authorizes Milestone 17:
two GET routes under `/v1/memories`, one exact scope, one default-off feature
flag, and a browsing surface in the native Apple client. Nothing it authorizes
writes.

The read half is also the half that pays for itself soonest. Every correction
a user makes on the CLI today begins with reading, and the reading is what the
CLI is worst at from a phone. Writes are the half where the interesting
questions are — what a remote edit does to provenance, to the rejection
tombstone, to the formation watermark — and none of them has to be answered to
let somebody look.

## Decisions

1. **Milestone 17 is authorized as a parallel workstream.** It may be developed
   alongside Milestones 13 through 15 and alongside Milestone 16 exactly as
   Milestone 11 was developed alongside Milestone 10, and its gates may become
   green independently. The verified gate ceiling still advances only in
   numerical order, so nothing in this milestone moves the ceiling past 15. The
   milestone shares no file with the delegation, surface, or operations
   tranches, and it reads the belief store Milestone 16 finished maturing.
2. **ADR-0045 decision 1 is superseded for read routes and for nothing else.**
   That decision closed the HTTP route set until a later milestone explicitly
   designed a remote management API; this is that milestone, restricted to
   inspection. The precedent for a scoped supersession of a closed route set is
   ADR-0050, which added the authoritative session index and session deletion
   after ADR-0049 had deferred them, without reopening the Milestone 5 route
   census. The same shape applies here: the Milestone 5 census stays historical,
   and every part of ADR-0045 decision 1 that governs writing survives intact.
3. **Read-only in round one.** The API browses, searches, filters, and shows one
   belief in detail. There is no edit, no retraction, no deletion, and no
   confirmation of a flagged conflict over HTTP. Corrections stay on the
   `agent memory` CLI, which already holds them and already routes them through
   the governed service rather than around it. The owner fixed this boundary
   when authorizing the milestone.
4. **This ADR is the authorized contract and security review ADR-0049 requires.**
   ADR-0049 decision 8 refused to expand the server for richer tool cards, and
   its consequences record that a future API expansion "would require its own
   authorized contract and security review." The detailed design supplies the
   contract — the routes, the scope, the exposure list, the ceiling rule, the
   error vocabulary — and the ten hard gates supply the review's teeth. An
   expansion that arrives without both is still forbidden.
5. **The native client is a full-parity viewing surface and declares the
   `restricted` ceiling.** The owner considered the conservative alternative, a
   lower default ceiling that would hide the platform's most sensitive beliefs
   from its only graphical surface, and explicitly rejected it: a browser that
   silently omits rows is worse than no browser, because the omission is
   invisible and the user concludes the belief was never formed. The device is
   the owner's own, the token is device-local and keychain-held, and the surface
   is private in exactly the sense ADR-0045 decision 11 already grants the
   Milestone 9 runtime surface. The owner accepts the consequence: a stolen,
   unlocked device with a live token can read restricted beliefs.
6. **The ceiling is a parameter, never an inference.** ADR-0045 decision 11's
   mechanism is unchanged and is the reason decision 5 is safe to make. Every
   request carries an explicit `ceiling`; the server filters strictly below it
   and never derives one from a scope, a principal, a role, or a user agent. A
   request that omits it is a validation error rather than a request granted the
   permissive default, because a default ceiling is the failure mode where a new
   caller is trusted by omission.
7. **Every route under `/v1/memories` is a GET.** The read-only decision is
   enforced structurally rather than by discipline: a gate walks the router and
   fails the build on any non-GET method, and on any route declaring a scope
   other than `memory.read`. A write route added here would otherwise be one
   plausible-looking commit away.
8. **The router is mounted only under `AGENT_MEMORY_API_ENABLED`, default off.**
   This follows the schedule and notification routers exactly: absent the flag
   the routes do not exist and do not appear in the OpenAPI document, while the
   scope remains available for configuration validation. A read surface over
   everything the platform believes should be something an operator turns on
   deliberately, and a client that meets a server without it degrades rather
   than fails.
9. **Milestone 16's exclusion list gives up its read half and keeps the rest.**
   The detailed design of Milestone 16 excludes "an HTTP memory surface" among
   the residue it does not take. This milestone takes the inspection half of
   that exclusion and no more; writes over HTTP, the semantic arm, the external
   provider, the persona surface, the entity graph, and belief merge remain
   excluded exactly as ADR-0069 left them, and each still enters on benchmark
   evidence.

## Consequences

- The platform gains a graphical answer to "what do you believe about me?" for
  the first time. Ten new gates in the existing `memory` area say the answer
  never exceeds the caller's ceiling, never crosses a principal, never skips or
  repeats a row under concurrent writes, and never carries a field the exposure
  list does not name.
- The `memory` area now spans four declaring specifications. It was already the
  only area a third joined; the argument is unchanged, because a browse of the
  belief store is a statement about the same beliefs the other three declare
  gates over.
- The closed scope vocabulary grows by one string, `memory.read`, when the code
  lands. The vocabulary is executable, so the documentation of the new scope
  ships in the same change as the enumeration that contains it rather than
  ahead of it.
- The native client acquires a second authoritative list surface beside the
  session sidebar, with the same keyset-pagination and version-skew handling,
  and native tests remain the client's verification under ADR-0049 decision 9.
  No Python gate observes Swift.
- Recall-trace viewing stays impossible until a tenant and principal predicate
  is added to the trace store's per-turn read. That is a bug this milestone
  names and does not fix, and naming it is what keeps a trace route from being
  added casually later.
- Three provenance fields — the formation run, the consolidation policy version,
  and the origin scopes — are withheld from the projection as a recommendation
  rather than as a settled decision, and the owner signs off on that trio during
  review of the implementing change.

## Alternatives considered

- **Reuse the recall arm, `MemoryStore.query(RecallQuery)`, for browsing:**
  rejected. Recall is a ranker: it caps candidates at the larger of eight times
  the requested item count and sixty-four, post-filters `LOCAL` portability
  against the current project scope, and defaults to live beliefs only, all of
  which are correct for assembling a context block and wrong for a page of a
  list. A browse built on it would silently truncate, and the truncation would
  look like an absent belief.
- **No feature flag, on the argument that a read surface is harmless:**
  rejected. It contradicts the schedule and notification precedent, it removes
  the operator's ability to run a deployment with no memory surface at all, and
  it takes away the clean signal a client uses to degrade gracefully against an
  older server.
- **A lower default ceiling for the native client:** rejected by the owner, as
  decision 5 records. The failure mode of a quietly filtered list is a user who
  believes the platform forgot something it did not forget.
- **Recall-trace viewing in round one:** deferred. `PostgresTraceStore.for_turn`
  filters on the turn identifier alone, with no tenant or principal predicate,
  and that must be fixed before any route can reach traces. Beyond the defect,
  a trace view would introduce the minimum-of-two-ceilings rule the retrieval
  design requires, which is a second ceiling mechanism in a milestone whose
  whole ceiling story is meant to be one parameter.
- **Consolidation and formation audit routes alongside the belief routes:**
  deferred to a later round. `formations` and `diagnose` expose operator-tier
  provenance whose exposure question is genuinely different from a belief's, and
  answering it here would double the surface under review.
- **Write routes in the same milestone:** rejected by the owner. Editing a
  belief remotely raises provenance, tombstone, and watermark questions that
  reading does not, and none of them blocks the surface people actually asked
  for.
