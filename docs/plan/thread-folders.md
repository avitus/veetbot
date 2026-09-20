---
title: Chat Thread Folders
status: design
canonical: true
---

# Chat thread folders and grouping proposals

This document specifies Milestone 29. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md) and it reuses rather than replaces
the session index, the persona nomination lifecycle, the memory extractor's
provider discipline, and the http-api-and-streaming conventions.
[ADR-0102](../adr/0102-chat-thread-folders.md) records the architectural
decisions and the owner's authorization.

A chat conversation is a `sessions` row shared by every surface, and every
client lists those rows as one flat, activity-ordered history
(http-api-and-streaming.md:752-756). Nothing groups them. The owner wants
conversations organized into folders, wants the assistant to notice when a
handful of unfiled conversations belong together and to say so, and wants the
folders to carry the names the owner gives them. Milestone 29 builds it: flat,
single-parent, principal-scoped **folders** as server-owned session state; a
**proposal** loop in which a maintenance pass nominates a grouping and only
the owner's explicit acceptance files anything; and the surfaces that show,
rename, move, and review — nine HTTP routes behind a default-off flag and a
native sidebar that degrades to today's flat list when the server has none.

Milestone 29 is authorized as a parallel workstream. Its gates may become
green independently, but the verified gate ceiling advances only in numerical
order.

## Scope

Milestone 29 delivers the folders, the proposals, the grouping computation,
and the surfaces that operate them.

- **Folders.** A named, principal-scoped container; a chat session belongs to
  at most one folder, and a folder holds any number of sessions. Create,
  rename, delete, and move are owner acts.
- **Proposals.** A maintenance pass proposes either a new folder over at least
  a configured number of unfiled chat sessions, or adding unfiled sessions to
  a folder that already exists. The owner accepts or declines; a decline is
  durable; a proposal whose members change before review withdraws itself.
- **Grouping.** A model-assisted grouper under a strict schema, a byte and
  cost budget, and a grounding check, with a deterministic lexical grouper as
  the fallback the pass can always fall back to.
- **Surfaces.** Nine routes under `/v1/folders` and `/v1/sessions/{id}/folder`,
  an additive `folder_id` on `SessionView`, and the native sidebar's folder
  sections, move menu, name sheet, and suggested-folder rows.

Seven things are out of scope, and each is named because a reader who does
not find it here should find the reason here.

1. **Nested or shared folders.** One level, one owner. Hierarchy and sharing
   each need a design of their own and nothing in the owner's request asks
   for either.
2. **Email-mode conversations.** Email discussion sessions are already
   organized by their thread and account (email-experience.md is their
   design); scheduled and delegated sessions are the scheduler's and the
   delegation service's. Folders file chat conversations only, and the
   predicate is the reserved metadata the platform already writes.
3. **Automatic filing.** No threshold, similarity score, or model confidence
   files a conversation without the owner's acceptance. The pass proposes;
   the owner disposes. This is the persona surface's rule
   (persona-surface.md:60-63) applied to a lighter object.
4. **Embeddings and a vector extension.** Roadmap item B6 keeps the semantic
   arm and `pgvector` deferred behind benchmark evidence, and ADR-0045
   recorded the no-embedding decision. Grouping here is a bounded structured
   model call over a few dozen titles, falling back to token overlap.
5. **Folder text in a conversation prompt.** Folder names and proposal
   rationale never render in the frozen prefix, the runtime-metadata row, or
   recall. They are organization, not instruction.
6. **Search over folders or conversations.** The sidebar groups; it does not
   search. A search surface is a separate request.
7. **Terminal-client work.** The Python client lists no history today and
   email-experience.md keeps it that way; folders are a native concern.

## Foundations this design reuses

**The session index is authoritative and principal-scoped.** ADR-0050 made
conversation history server state and every local history store a cache
(multi-device-and-surfaces.md:49-53). A folder is a fact about a session the
same way its title is: `SessionView.title` is server-owned and never a
client-only value (http-api-and-streaming.md:620-627). Membership therefore
lives beside the session on the server and every client reads it from the
index, which is what lets a folder created on the Mac appear on the iPhone.

**A session is not owned by a channel, and metadata is not a column.** The
surface seam records the surface in session metadata precisely because a
session column would be wrong (inbound-surfaces.md:347-350). Folder
membership is the opposite case — it is owner-organized state with a
uniqueness rule, a cascade, and a count — so it is a table, not a metadata
key. Metadata stays opaque, bounded, and untrusted.

**Session metadata never enters a model prompt.** The rule is a security rule
(http-api-and-streaming.md:662-670): metadata is client input and the shape
an injection takes. The grouping call reads titles, bounded message text, and
folder names — content the platform already treats as user-authored data
under a trust label — and reads no metadata key at all.

**The persona nomination lifecycle.** Milestone 22 defined the shape of an
assistant proposal the owner resolves: closed eligibility
(persona-surface.md:174-181), a bounded open set (persona-surface.md:183-185),
a decline that is durable and content-keyed, and withdrawal when the source
dies before review (persona-surface.md:201-211). Proposals here take that
lifecycle unchanged and add a second withdrawal cause, so a reason field
exists where the persona design deliberately omitted one.

**The extractor's provider discipline.** Model-assisted memory formation
computes the deterministic result first, calls the provider under a strict
response schema and a fixed budget, grounds every returned item against its
source, and records an audited deterministic fallback on any failure
(memory-formation-and-consolidation.md:749). The grouper is that design with
a different schema.

**The maintenance role.** Background work runs as sweeps in the maintenance
worker under an advisory lock (runtime-loop.md:1412), each on its own timer
when it is slow. The proposal pass is one more sweep.

## The domain model

Three records, all tenant- and principal-scoped as repository predicates in
both stores under one contract suite.

```python
class ThreadFolder(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    name: str                        # normalized display name, 1–64 characters
    created_at: AwareDatetime
    updated_at: AwareDatetime
```

**Names are normalized and unique per principal.** Normalization collapses
whitespace, applies NFC, and refuses an empty result, more than 64 characters
(the session title's cap, so the sidebar's two columns share one width),
control characters, and any value failing the secret-material or
injection-pattern scan; a refusal is `malformed_request`. Uniqueness is on
the case-folded name, so `Travel` and `travel` are one folder and a second
create is `conflict` with `details.reason = "folder_name_taken"`. A principal
holds at most 200 folders; the 201st is `conflict` with reason
`folder_limit`.

**Membership is a row keyed by the session.**

```sql
session_folder_memberships (
    session_id   uuid primary key references sessions(id)        on delete cascade,
    folder_id    uuid not null    references thread_folders(id)  on delete cascade,
    tenant_id    text not null,
    principal_id text not null,
    added_at     timestamptz not null
)
```

The primary key is the single-parent rule: a session is in at most one
folder, and a move is an upsert. The first cascade is how ADR-0050's session
deletion (http-api-and-streaming.md:758-764) leaves no membership behind
without a new step in the deletion transaction; the second is how deleting a
folder returns its conversations to the unfiled state in the same statement.
The denormalized tenant and principal let ownership be a predicate on this
table without a join. The `sessions` table, its domain object, its repository
port, and its list ordering are untouched; the list route fills
`SessionView.folder_id` from one batched membership lookup per page, the same
way it already fills `last_run_id`.

```python
class FolderProposal(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    kind: Literal["new_folder", "add_to_folder"]
    proposed_name: str | None        # new_folder only
    target_folder_id: UUID | None    # add_to_folder only; no foreign key
    member_session_ids: tuple[UUID, ...]   # sorted, unique, 1–12
    rationale: str | None            # at most 200 characters, hazard-scanned
    derivation: Literal["lexical", "model"]
    content_key: str                 # SHA-256 over kind, target, sorted members
    state: Literal["proposed", "accepted", "declined", "withdrawn"]
    withdrawal_reason: Literal["member_gone", "target_gone", "name_taken"] | None
    resulting_folder_id: UUID | None # set by acceptance
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None
```

A proposal is self-contained: it copies the member identifiers and the name
it proposes, so it outlives a folder's later deletion without dangling, which
is why `target_folder_id` carries no foreign key and withdrawal, not the
database, resolves a proposal whose target disappears. The model validates
its own consistency — a new-folder proposal carries a name and no target, an
add-to-folder proposal the reverse; a resolved proposal carries `resolved_at`
and an open one does not; a reason appears only on a withdrawn row and a
resulting folder only on an accepted one; the content key equals the hash of
its own fields. A partial unique index over `(tenant_id, principal_id,
content_key)` where `state = 'proposed'` makes the same grouping open at most
once, the same guard the persona nomination table uses.

## Manual operations

Every manual operation is one unit of work, principal-first, and records one
process event with a stable derivation key and a content-free payload.

- **Create** normalizes the name, checks the count, inserts, records
  `folder.created`.
- **Rename** normalizes, refuses a collision with a different folder's key
  (renaming `travel` to `Travel` is allowed), updates `updated_at`, records
  `folder.renamed`. Membership is untouched; a rename never moves anything.
- **Delete** removes the folder and, by cascade, its memberships; the
  conversations are unfiled, never deleted. Open add-to-folder proposals
  naming the folder withdraw in the same transaction with reason
  `target_gone`. Records `folder.deleted`.
- **Move** takes a session and a folder identifier or null. The session must
  be owned and must be a chat session — its metadata carries none of
  `email_thread_id`, `email_operational`, `schedule_id`, or `run_kind` — or
  the request is `conflict` with `details.reason = "session_not_chat"`.
  The folder, when given, must be owned. Moving to the folder a session is
  already in, or unfiling an unfiled session, is a 200 with no event. A real
  change upserts or deletes the membership row, withdraws every open proposal
  that names the session (reason `member_gone`; the pass will re-derive the
  remainder), and records `session.folder.changed`.

A missing, cross-tenant, or differently owned folder or session is a 404
indistinguishable from absence, the rule every principal-scoped route in
[http-api-and-streaming.md](http-api-and-streaming.md) applies.

## Proposals

**Eligibility is closed.** Only the maintenance pass proposes; no route, tool
call, or model output creates a proposal. A session is a candidate only when
all of the following hold: it is a chat session under the predicate above; it
is listed by the ordinary session index with operational sessions excluded;
it has a title; it is unfiled; and it is not already named by an open
proposal. A new-folder proposal requires at least `threshold` members
(shipped: 4) and at most `max_members` (shipped: 12); an add-to-folder
proposal requires at least one member and an existing, owned target folder.
A candidate set below the threshold is not a proposal, however confident the
grouper.

**The set is bounded.** At most `max_open` proposals (shipped: 3) may be
outstanding per principal. The pass computes nothing when the set is full,
and takes at most the free slots when it is not. Review must stay a minute's
work, or it will not happen and the sidebar fills with suggestions nobody
reads. Proposals are disjoint: no session is named by two open proposals.

**Decline is durable, and content-keyed.** A declined or accepted proposal's
content key is never proposed again — not by the next pass, not by a provider change,
not by a fallback run. Because a grouping re-derives with new identifiers
every pass, the key is the grouping itself: the kind, the target, and the
sorted member set. The pass also refuses a candidate whose member set is a
near duplicate of a declined proposal's of the same kind and target — Jaccard
similarity of the two member sets at or above 0.8 — so one new arrival
joining a declined group does not resurrect it, while a group that has
materially changed may be offered once more.

**Withdrawal** happens when a member is deleted, moved, or otherwise filed
before review, when an add-to-folder target is deleted, or when a new-folder
name has since been taken by an owner-created folder. A withdrawn proposal
frees its slot, its members become candidates again, and the withdrawn state
with its reason is the record. Withdrawal is done by the operation that
caused it — a move, a delete, an acceptance of another proposal — in that
operation's transaction, and again defensively at the start of every pass.

**Acceptance** is one unit of work. An accepted proposal replays
idempotently; a declined or withdrawn one is `conflict` with reason
`proposal_resolved`. The service re-reads every member and keeps those that
are still owned, chat, and unfiled; if none remain it withdraws the proposal
with reason `member_gone` and answers `conflict` with reason
`proposal_stale`. For a new folder it creates one from the proposed name or
the owner's optional override in the request body; a name collision is
`conflict` with reason `folder_name_taken` and the owner retries with a
different name. For an add-to-folder proposal it re-reads the target and, if
the folder is gone, withdraws with reason `target_gone`. It then files the
surviving members, withdraws any other open proposal that named one of them,
and resolves the proposal last, under a guarded transition only a still-open
row can take, so a rolled-back acceptance leaves folder, memberships, and
proposal untouched together. Records `folder.created` when a folder was made
and `folder.proposal.accepted` with the counts of members filed and skipped.

**Decline** is a single guarded transition to `declined` and one
`folder.proposal.declined` event. Declining a declined proposal is a 200;
declining an accepted or withdrawn one is `conflict`.

## The grouping computation

The grouper answers one question for one principal: given the unfiled chat
sessions and the folders that exist, which sessions belong together, under
what name, and which belong in a folder that already exists?

**Inputs.** For each candidate session: its identifier, its title, and a
snippet — the first 400 characters of the principal's first
`user.message.created` text in that session, read from the event log. For
each existing folder: its identifier, its name, and up to five member titles.
The threshold. Nothing else: no metadata key, no assistant or tool text, no
timestamp, no prior proposal, no memory. At most 200 candidates enter a pass;
the index is read in its own order and the first 200 eligible sessions are
the pass's universe.

**The lexical grouper is the floor.** It is deterministic, runs on every
pass, and is the whole answer whenever the model policy is non-routed or the
model call fails. Each session's terms are the lexical tokens of its title and
snippet — the same tokenizer memory retrieval uses — minus a stop list,
tokens shorter than three characters, and all-digit tokens. Two sessions are
linked when they share at least two terms and the Jaccard similarity of their
term sets is at or above `similarity_threshold` (shipped: 0.2). Connected
components of at least `threshold` sessions are new-folder candidates, named
from their two most document-frequent terms; a component larger than
`max_members` keeps its first `max_members` sessions in index order. Each
existing folder's term profile is the union of its members' terms, and an
unfiled session joins an add-to-folder candidate for that folder when it
shares at least two terms with the profile and their overlap coefficient is
at or above the same threshold. A new-folder candidate whose name key
collides with an existing folder becomes an add-to-folder candidate for it.
Output order is fixed — new folders first, larger first, then by lowest
member identifier — so the same input yields the same proposals.

**The model-assisted grouper refines it.** When the configured policy is
routed, the pass resolves a structured-output model and sends the inputs as
one JSON document: a platform-trust system instruction and a user-trust
message carrying the encoded inputs, with the principal on the label. The
response schema is closed — a list of at most sixteen groups, each with a
name, a member list, an optional target folder, and a short rationale, every
field required — and the provider is asked for exactly one final document
and no tool call. Budgets are fixed module constants rather than knobs: 32 KiB
of encoded input, 8,192 input tokens, 2,048 output tokens, a cost ceiling of
USD 0.10 per call, and a thirty-second deadline; an over-budget call is a
failure, not a partial success.

**Grounding is local and total.** Every returned group is checked against the
pass's own inputs before it becomes anything: every member identifier must
name a candidate session, no session may appear in two groups (the later
group is dropped), a target must name an existing folder or be null, a
new-folder group must reach the threshold, the name must pass the folder
name normalization and scans, a new-folder name whose key collides with an
existing folder is converted to an add-to-folder group for it, and a
rationale failing the scans is dropped while its group survives. The
surviving model groups come first; lexical groups disjoint from them are
appended. Any exception on the model path — resolution, transport, timeout,
budget, schema, parse — records an audit with the error class and the pass
proceeds with the lexical result alone.

**Injection defense on the way in.** A title, snippet, or folder name that
matches the injection-pattern scan is replaced by `[BLOCKED]` in the encoded
input, the substitution the memory extractor makes for existing beliefs, and
a snippet that matches the secret-material scan is dropped so that the title
alone represents its session. The system instruction says the inputs are data
to group and never instructions to follow.

**Audit.** Every pass records one `folder.proposal.pass` event carrying the
provider and model resolved, token usage, cost, whether the fallback was
used, the error class if any, and counts of candidates, proposals created,
and proposals withdrawn — never a title, snippet, name, or rationale.

## The maintenance pass

The pass runs in the maintenance worker as a sweep on its own timer, every
`interval_seconds` (shipped: 900), for the composed principal, and only when
`AGENT_THREAD_FOLDERS_API_ENABLED` is set and `proposals.enabled` is true.
It is idempotent under retry: the partial unique index refuses a duplicate
open grouping, a `conflict` on insert is skipped rather than raised, and
every proposal it creates is content-keyed.

In order: withdraw stale open proposals and count the rest; stop when the set
is full; read candidates and folders; read snippets; group; post-filter for
disjointness, size, declined keys, and near duplicates of declined proposals;
take at most the free slots in the grouper's order; insert each and record
`folder.proposal.created`; record the pass audit. The pass trusts nothing the
grouper returns: the post-filter re-applies every eligibility rule, so a
grouper defect can produce a bad suggestion but never an ineligible proposal.

The pass is not user-facing scheduling; it is internal maintenance, in the
same process and under the same lock as memory consolidation.

## The routes

Nine routes, mounted only when `AGENT_THREAD_FOLDERS_API_ENABLED` is set —
default off, like every optional surface. They require the existing
`session.read` and `session.write` scopes rather than a new pair, because a
folder is a view over sessions the principal already owns: nothing here
grants authority a session-writing client does not already hold, and a new
scope would force every deployment's token grant and the native client's
credential to change for no security gain. They use the same authentication
middleware, principal-first application signatures, request-id header, error
envelope, and cross-principal not-found rule as every route in
[http-api-and-streaming.md](http-api-and-streaming.md); that document carries
a stub subsection pointing here, and this document owns the schemas. Every
success response carries `Cache-Control: private, no-store`.

```text
POST   /v1/folders                                     session.write   201
GET    /v1/folders                                     session.read    200
GET    /v1/folders/proposals?state=proposed            session.read    200
POST   /v1/folders/proposals/{proposal_id}/accept      session.write   200
POST   /v1/folders/proposals/{proposal_id}/decline     session.write   200
GET    /v1/folders/{folder_id}                         session.read    200
PATCH  /v1/folders/{folder_id}                         session.write   200
DELETE /v1/folders/{folder_id}                         session.write   204
PUT    /v1/sessions/{session_id}/folder                session.write   200
```

- `POST /v1/folders` takes `{"name"}` and returns a `FolderView` — identifier,
  name, thread count, timestamps. `PATCH` takes the same body and renames.
  `DELETE` returns 204 once the folder and its memberships are gone.
- `GET /v1/folders` returns every folder in one page ordered by case-folded
  name; two hundred is the cap, so there is no cursor.
- `GET /v1/folders/proposals` filters by state and defaults to the open ones,
  newest first. Each `FolderProposalView` carries the kind, proposed name,
  target, member identifiers, rationale, derivation, state, reason, resulting
  folder, and timestamps — no tenant, no principal, no store internals.
- Accept takes an optional `{"name"}` override and returns the resolved
  proposal; decline returns it likewise. Both on a foreign or unknown id are
  404; both on a resolved proposal behave as the proposal section states.
- `PUT /v1/sessions/{session_id}/folder` takes `{"folder_id"}`, a UUID or an
  explicit null, and returns the session's `SessionView` with its new
  `folder_id`. The key is required; an absent key is `malformed_request`, so
  a client cannot unfile a conversation by forgetting a field.
- `SessionView` gains `folder_id`, null when unfiled, on every route that
  returns it, whether or not the flag is set; with the flag off it reports
  memberships that exist and no route can change them.

The proposal routes are registered before the folder-identifier routes so
`proposals` is never parsed as an identifier. Request bodies forbid unknown
fields.

## The native experience

The Apple client's sidebar keeps mirroring the server's authoritative index
under the contract in [apple-client.md](../apple-client.md): the cache gains
the server-assigned `folder_id` beside the title, the server's value wins on
every reconciliation, and the list of folders and open proposals is fetched
with the history and held in memory. The sidebar renders, in order, a
suggested-folders section with one row per open proposal — the proposed name
or target, the member titles resolved from the cached history, and accept and
decline controls — then one collapsible section per folder whose context
menu renames or deletes it, then the flat history of unfiled conversations,
then a new-folder control. Each conversation row gains a move menu listing the
folders, an unfile action, and a new-folder action. Creating and renaming use
a sheet with a text field and an inline error, so a duplicate name or a
refused value is shown where it was typed; deleting a folder uses the same
confirmation idiom as deleting a conversation, with a message saying that the
conversations return to history and nothing is deleted.

Against a server without the flag — a 404 or 405 on the folder list — the
client marks folders unavailable, renders exactly today's flat history, hides
every folder control, and shows no banner; the unavailability is contained
in the reconciliation and never surfaces as an error, so an older server
costs one extra request per poll and nothing else. Native behavior is
verified by the Swift testing lanes under ADR-0049, the same way the persona
editor's is.

## Events, telemetry, and privacy

- `folder.created`, `folder.renamed`, `folder.deleted`,
  `session.folder.changed`, `folder.proposal.created`,
  `folder.proposal.accepted`, `folder.proposal.declined`,
  `folder.proposal.withdrawn`, and `folder.proposal.pass` are process events
  with stable derivation keys. Payloads carry identifiers, kinds, reasons,
  counts, usage, and cost — never a folder name, title, snippet, or
  rationale.
- Logs on the pass and the routes carry error classes and opaque identifiers
  only, the rule email-experience.md applies to failures that may embed
  private text.
- Folder names and proposal text never render in a conversation prompt. The
  frozen prefix, the runtime-metadata row, and recall are byte-identical
  with and without folders; the prefix hash is the proof.
- Session metadata never enters the grouping call. The encoded input is
  built from titles, snippets, and folder names, and a test asserts that no
  reserved or client-supplied metadata key appears in the bytes sent.

## Migration, configuration, and operations

**Migration.** One structural revision adds the three tables, the unique
constraint, the partial unique index, and the state index, with a
`downgrade()` that drops them in reverse — written because the round-trip
gate requires it, not as an operational promise
(event-log-and-persistence.md:966-968). It descends linearly from the current
head (event-log-and-persistence.md:888-891) and the expected-revision pin
moves in the same change. A pre-migration session reads as unfiled.

**Configuration.** The tuning values are a checked-in document, because none
of them differs between two deployments of the same revision
(bootstrap-and-composition.md:337-339). `folders/profiles.yaml` ships:

```yaml
schema_version: 1
proposals:
  enabled: true
  threshold: 4              # members required for a new-folder proposal
  max_open: 3               # open proposals per principal
  interval_seconds: 900     # the pass's own timer in the maintenance worker
  model_policy: balanced    # a non-routed policy makes the grouper lexical-only
  similarity_threshold: 0.2 # Jaccard floor for a lexical link
  max_members: 12           # members per proposal
```

Its seven knobs join the executable inventory and the knob table in
bootstrap-and-composition.md. One environment key,
`AGENT_THREAD_FOLDERS_API_ENABLED`, gates the router and the pass together,
defaults off, and appears in `.env.example` in the same change
(bootstrap-and-composition.md:511-516).

**Disabling.** Unsetting the flag hides the routes and stops the pass; the
tables, folders, memberships, and proposals remain, `SessionView.folder_id`
keeps reporting them, and the native client falls back to the flat list.
Setting `proposals.enabled: false` keeps the routes and stops only the pass.
Rolling the schema back is a restore, as it is for every revision.

**Cost.** The pass makes at most one model call per interval per principal,
capped at USD 0.10, and none when the set is full or fewer than `threshold`
unfiled sessions exist and no folder does. The audit event is the ledger.

## Safety

- **Names are refused at write time, everywhere.** Create, rename, and
  acceptance run the secret-material and injection-pattern scans before
  persistence; a credential-shaped or instruction-shaped value is a refusal,
  not a warning. The grouper drops such a name before it becomes a proposal.
- **Nothing automatic files a conversation.** A proposal is a row the owner
  resolves; the only paths that write a membership are the owner's move and
  the owner's acceptance, and each is recorded as an event.
- **The grouping call is data in, document out.** User-trust labels on the
  input, a closed response schema, no tools, a fixed budget, and a local
  grounding check that discards anything the input does not support.
- **Ownership is a repository predicate.** Every read and write on all three
  tables carries tenant and principal in both stores under one contract, and
  a cross-principal read is an indistinguishable 404.

## Hard gates

1. **The schema is additive and both stores agree.** The migration creates
   the folder, membership, and proposal tables with their constraints and
   indexes, downgrades cleanly, reads a pre-migration session as unfiled,
   and the folder store passes one contract suite on both adapters with
   tenant and principal as predicates. Registered as
   `gate.folder.schema_additive`, case. **M29.**
2. **Folder invariants hold.** Names are unique per principal on the
   case-folded key with `conflict`, refused when empty, over 64 characters,
   or failing a hazard scan with `malformed_request`; rename preserves
   membership; delete unfiles every member and withdraws its open proposals
   in one transaction; and every write records an event. Registered as
   `gate.folder.crud_invariants`, case. **M29.**
3. **Move semantics are exact.** Filing and unfiling are idempotent; a
   foreign or unknown folder or session is an indistinguishable 404; an
   email, operational, scheduled, or delegated session is `conflict`
   with reason `session_not_chat`; and ADR-0050 session deletion leaves no
   membership behind. Registered as `gate.folder.move_semantics`, case.
   **M29.**
4. **Proposal eligibility is closed.** Only the maintenance pass proposes,
   only over unfiled chat sessions with titles, only at or above the
   configured threshold for a new folder, and never over a session already
   named by an open proposal; scheduled, delegated, and email sessions and
   sub-threshold groups never yield a proposal. Registered as
   `gate.folder.proposal_eligibility`, case. **M29.**
5. **The open set is bounded and disjoint.** At most `max_open` proposals
   are open per principal, no session is named by two of them, further
   candidates wait, and resolution or withdrawal frees the slot. Registered
   as `gate.folder.bounded_open_set`, case. **M29.**
6. **Decline is durable.** A declined content key, and any candidate whose
   member set is a near duplicate of a declined proposal's for the same kind
   and target, is never proposed again — across re-derivation, a provider
   change, or a fallback run. Registered as `gate.folder.decline_durable`,
   case. **M29.**
7. **Withdrawal follows the cause.** Deleting, moving, or filing a member,
   deleting the target folder, or an owner taking the proposed name
   withdraws the open proposal in the causing transaction with its reason,
   and the pass withdraws any it finds stale. Registered as
   `gate.folder.withdrawal`, case. **M29.**
8. **Grouping is grounded and falls back.** Model output is validated
   against the closed schema and grounded to the pass's inputs; an unknown
   member, a duplicate, a sub-threshold new folder, and an over-length or
   hazardous name are dropped; and a resolution, transport, budget, schema,
   or parse failure yields the deterministic lexical result, which is
   identical for identical input. Registered as
   `gate.folder.grounded_grouping`, property. **M29.**
9. **Acceptance is atomic and idempotent.** Accept creates or targets the
   folder, files the still-eligible members, withdraws competing proposals,
   and resolves the proposal last in one unit of work; racing accept and
   decline yield exactly one terminal state and the loser sees `conflict`;
   accepting an accepted proposal replays. Registered as
   `gate.folder.accept_atomic`, case. **M29.**
10. **Routes carry exact scopes and mount under the flag.** Every folder
    route requires exactly `session.read` or `session.write` and mounts only
    under `AGENT_THREAD_FOLDERS_API_ENABLED`; with the flag unset the route
    inventory is unchanged and the session routes differ only by the
    additive `folder_id`. Registered as `gate.folder.routes_exact_scope`,
    structural. **M29.**
11. **Organization text stays out of prompts and logs.** No session metadata
    key enters the grouping call, folder names and proposal text never
    render in a conversation prompt — the frozen prefix hash is identical
    with and without folders — and every event, log line, and audit carries
    identifiers and counts only. Registered as `gate.folder.content_free`,
    case. **M29.**
12. **The native sidebar degrades and reconciles.** Against a server without
    the flag the sidebar renders the flat index with no error and no folder
    control; against one with it, folder sections, the cached `folder_id`,
    and the proposal rows reconcile with the authoritative index, and the
    server's value wins. Registered as `gate.folder.native_degradation`,
    case. **M29.**

## Tracked metrics

- **Proposal flow** — created, accepted, declined, withdrawn counts by kind;
  a decline-heavy mix means the threshold or the similarity floor is too low.
- **Fallback share** — passes that used the lexical result alone; a rising
  share means the provider path is failing or over budget.
- **Pass cost** — tokens and cost per pass against the ceiling.
- **Unfiled share** — unfiled chat sessions as a share of the index; the
  number the feature exists to lower.

## Build sequence

1. Domain types, the folder store port, both adapters under one contract
   suite, the migration. Gate 1 turns green here.
2. The application service, the routes, the flag, `SessionView.folder_id`,
   and write-time refusals. Gates 2, 3, and 10.
3. The proposal lifecycle: propose, accept, decline, withdrawal from every
   cause. Gates 5, 6, 7, and 9.
4. The lexical grouper, the model-assisted grouper, the pass, its profile,
   and the maintenance sweep. Gates 4, 8, and 11.
5. The native sidebar, on the existing Swift lanes. Gate 12.

## Decisions

1. **A membership table, not a column on `sessions`.** The session model,
   its repository, its fixtures, and its deletion transaction stay untouched;
   the cascade does the deletion work; and with the flag off nothing about a
   session changes but one nullable view field.
2. **Session scopes, not a new pair.** No new authority is conferred, and a
   new scope would break every existing token grant for no security gain.
3. **The grouping is the key.** A proposal has no stable source object the
   way a nomination has a belief, so the durable decline hangs on the kind,
   target, and member set, with a near-duplicate rule so a single new
   arrival cannot launder a declined group.
4. **Whole-proposal withdrawal.** When a member moves, the proposal
   withdraws rather than shrinks; the next pass re-derives the remainder
   under a new key. Mutating an open proposal's members would change its
   content key under the owner's feet.
5. **Three outstanding proposals.** A folder proposal is a heavier read than
   a one-line belief; three keep review a glance.
6. **Lexical fallback over no fallback.** A pass that produces nothing when
   the provider is down is a feature that silently stops; token overlap over
   title and first message is weak but honest, and it is what the model
   refines rather than replaces.

## Open questions

1. Whether accepted proposals should teach the lexical grouper — a folder's
   term profile is already the union of its members, so acceptance improves
   add-to-folder proposals without a learned policy; whether that is enough
   is a metric question.
2. Whether a folder should collapse by default once it exceeds some size.
   The sidebar keeps sections expanded and remembers the owner's choice per
   device; a default can follow the unfiled-share and folder-size metrics.

## Implementation checkpoint: 2026-09-16

The build sequence landed in five commits on the day of authorization. The
domain types, the `FolderStore` port, both adapters under
`tests/contract/test_folder_store_contract.py`, and migration `a4f7c1e9d2b3`
bind gate 1 through the PostgreSQL parity test. The folder service and the nine
routes bind gates 2, 3, 7, 9, and 10 through
`tests/gates/test_folder_api_boundary_m29.py`. The lexical grouper, the
model-assisted grouper, and the pass bind gates 4, 5, 6, 8, and 11 through the
unit suites under `tests/unit/`, with `tests/gates/test_folder_m29.py`
exercising the composed pass through the maintenance worker. The native
sidebar binds gate 12 through `tests/native/test_folders_m29.py`, which runs
the Swift package cases and the macOS journeys on a full Xcode installation and
never passes on a skipped lane. The `folders/profiles.yaml` document ships the
seven knobs of the configuration section and joins the executable inventory.
Registration is not release evidence: exact-head hosted CI, review, and
production delivery remain the open items in project state.
