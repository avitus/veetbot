# ADR-0102: Chat thread folders and grouping proposals

- Status: Accepted — owner authorized Milestone 29 implementation on 2026-09-16
- Date: 2026-09-16
- Related: ADR-0045, ADR-0049, ADR-0050, ADR-0077, ADR-0079, ADR-0092, ADR-0100
- Design: [Chat thread folders](../plan/thread-folders.md)

## Context

Every chat conversation is a `sessions` row, and every client lists those rows
as one flat, activity-ordered history under the authoritative index ADR-0050
introduced. Nothing groups them, renames a group, or notices that several
recent conversations are about the same thing. The owner asked for
conversations organized into folders, for Veetbot to propose a folder once a
threshold of unfiled conversations can be grouped, and for folders the owner
can rename.

The corpus already holds the two patterns the request needs. The persona
surface (ADR-0079) defined how the assistant proposes and only the owner
resolves: closed eligibility, a bounded open set, a durable content-keyed
decline, and withdrawal when the source changes. Model-assisted memory
formation (ADR-0077) defined how a background pass may call a provider: a
deterministic result first, a strict response schema, a fixed budget, local
grounding, and an audited fallback. What the corpus lacks is any notion of
conversation organization, any embedding or clustering machinery — ADR-0045
recorded the no-embedding decision and roadmap B6 keeps the semantic arm
deferred — and any session write route beyond creation and deletion.

## Decisions

1. **Folders are server-owned session state, not metadata and not a cache.**
   A folder is a principal-scoped row; membership is a row keyed by the
   session with cascades from both sides. The session model, its repository,
   and the ADR-0050 deletion transaction are untouched, `SessionView` gains an
   additive `folder_id`, and every client reads membership from the index the
   way it reads the title. Session metadata stays opaque and untrusted.
2. **Proposals reuse the persona nomination lifecycle.** Only the maintenance
   pass proposes; at most three proposals are open per principal; a decline
   is durable and keyed on the grouping itself — kind, target, and sorted
   member set — with a near-duplicate rule; a proposal whose members or target
   change before review withdraws with a reason. Nothing files a conversation
   without the owner's explicit acceptance.
3. **Grouping is model-assisted under the extractor's discipline, with no
   embedding.** The pass computes a deterministic lexical grouping over titles
   and a bounded first-message snippet, then asks a structured-output model to
   refine it under a closed schema, fixed byte, token, and cost budgets, and a
   local grounding check; any failure yields the lexical result and an audit.
   No vector extension, no embedding provider, no learned policy: B6 is not
   amended.
4. **The grouping input is titles, snippets, and folder names — never
   metadata.** The call carries user-trust labels, executes no tools, and
   returns one document. Folder names and proposal text never render in a
   conversation prompt; the frozen prefix is byte-identical with and without
   folders.
5. **Chat conversations only.** Email, operational, scheduled, and delegated
   sessions — identified by the reserved metadata the platform already writes
   — can be neither filed nor proposed. Email-mode organization stays with
   ADR-0092's design.
6. **The existing session scopes gate the routes.** A folder confers no
   authority over anything the principal cannot already read or delete, so
   the nine routes require `session.read` and `session.write` rather than a
   new pair, and existing token grants keep working. The routes and the pass
   mount only under `AGENT_THREAD_FOLDERS_API_ENABLED`, default off.
7. **Deleting a folder unfiles; it never deletes.** The cascade returns the
   folder's conversations to the unfiled state; conversation deletion remains
   ADR-0050's operation alone.
8. **The native sidebar keeps mirroring the server and degrades to today.**
   The cache gains the server-assigned folder id, the server wins on every
   reconciliation, and a server without the flag yields exactly the current
   flat list with no error, verified on the ADR-0049 Swift lanes.

## Scope admission and consequences

The owner authorized Milestone 29 as an independent parallel workstream on
2026-09-16. Its phase 0 updates the engineering plan, project state, the
current milestone, the milestone map, readiness, AGENTS routing, the HTTP and
composition contracts, and the executable gate registry, which admits a new
`gate.folder.*` area with twelve registered gates. Registration is not passing
evidence.

The design admits one structural migration, one folder store port with both
adapters, one application service, nine routes, one maintenance sweep, one
checked-in tuning document of seven knobs, one environment flag, and the
native sidebar work. It admits no new dependency. Model-assisted grouping is
capped at one call per pass under a fixed cost ceiling and is subject to the
existing provider egress policy.

## Alternatives considered

- **Folders in the client's SwiftData store only:** invisible on the owner's
  other devices and lost with the cache, which ADR-0050 defined as
  non-authoritative.
- **A `folder` key in session metadata:** opaque, unindexed, bounded at 8 KiB,
  with no uniqueness or cascade, and metadata is the one thing that must never
  reach a prompt.
- **A nullable `folder_id` column on `sessions`:** works, but touches the
  session model, both session repositories, every session fixture, and the
  deletion transaction for a feature that is off by default.
- **Automatic filing at a similarity threshold:** repeats the mistake the
  persona surface refused; a wrong filing is silent and a proposal is not.
- **Embeddings with `pgvector`:** reverses ADR-0045 and B6's entry condition
  for a feature whose input is a few dozen short titles.
- **A new `folder.read`/`folder.write` scope pair:** exact scopes are the
  house style, but here they would force every deployment's token grant and
  the native credential to change while granting nothing new.
- **Nested folders:** deferred; one level satisfies the request and a
  hierarchy needs its own design.

## Acceptance status

The owner approved the implementation plan in chat on 2026-09-16. Implementation
may proceed through the design's build sequence under the repository's
red-green rule. Pull request creation, merge, and production activation retain
their explicit boundaries.
