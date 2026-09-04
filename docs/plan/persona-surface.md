---
title: Persona Surface
status: design
canonical: true
---

# Persona surface and curated belief promotion

This document specifies Milestone 22. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md) and it reuses rather than replaces
the context-engine, memory-formation, memory-retrieval, and
http-api-and-streaming designs.
[ADR-0079](../adr/0079-milestone-22-persona-surface.md) records the
architectural decisions, the owner's authorization, and the adjustment of
roadmap item B6's entry condition for this one item.

The platform holds exactly one piece of standing instruction text — the
agent's instructions, a hardcoded constant — and no surface anywhere lets the
owner say who they are and have the agent treat it as anything better than
retrievable data. The session-open snapshot
(memory-retrieval-and-ranking.md:77-82) is the "who you are talking to" layer,
but it is enveloped `MEMORY`-trust content, re-selected by a ranker at every
session open, and the model is explicitly told it is data to consider rather
than instruction to follow. The owner's core beliefs and standing truths
deserve the other side of that boundary, and the corpus has known it since
ADR-0014 named a persona surface and deferred it.

Milestone 22 builds it: a persisted, versioned, principal-scoped **persona
document**, rendered on every model call as its own Region A prefix row at
`TRUSTED_CONFIGURATION`; a **curated promotion** loop in which consolidation
nominates its strongest universally relevant beliefs and only the owner's
explicit affirmation moves one into the document; and the full edit stack —
CLI, six HTTP routes behind a default-off flag, and a native editor.

Milestone 22 is authorized as a parallel workstream. Its gates may become
green independently, but the verified gate ceiling advances only in numerical
order.

## Scope

Milestone 22 delivers the document, the row, the promotion loop, and the
surfaces that edit them.

- **The document.** An ordered list of persona entries, versioned as a whole,
  scoped to one tenant and principal, with per-entry provenance and
  sensitivity.
- **The row.** One new frozen-prefix row rendering the document at
  `TRUSTED_CONFIGURATION`, pinned per context plan, capped, and byte-stable
  within an epoch.
- **The promotion loop.** Governed nomination out of consolidation, bounded
  review, explicit affirmation or durable decline, bidirectional linkage, and
  snapshot de-duplication.
- **The surfaces.** `agent persona` on the CLI, six routes under
  `/v1/persona`, and a native editor with nomination review.

Four things are out of scope, and each is named because a reader who does not
find it here should find the reason here.

1. **Any automatic promotion.** No confidence threshold, corroboration count,
   or benchmark score writes persona text. The trust ladder survives because
   the only path across it is a human act; ADR-0079 decision 4 fixes this and
   hard gate 7 enforces it structurally.
2. **Belief writes over HTTP.** Affirming a nomination marks the source
   belief promoted through the governed lifecycle service, but belief edit,
   retraction, and deletion over HTTP remain excluded exactly as Milestone 17
   left them (memory-read-api-and-browser.md), and nothing under
   `/v1/memories` gains a verb.
3. **Per-agent or per-surface persona variants.** One document per principal.
   A second agent reading a different persona is a composition question for a
   milestone that has a second agent.
4. **Any change to `AgentSpec.instructions`** (engineering-plan.md:543). The
   persona layers over it at assembly time; the agent's spec, its
   content-addressed version, and every pin that references it are untouched.

## The persona document

One document per `(tenant_id, principal_id)`, versioned as a whole. A version
is immutable; an edit appends the next one. Each version carries an ordered
tuple of entries:

```python
class PersonaEntry(BaseModel):
    text: str                       # one belief or standing truth, <= 500 chars
    source: Literal["user_edit", "affirmation"]
    source_belief_id: UUID | None   # set exactly when source == "affirmation"
    sensitivity: Sensitivity        # ceiling-filtered at session open

class PersonaDocument(BaseModel):
    tenant_id: UUID
    principal_id: str
    version: int                    # dense, monotonic, starts at 1
    entries: tuple[PersonaEntry, ...]
    source: Literal["user_edit", "affirmation"]   # what created this version
    source_nomination_id: UUID | None
    created_at: AwareDatetime
```

The unwritten persona is version 0 with no entries — a real, readable state,
not an error — and it renders nothing. Provenance lives on the entry, not in
the prose: an affirmed entry knows its belief, an owner-typed entry knows it
was typed, and no flat-string representation could carry either.

Every entry carries `USER` authority in the sense the lifecycle design ranks
it (memory-evaluation-and-lifecycle.md:908): owner-typed text by authorship,
affirmed text because affirmation is a direct owner statement about the
belief. Writes are guarded by `expected_version`: a write naming any version
but the current head is a `conflict` and changes nothing.

Two stores implement one `PersonaStore` port — in memory and PostgreSQL —
under one shared contract suite, the same discipline every other paired
adapter in the repository follows. Persona tables carry `tenant_id` and
`principal_id` as repository predicates; a cross-principal read is an
indistinguishable 404.

## The prefix row

The row sits directly after the agent instructions in the assembly order —
row 4 of the table in [context-engine.md](context-engine.md) — because
instruction text belongs beside instruction text, ahead of the tool
advertisement and everything enveloped. It renders un-enveloped at
`TRUSTED_CONFIGURATION`, one line per entry, in document order.

- **Cap.** Thirty entries and 2,000 tokens, a `persona` context class that
  never yields. An over-cap persona fails session open with the class named,
  exactly as an over-cap agent instruction does. The prefix ceiling moves
  from 15,000 to 17,000 — the cap's own size, so the ceiling remains the
  exact sum of the Region A class caps.
- **Pinning.** The context plan records the persona text and version it was
  built from. The builder re-derives the prefix from the plan, so a
  mid-session edit cannot perturb an open epoch, and two builds of one plan
  read the same revision by construction.
- **Rotation.** At the next context plan after an edit, the planner compares
  the live document against the pinned version and rotates one epoch with
  reason `persona_changed` — the same lifecycle an agent-instruction edit
  already has. One edit, one rotation; a plan call with nothing changed
  returns the current plan unchanged.
- **The empty persona renders zero bytes.** Not an empty message — no item at
  all. Every session without a persona reproduces the pre-Milestone-22 prefix
  byte-for-byte, which is what lets this row land without a builder-version
  bump or a global epoch rotation, and what keeps the fifty-turn stability
  gate meaningful as the guard of the empty path.
- **Sensitivity is session-stable.** Entries above the session surface's
  sensitivity ceiling are filtered when the plan is created, never
  mid-session, so byte-stability holds within the epoch.

## Nomination and curated promotion

Formation's side of the hook is specified in
[memory-formation-and-consolidation.md](memory-formation-and-consolidation.md):
inside the same governed unit of work that commits or reinforces a qualifying
belief, the service records a nomination. This document owns the record and
its lifecycle.

```python
class PersonaNomination(BaseModel):
    id: UUID
    tenant_id: UUID
    principal_id: str
    belief_id: UUID
    statement: str                  # canonical copy, self-contained
    belief_type: BeliefType
    authority: MemoryAuthority
    confidence: float
    corroboration_count: int
    sensitivity: Sensitivity
    state: Literal["nominated", "affirmed", "declined", "withdrawn"]
    consolidation_run_id: UUID | None
    nominated_at: AwareDatetime
    resolved_at: AwareDatetime | None
    affirmed_version: int | None    # the document version affirmation created
```

**Eligibility is closed.** A belief qualifies only when all of the following
hold: status `active`; derivation direct, never a hypothesis; a durable type —
preference or user-model attribute — at `user` scope; confidence and
corroboration at or above configured floors (shipped: 0.75 and 3);
sensitivity at or below `internal`; outside every never-auto-store category;
and a statement that passes the injection and secret scans. Only the governed
consolidation service may nominate. Nomination copies the canonical statement,
so a nomination outlives its belief's later deletion without dangling.

**The set is bounded.** At most five nominations may be outstanding per
principal; further qualifiers wait for a slot. Review must stay a minute's
work, or it will not happen and the surface rots.

**Affirmation** appends a document version whose entries are the current ones
plus one `affirmation` entry carrying the statement and the belief id, marks
the source belief promoted with a link back to the entry, and resolves the
nomination with the version it created — one unit of work, the nomination
resolved last, under a guarded state transition only a still-open row can
take. Affirmation carries no client-supplied `expected_version`: the head it
extends is read inside the same transaction, the version primary key makes a
concurrent append a `conflict` rather than a lost write, and a rolled-back
affirmation leaves document, belief link, nomination, and audit event
untouched together — the retry simply affirms onto the new head. Affirming an
already-affirmed nomination is idempotent.
Promotion never supersedes or deletes the belief; removing the persona entry
later clears the promoted marker and nothing else.

**Decline is durable, and content-keyed.** A declined nomination's belief is
never nominated again — not by reinforcement, not by re-derivation, not by a
policy upgrade — the same standard the formation design applies to
corrections. Because re-derivation mints new belief identifiers, the id alone
cannot carry that promise: the nomination pass also refuses any candidate
whose case-folded statement matches a declined or affirmed nomination's, so
the owner's verdict follows the statement across identities. **Withdrawal**
happens when the source belief supersedes, retires, or is deleted before
review; a withdrawn nomination frees its slot, and the withdrawn state is
itself the record — withdrawal has exactly one cause, the source belief dying
before review, so no separate reason field exists until a second cause does.

**Staleness flows toward the owner, never past them.** When a promoted
belief's source is later superseded or corrected, the persona entry is
flagged stale for review on the same surfaces that show nominations.
`USER`-authority text is never auto-edited; the owner updates or removes the
entry, or keeps it — their text, their call.

## Snapshot and recall interaction

The snapshot side is specified in
[memory-retrieval-and-ranking.md](memory-retrieval-and-ranking.md): the
session-open snapshot query excludes, as a hard predicate, every belief whose
persona entry stands, because the persona row already carries it at higher
trust and a forty-item core (memory-retrieval-and-ranking.md:156) cannot
afford duplicates. Eligibility returns the moment the entry is removed.
In-turn recall and explicit search are unfiltered. Because snapshot
membership changes, the implementing change re-records the deterministic
benchmark baseline in the same commit, under ADR-0069 decision 2.

## The routes

Six routes, one exact scope pair, mounted only when
`AGENT_PERSONA_API_ENABLED` is set — default off, like every optional
surface. They use the same authentication middleware, principal-first
application signatures, request-id header, error envelope, and
cross-principal not-found rule as every route in
[http-api-and-streaming.md](http-api-and-streaming.md); that document carries
a stub subsection pointing here, and this document owns the schemas. Every
success response carries `Cache-Control: private, no-store`, the rule the
memory read routes already apply.

```text
GET    /v1/persona                                   persona.read
PUT    /v1/persona                                   persona.write
GET    /v1/persona/history                           persona.read
GET    /v1/persona/nominations                       persona.read
POST   /v1/persona/nominations/{nomination_id}/affirm    persona.write
POST   /v1/persona/nominations/{nomination_id}/decline   persona.write
```

- `GET /v1/persona` returns the head `PersonaView` — version, entries with
  provenance and staleness flags, timestamps. Version 0 with no entries is a
  200, not a 404.
- `PUT /v1/persona` takes `expected_version` and the full replacement entry
  list (owner-typed entries only may be added this way; existing `affirmation`
  entries may be kept, edited, or dropped, and dropping one clears its
  belief's marker). Stale `expected_version` is the closed vocabulary's
  `conflict`; refused content is `malformed_request`; both leave the document
  untouched.
- `GET /v1/persona/history` pages versions newest-first by keyset.
- The nomination routes list, affirm, and decline. Affirm replays
  idempotently and takes no `expected_version` — it extends whatever head it
  reads atomically in its own transaction, and loses cleanly as a `conflict`
  when a concurrent write gets there first; affirm or decline on a foreign
  or unknown id is a 404 indistinguishable from absence; decline of a
  resolved nomination is a `conflict`.

The views are explicit allow-lists in the `MemoryView` style: no tenant or
principal identifiers, no store internals.

## The CLI

`agent persona` joins the existing command groups, built on the same
composition and principal the memory commands use:

```text
agent persona show                    # head document, JSON
agent persona edit                    # replace entries, --expected-version
agent persona history [--limit N]
agent persona nominations [--state nominated|affirmed|declined|withdrawn]
agent persona affirm <nomination-id>
agent persona decline <nomination-id>
```

Exit codes follow the house convention; a version conflict is its own exit
code and message naming the current head, so a scripted edit fails loudly
rather than clobbering.

## The native editor

The Apple client gains a persona editor and a nomination review — the
document's entries editable in place with the version surfaced, a conflict
presented as reload-and-merge rather than data loss, and pending nominations
affirmed or declined with the statement and its evidence visible. Native
behavior is verified by the Swift testing lanes under ADR-0049, the same way
the memory browser's is.

## Safety

- **Secrets are refused at write time, everywhere.** The system prompt must
  not contain secrets (engineering-plan.md:4002), and the persona is system
  prompt. CLI, HTTP, and affirmation all run the secret-material scan before
  persistence; a credential-shaped value is a refusal, not a warning.
- **Injection is scanned at load.** Persona text passes the same
  injection scan memory does before rendering; a poisoned entry renders as a
  `[BLOCKED]` placeholder, never as instruction text. The scan at write time
  is defense in depth; the scan at load is the guarantee, because it also
  covers text written before a pattern existed.
- **Sensitivity is honored.** Entries carry sensitivity and the session
  surface's ceiling filters them at plan creation.
- **Nothing automatic crosses the trust boundary.** The row renders at
  `TRUSTED_CONFIGURATION` precisely because every byte of it was typed or
  explicitly affirmed by the owner; hard gates 7 and 8 keep that true.

## Hard gates

1. **The persona row is pinned and stable.** A scripted fifty-turn session
   with a mid-session persona edit yields exactly two distinct
   `prefix_sha256` values and one `context.epoch.rotated` event with reason
   `persona_changed`; within each epoch the hash is constant, and an empty
   persona renders no bytes, reproducing the pre-Milestone-22 prefix exactly.
   Registered as `gate.persona.prefix_row_stable`, case. **M22.**
2. **The persona class is capped.** The row holds at most thirty entries and
   2,000 tokens and never yields; an over-cap persona fails session open with
   the class named, and the 17,000-token prefix ceiling holds. Registered as
   `gate.persona.budget_capped`, case. **M22.**
3. **Trust and provenance are labeled.** Persona content renders in Region A
   at `TRUSTED_CONFIGURATION` with per-entry provenance, and
   nominated-but-unaffirmed text never renders in the row. Registered as
   `gate.persona.trust_labeled`, case. **M22.**
4. **The revision is pinned per plan.** A session reads one persona revision
   for the life of an epoch; a concurrent edit changes no open plan's prefix,
   and two builds of one plan are byte-identical. Registered as
   `gate.persona.revision_pinned`, case. **M22.**
5. **Secrets are refused at every write surface.** A credential-shaped value
   is refused before persistence at the CLI, the HTTP routes, and
   affirmation alike. Registered as `gate.persona.secret_refused`, case.
   **M22.**
6. **Persona text is injection-scanned at load.** A poisoned entry renders as
   a `[BLOCKED]` placeholder and never as instruction text. Registered as
   `gate.persona.injection_scanned`, case. **M22.**
7. **Only a human writes the persona.** No pipeline stage, tool call, or
   model output writes persona text; the only write paths are an owner edit
   and an explicit affirmation of a named nomination, and each write is
   recorded as an event. Registered as
   `gate.persona.promotion_requires_affirmation`, case. **M22.**
8. **Nomination eligibility is closed.** Nominations arise only from the
   governed consolidation service, only over active, direct, durable,
   confidence- and corroboration-qualified, sensitivity-bounded beliefs, and
   never from a never-auto-store category or a statement failing the hazard
   scans. Registered as `gate.persona.nomination_eligibility`, case. **M22.**
9. **Decline is durable.** A declined nomination's belief is never
   re-nominated, including after a full re-derivation. Registered as
   `gate.persona.decline_durable`, case. **M22.**
10. **Affirmation links and never consumes.** Affirmation marks the source
    belief promoted with bidirectional links, never supersedes or deletes it,
    and removing the persona entry clears the marker. Registered as
    `gate.persona.affirmed_linked`, case. **M22.**
11. **No double occupancy.** A promoted belief never occupies a session-open
    snapshot slot while its persona entry stands, and regains eligibility
    when the entry is removed; in-turn recall is unfiltered. Registered as
    `gate.persona.snapshot_dedup`, case. **M22.**
12. **Routes carry exact scopes and the memory API stays read-only.** Every
    persona route requires exactly `persona.read` or `persona.write` and
    mounts only under `AGENT_PERSONA_API_ENABLED`; a walk of both routers
    shows `/v1/memories` still serves only GETs under `memory.read`.
    Registered as `gate.persona.routes_exact_scope`, structural. **M22.**
13. **Writes are revision-guarded.** A persona write without a matching
    `expected_version` is rejected with the `conflict` code and no partial
    write, and a corrected retry succeeds. Registered as
    `gate.persona.revision_precondition`, case. **M22.**
14. **Persona data is principal-scoped.** Documents and nominations carry
    tenant and principal as repository predicates in both stores under one
    contract, and a cross-principal read is an indistinguishable 404.
    Registered as `gate.persona.principal_scoped`, case. **M22.**

## Tracked metrics

- **Persona size** — entries and tokens at plan time, against the cap.
- **Rotations with reason `persona_changed`** — should track edits
  one-to-one; more means churn, fewer means a pinning bug.
- **Nomination flow** — nominated, affirmed, declined, withdrawn counts; a
  decline-heavy mix means the eligibility bar is too low.
- **Stale entries outstanding** — flagged-stale persona entries awaiting
  review.

## Build sequence

1. Domain types, the `PersonaStore` port, both adapters under one contract
   suite, the migration. Gates 13 and 14 turn green here.
2. The context class, the row, pinning, rotation, and caps. Gates 1 through 4
   and 6.
3. The nomination hook, affirmation and decline, snapshot exclusion, and the
   baseline re-record. Gates 7 through 11.
4. The application service, routes, flag, CLI, and write-time refusals.
   Gates 5 and 12.
5. The native editor and nomination review, on the existing Swift lanes.

## Decisions

1. **A separate document, not `AgentSpec.instructions`.** ADR-0079
   decision 3; the version-forking consequence decided it.
2. **Entries, not a flat string.** Provenance, staleness, per-entry
   sensitivity, and marker-clearing on removal all need the entry as the
   unit; a flat string gives them nothing to attach to.
3. **Rotation on the next plan, not on the next session.** An
   agent-instruction edit already rotates at the next plan; inventing a
   second lifecycle for the adjacent row would be a difference with no
   justification.
4. **The ceiling rises by exactly the new cap.** The alternative — carving
   2,000 tokens out of existing classes — silently shrinks surfaces that
   argued for their sizes.
5. **Five outstanding nominations.** Small enough to review in passing,
   large enough that a productive week does not stall the pipeline.

## Open questions

1. Whether affirmation should offer the owner an edited restatement in the
   same gesture — affirm-with-rewording — or whether edit-after-affirm is
   enough. Deferred until the review surface exists and the friction is
   observable.
2. Whether a persona entry should ever expire. Nothing here decays; the
   design bets that thirty entries stay curated by hand. If stale-entry
   counts say otherwise, lifecycle can revisit.
