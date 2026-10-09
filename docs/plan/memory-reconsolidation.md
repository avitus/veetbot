---
title: Memory Reconsolidation (Dreaming)
status: design
canonical: true
---

# Memory reconsolidation (dreaming)

The owner authorized this work on 2026-10-02: periodically revisit the memory
bank, merge related beliefs, and synthesize useful connections. It is parallel
**Milestone 32**, extracting that bounded portion of roadmap B6. The
[engineering plan](engineering-plan.md#milestone-32-memory-reconsolidation-dreaming)
records the requirements and [ADR-0169](../adr/0169-milestone-32-memory-reconsolidation.md)
records the architecture. On 2026-10-03 the owner instructed engineering to
begin Phase 1. This specification now defines the contracts and twenty-four
registered gates. Runtime implementation and production activation remain
separate: a registered pending gate is not a passing gate. Phase 1 includes
benchmark data, deterministic scoring and registry tooling, not a running
reconsolidator.

## Outcome and boundaries

Memory should become easier to retrieve and more useful across conversations
without losing the detail or source evidence that made it trustworthy. A
nightly-style maintenance pass should be able to produce these three outcomes:

| Outcome | Example | Treatment |
| --- | --- | --- |
| Equivalent beliefs merged | Two records both say the owner prefers Celsius | One canonical recall item; original records and source identities remain traceable |
| Related beliefs summarized | Several project decisions describe a preference for local execution | A compact summary linked to every underlying claim; specific decisions remain independently retrievable |
| A connection inferred | Recent, independently supported constraints suggest a recurring scheduling preference | A visibly tentative hypothesis, with cited supporting evidence and expiry |

Similarity alone never establishes equivalence. Differing dates, people,
quantities, negations, scope, attribution or uncertainty must survive. An
abstraction such as a general preference is a hypothesis unless the sources
actually state it. A contradiction becomes an inspectable conflict, not an
automatic choice of whichever statement compresses better.

“Entire memory bank” means eventual coverage of all eligible live beliefs for
one tenant and principal, including old records and different sessions. It
does not mean every pair is compared or the whole store fits into one prompt.
Cross-project candidates must satisfy the existing portability and sensitivity
rules. People identity merge/split remains owned by Milestone 28; dreaming
cannot silently merge people or change its thresholds.

This milestone does not add embeddings, a graph database, an external memory
provider, learned policies, arbitrary multi-hop reasoning, external enrichment,
new source types, automatic persona edits, or broad historical re-extraction.
The existing opt-in re-derivation command stays opt-in. Raw evidence may be read
only to verify selected, already admitted memories under current policy.

## Existing foundation

- The maintenance worker already handles idle-session formation, expiry and
  daily confidence decay. Reconsolidation becomes a separate bounded task in
  that role; ordinary replies and those existing jobs retain their cadence.
- Milestone 16 supplies the deterministic benchmark, provenance, correction,
  forgetting and opt-in re-derivation contracts. Milestone 21 separates evidence
  time from usage time and supplies hypothesis evaluation. It remains in
  progress: use verified artifacts, not an assumed milestone completion.
- The original memory foundation supplies related-belief lookup, paging and
  supersession. M32 adds the many-to-one merge/dependency contract in both
  in-memory and PostgreSQL adapters, as described below.
- Milestones 17 and 22 and ADR-0117 provide memory inspection, review/deletion,
  and human-only persona promotion. Milestone 28 adds People dependencies and
  erasure obligations that every new derived object must honor.

## Maintenance pipeline

1. **Claim a bounded job.** A daily due time creates durable, principal-scoped
   work. A lease, heartbeat, retry budget and saved cursor make it restartable
   across workers and deployments. Yield between slices, and back off under
   interactive load without blocking existing formation, expiry or erasure.
2. **Inventory and select.** Keep two durable cursors: changed beliefs first,
   and a fair rotating pass over the full eligible store. Reserve capacity
   for the latter so frequent new writes cannot starve old memories. Pin a
   scan generation/high watermark and deterministic keyset order; concurrent
   inserts enter the next generation and changed/deleted inputs are rechecked.
3. **Group across sessions.** Use existing subjects, admitted entity IDs,
   belief types and lexical overlap to fetch bounded related groups from the
   full store, not just the current page. Include deterministic coverage of
   old and cross-session examples in evaluation. No quadratic all-pairs scan.
4. **Propose operations.** A constrained provider proposes `merge_equivalent`,
   `summarize_related`, `infer_connection`, `flag_conflict` or `no_change` over
   supplied IDs. It cannot execute tools, browse, choose new source IDs, write
   persona text or mutate the store. Local code owns schema and trust checks.
5. **Validate and commit.** Verify each output against its original evidence,
   distinct source identities, exact input revisions, current rejections and
   erasure fences. Provider validation is supporting evidence, never the sole
   authorization for a merge. Recheck in a short transaction; commit an
   operation, dependency edges, its derived projection and content-free audit
   atomically. One stale group retries without discarding valid siblings.
6. **Observe and resume.** Advance the cursor only after every selected group
   has a durable outcome. Report inspected coverage, selected/deferred groups,
   abstentions, committed operations, failures, tokens, cost and time. A
   complete inventory scan must not be reported as complete model analysis.

Initial versioned bounds for `reconsolidation@1`:

| Control | Bound |
| --- | --- |
| Cadence | Daily due time, continued in short resumable slices |
| Inventory slice | At most 128 anchor beliefs; reserve at least half for the full scan |
| Provider input | At most 4 groups of 32 beliefs each, also bounded by tokens and bytes |
| Provider calls | At most 2 batched calls per slice, proposal and verification; no per-candidate calls |
| Changes | At most 8 operations per slice |
| Time | At most 120 seconds per slice, with individual provider timeouts |
| Spend | At most USD 0.25 per slice and USD 2 per principal per UTC day |

These are reconsolidation budgets, not changes to existing formation
budgets. Persist reservations before provider admission so concurrent workers
and retries share the same ceiling. Price the maximum input/output request
before sending it; unknown pricing, unavailable evidence or exhausted budget
defers the group. Retry attempts count, and unsuccessful validation commits no
belief mutation. Inventory progress and deferred provider work have separate
cursors so either can proceed without misreporting the other's coverage.

## Provenance, merging and forgetting

Every operation retains its policy/model/evidence identity, source belief IDs
and revisions, original event IDs, scope, input digest and output lineage.
Distinct source events determine corroboration; duplicate records, repeated
dreaming passes and citations do not create new evidence.

An equivalence merge uses a canonical record plus reversible membership and
recall suppression, not destructive deletion. Initially automatic merging is
limited to claims whose equivalence local rules can establish with matching
attribution, temporal meaning, polarity and scope. Provider-only paraphrase
matches remain proposals or summaries until their verification contract has
measurable evidence. Explicit owner memories are never silently rewritten.
Higher-level summaries supplement atomic facts; they do not replace them.

Source visibility is the intersection of its dependencies' permissions and
portability, and its sensitivity is at least the strictest source. Conflicting
source scopes abstain rather than selecting a broader one. Attributed
communication stays attributed, local, sensitive and tentative; no chain of
summaries makes it owner speech. All output remains untrusted memory content,
never system instructions or persona. The existing secret and injection checks
apply to both inputs sent to the provider and proposed outputs.

Connections require at least two distinct original supporting events. They
enter the existing hypothesis lane, remain marked as inferred, and never gain
the authority of an explicit user statement. Their original supporting evidence
determines the evidence clock: reconsolidation does not reset it. Initially
their expiry is no later than thirty days after the latest supporting evidence
and no later than the expiry of any required support. Already-expired proposals
are discarded. Summaries and canonical membership preserve the underlying
claims' lifetimes instead of rejuvenating them. New independent evidence may
justify a later update through the existing governed formation path.

This policy forbids a generated hypothesis or summary from serving as fresh
support for another synthesis. Dependency graphs are acyclic and terminate in
admitted original evidence. Repeating the same inputs is idempotent, including
after a policy update: corrections and prior rejections remain a commit gate.

## Correction, erasure and inspection

Add durable operation, membership and dependency records through the existing
unit of work. A scoped generation/fence plus reverse dependencies invalidates
derived outputs synchronously when any required source is corrected, rejected,
expired or erased. Reads and in-flight commits honor that invalidation before
bounded physical cleanup finishes. Source deletion races, worker restarts,
cached snapshots and active recall deltas must obey existing erasure and
correction guarantees. Retained audits contain identifiers and reason codes,
not deleted text. A previously delivered reply follows the established session
erasure contract; this feature cannot retract content already delivered.

The memory browser should show the resulting summary or hypothesis, its
supporting memories, why it changed, and whether it was merged or inferred.
Reuse review/delete actions and add an owner-visible operation history and
safe undo for merges. Undo restores eligibility only for still-valid sources,
cannot revive erased or subsequently corrected content, and records a durable
constraint against repeating the same rejected merge. The contracts below define
the additive routes and projections; existing route-table and client compatibility
tests must expand when those surfaces land.

## Domain and persistence contracts

All values are frozen, reject unknown fields, carry a tenant/principal pair and
use aware UTC instants. Identifiers are UUIDs; money is non-negative Decimal in
USD. A store never accepts a caller-supplied owner in place of its authenticated
`Principal`. The implementation adds these records rather than overwriting the
single-session provenance shape of `MemoryRecord`:

| Record | Required fields and invariants |
| --- | --- |
| `ReconsolidationJob` | id, owner, policy version, due day, state, lease owner/token/expiry, attempt count, full-scan generation and cursor, changed-scan cursor, revision; unique owner/due day/policy |
| `ReconsolidationGroup` | id, job id, sorted source IDs and content revisions, input digest, fence generation, state, retry count, reason; unique owner/policy/input digest; no source text in queued work |
| `ReconsolidationOperation` | id, owner, kind, state, revision, group id, input digest, policy/model/evidence identities, created/committed/invalidated/undone instants, reason; immutable kind and original input identities |
| `ReconsolidationDependency` | operation id, source belief id, content revision, source session/event identity and evidence instant; owner-bound composite foreign keys; complete support, never a provider-chosen source |
| `ReconsolidationMember` | merge operation id, canonical belief id, member belief id; member belongs to at most one active equivalence set; original beliefs survive |
| `ReconsolidatedMemory` | id, operation id, kind `summary` or `hypothesis`, ordered grounded clauses, subject/type, sensitivity, scope constraints, confidence, evidence and validity instants, status, store position; no fabricated single source session |
| `ReconsolidationBlock` | owner, stable claim/source signature, reason `owner_undo` or `owner_rejection`, created time; independent of model/policy version, contains no deleted prose |
| `ReconsolidationSpend` | owner, UTC day, reservation id, request digest, maximum reserved and settled amount, state; one reservation per attempt; totals are shared across jobs/workers |

Operations have `proposed -> committed|rejected|stale` and
`committed -> invalidated|undone`. Invalidated/undone operations cannot become
committed again. A new independent evidence set can form a distinct operation,
but cannot evade an owner rejection. `flag_conflict` records an operation with
source IDs, never a claim that one source is true. `no_change` is a group outcome,
not a derived belief. A merge has at least two members. Every summary clause
has its own non-empty source support; every hypothesis has at least two distinct
original supporting events. IDs alone are not proof that an event supports text.

Add `content_revision` (positive integer, initially 1) to `memories` and its
historical revision representation. Increment it on statement, polarity,
subject, confidence, evidence, authority, sensitivity, scope/portability, validity, review
rejection, status or person-attribution changes. Usage-only updates to utility
or `last_used_at` do not increment it. Existing `store_position` is not a content
revision: decay intentionally preserves positions until retirement. Membership
and derived objects get their own monotonically increasing revision and new
store positions through the existing allocator when recall-visible state changes.

`ReconsolidationStore` is a new unit-of-work repository. Its async methods are:

```text
claim_due(principal, now, lease_owner, lease_seconds=180) -> JobLease | None
renew(principal, job_id, lease_token, now) -> JobLease
inventory(principal, generation, lane, cursor, limit) -> SourcePage
queue_group(principal, lease_token, inputs, input_digest) -> Group
claim_group(principal, lease_token, now) -> GroupLease | None
reserve(principal, group_lease, request_digest, maximum_usd, now) -> Reservation
settle(principal, reservation_id, outcome, actual_usd | None) -> SpendReceipt
commit(principal, group_lease, expected_inputs, expected_fence, proposal) -> Operation
finish_group(principal, group_lease, outcome, reason) -> Group
invalidate(principal, source_ids, reason) -> InvalidationReceipt
get_operation(principal, operation_id, ceiling) -> OperationView
list_operations(principal, ceiling, cursor, limit) -> OperationPage
undo(principal, operation_id, expected_revision, idempotency_key) -> OperationView
```

Read results are domain values; adapters translate ORM rows by hand. Exhausted
leases, revision mismatches and lost fences fail as conflicts without mutation.
`commit` checks visibility, source validity, all outstanding rejections, budgets,
current policy identity and membership uniqueness. It locks source rows in sorted
UUID order, rechecks leaf-source erasure fences, then writes operation,
dependencies, membership/projection and audit in one short transaction. Source
mutation paths use the same ordering and invalidate dependencies in their own
transaction. No await to a provider occurs in either transaction. In-memory
adapters provide the equivalent atomic unit of work and shared contract suite.

Migrations are additive: create owner-scoped job/group/operation/dependency,
membership/projection/block/spend tables and indexes; add/backfill
`content_revision=1` and immutable `creation_sequence` without changing evidence
or expiry; and advance the repository's migration-head assertion. Index due jobs, pending groups by age,
dependencies by source, active membership uniqueness, operation list order and
spend by owner/day. A partial active-member uniqueness constraint and owner-bound
foreign keys prevent racing merges and foreign references. Downgrade requires
the feature disabled and no leased work, removes only new derived state and
columns, and leaves original memories and existing rejection/erasure records
unchanged. It must explicitly report that derived history/undo is lost.

## Scheduling, grouping and recovery

The maintenance role starts at most one non-blocking reconsolidation task per
process. It returns to existing sweeps immediately; a separately awaited task
performs the bounded slice, so its provider latency cannot delay expiry or
approvals. Fleet-wide owner admission is enforced by the durable lease, not a
process flag. Shutdown stops admission, cancels the task and leaves fenced work
recoverable. A slice's 120-second wall deadline is below its 180-second lease;
renew every 30 seconds and cancel provider work on lease loss. At most three
failed attempts run for an identical group before a terminal audited failure;
new source revisions create new eligible input. Due days do not create a backlog
of identical full scans: continue an unfinished scan and coalesce missed days.

At generation start record a stable maximum committed insertion sequence and
current change watermark. Add immutable `creation_sequence` to original rows;
allocate it under the same owner lock that snapshots a generation bound, so a
late commit or backdated `created_at` cannot slip behind a saved cursor. Backfill
existing rows in `(created_at, id)` order while admissions are locked. Full
inventory orders by `(creation_sequence, id)`, never mutable recall position.
Include live direct records admitted by current source policy; excluded rows advance the inventory cursor
with a content-free reason. New creations beyond the generation bound wait for
its successor. Changes use a durable monotonically sequenced change journal;
expiry/erasure entries invalidate dependencies even when their source is gone.
A creation, revision and change-journal entry commit atomically. Compact a
journal prefix only after the consumer watermark has passed it.

Each 128-anchor slice reserves 64 slots for full inventory. Unused capacity
in either lane may be borrowed, but new changes cannot displace the full-lane
reservation. A slice checkpoints scanned anchors even when they have no useful
neighbors. It creates at most four groups; overflow anchors receive a durable
`not_selected` receipt, not an unbounded queue. The next generation rotates the
start key used for equal-ranked grouping. Pending model work is capped at 256
groups per owner; when full, pause selection without losing the unscanned cursor.
Within that cap select oldest groups first. Report scan coverage separately from
unselected/deferred/model-processed groups: budget exhaustion is not completion.

For an anchor, query eligible original direct memories across sessions using
exact subject and known entity IDs first, then deterministic lexical overlap.
Take at most 31 neighbors, ordered by exact-subject/entity match, overlap score,
creation key and UUID; append the anchor. Filter owner, lifecycle, erasure,
sensitivity and scope before the bound. Scope is compatible only for equal
local scopes, or shared user scope with every input portable under existing
rules. No implicit crossing of local projects is admitted. Group dedup hashes
sorted source IDs/revisions and the current policy. The complete scan test uses
100,000 synthetic originals, concurrent inserts and repeatedly changing recent
memories; every original eligible creation key must be visited within
`ceil(N/64)` successful full-lane slices. Model processing need not cover every
anchor, and its coverage must be reported honestly.

Reservations serialize on the owner/day row. Each of the at most two requests
is prepriced at its maximum input and output tokens, admitted only if it fits
both slice and daily headroom, and consumes a durable reservation before send.
Unknown settlement after timeout or crash charges the full reservation rather
than releasing it for another attempt. A verified lower bill releases only the
difference. Retrying requires a new reservation. Across a UTC midnight, a slice
keeps its original day bucket and cannot borrow from the new day. The job may
resume on a new slice the next day. A kill switch stops new provider admissions;
invalidation, deletion and spend settlement remain active.

## Provider, merge and derivation contracts

The provider sees source records with opaque IDs, exact admitted event excerpts,
source timestamps, attribution, visibility constraints and untrusted-content
labels. The input contains no gold labels, prior generated summaries or arbitrary
history. Bound each request to 16,384 input tokens, 4,096 output tokens and
65,536 UTF-8 bytes, and each source excerpt to 2,048 bytes at source boundaries.
A source that cannot be supported within the bound defers rather than receiving
a misleading truncated citation. Input memory and its leaf evidence must satisfy
the provider's current sensitivity/residency policy before serialization.

The source-only request builder reads a claimed group's current original versions
through the maintenance repository under the owner and People locks. It admits
only authenticated owner `user.message.created` text, retaining each entire string
or text part, its timestamp and stable event/part identity. Shared copies of one
event remain one evidence identity. Every required part must fit: at most thirty-two
parts per event/source and 256 excerpts per group. Missing, changed, excluded,
rejected or oversized support defers the group without truncation. Attachments,
assistant/tool/scheduled content and arbitrary history do not become evidence.

An explicit local policy function assesses every source record and original
excerpt independently, with its scope, portability and attribution. The memory's
classification is a floor; it cannot classify the whole original message. Each
decision must match the owner, resolved provider/model, preparation instant and
policy version, explicitly allow residency, and classify the content as public or
internal. Missing classification, unknown residency, policy failure or mismatched
decisions defer the group before request serialization. Production composition supplies the explicit local classification and residency
policy described below; the builder never supplies an implicit allow decision.

The `reconsolidation-input@1` payload allowlists source fields, uses opaque scope
identities, labels all supplied content untrusted and offers no tools. It binds the
response context to the admitted identities/revisions. A batch admits at most four
groups from one job/lease with consistent shared source and excerpt snapshots;
deferring one group preserves valid siblings. The conservative input estimate is
one token per UTF-8 byte of the complete neutral `ModelRequest`, including system
instructions, schema, framing and metadata. Context/output capacity and all declared
byte/token ceilings apply. Explicitly configured prices, including separately priced
reasoning and the highest cache/input rate, bound cost before reservation; unknown
prices and amounts above the slice ceiling defer. An explicitly free request retains
the reservation protocol's minimum positive quantum, which settlement may reduce.

Prepared requests keep immutable serialized bytes and a digest binding the selected
provider/model, pricing and egress policy. They are transient previews, not permission
to send or commit. Orchestration must freshly recheck source/lease/policy, reserve
the complete request against both durable spend ceilings, and use those exact bytes
and resolved model. The pure builders perform no provider call or persistent write;
the separate admission service below owns the reservation transaction.

Verification preparation accepts the original request, the structurally valid
proposal and freshly read original snapshots. The job/lease and preparation window
must still match; current source identities, revisions, exact payloads and egress
decisions must agree with the proposal's admitted originals. Each proposed clause
also receives its own local sensitivity/residency assessment before export. Proposed
text is explicitly labeled untrusted claim data, never supporting evidence.

Local source/support, hazard, policy and budget failures exclude only the affected
candidates. The verifier receives complete original evidence for the remaining
candidates and their exact clauses, never excluded prose. Its digest still binds
the entire original proposal. Local exclusions are retained separately in the
transient prepared request; they cannot be supplied or reversed by the provider.
The prepared-response validator requires exactly the remaining clause verdicts,
rejects invented verdicts for excluded candidates, and carries local rejections
unchanged into the review. It checks the serialized request digest and its exact
offered operation projection, so altered claims or exclusions cannot reuse a reply.
The complete second request must fit all token/byte limits and the caller's
remaining slice headroom. A request too large for all candidates retains valid
bounded siblings. Even a no-change proposal needs a matching verification envelope.

`ReconsolidationRequestAdmission` is the worker-facing preparation/reservation
service. For each request it holds the memory owner and People guards, reads current
claimed originals, applies local policy, then reserves the exact prepared request
digest and maximum cost in the same unit of work. The time used for admission is
sampled after lock waits; the reservation checks the current slice deadline and
durable shared budget. A disabled or withdrawn admission predicate refuses the
transaction, and a failed commit returns no request/reservation pair. Duplicate
reservations refuse another send; previously reserved unknown spend stays charged.
The service returns after commit and performs no external call. A separate
`ReconsolidationBatchExecutor` consumes this admission seam for already claimed
groups on a caller-owned lease. The caller resolves the provider/model outside
transactions; the executor deep-copies that resolution for the whole batch and
checks the exact request/pricing binding before sending. It never reroutes or
reuses an existing reservation. Each request sets the neutral
`maximum_provider_attempts=1`: all three real adapters suppress internal transport,
stream and optional-summary retries for that request. Ordinary callers retain
their existing retry behavior when the optional cap is absent.

The executor runs proposal then verification outside every unit of work. It uses
the original slice deadline, renews the lease every thirty seconds, and bounds
each call to thirty seconds with a ten-second idle limit. The slice has a monotonic
120-second ceiling as well as the durable wall-clock check. Lost leases, withdrawn
admission and shutdown cancel the provider stream. Stream identities/sequences,
terminal completeness, no-tool output, reported token ceilings and UTF-8 bounds
are checked before response admission. Provider failure or malformed replies
return finite reasons without provider prose and never produce fallback claims.
Verification repeats source/policy admission after proposal settlement.

Settlement runs independently of the admission switch, with at most five seconds
for cleanup. Only attributable, positive, internally consistent terminal token
counts within the admitted ceilings can lower a reservation, repriced against the
copied model prices and rounded upward to the durable money quantum. Missing
usage or an unknown separately priced reasoning count retains the full charge.
Timeout, interrupted streams and unverified failures settle as unknown. If lease
loss, transaction failure or repeated cancellation prevents settlement, the existing
durable reservation remains fully charged and recovery marks it unknown. The
returned settlement receipt is published only after its transaction commits;
settlement failure stops the batch. A new slice still needs a new reservation.

Execution returns ephemeral prepared content and a local-validation review plus
content-free call receipts; it writes no merge, summary or hypothesis.
`apply_review` connects that local result to the existing merge and extractive
summary planners. It validates the prepared request and review binding, then takes
the owner/People guards and rechecks current source revisions in one transaction
per group. Each candidate can select a distinct subset of the offered originals;
the planners retain exact evidence, scope, active-membership and owner-undo checks.
Each operation records its own complete subset dependencies and input digest.
Summary planning uses the existing source-dependency index and policy-independent
evidence identity to refuse repeated inputs, including across overlapping groups.
Valid siblings survive candidate rejection. All accepted writes in a group and
its completion commit together; returned operation IDs appear only after commit.
Admission withdrawal rolls back earlier writes in that group. Stale groups retry
independently, and replay cannot recommit a completed group.

Migration `ea3210a1b009` replaces the former one-operation-per-group constraint
with a lookup index, preserving the eight-operation slice limit and unique active
merge membership. Downgrade refuses running work or multi-operation history.
The application also routes reviewed connections and conflicts through their
local writers. Invalid connection support completes as no-change without mutating
originals; stale work retains its bounded retry path. The composed pass owns lease
release and requires matching activation evidence.

The closed proposal envelope carries a schema version, group IDs and up to eight
operations. Every operation has a kind, input IDs/revisions and, for each clause,
text plus supplied source/excerpt IDs. No arbitrary source, tool call, instruction
or provider-selected authority is accepted. The second batch verifies entailment
and reports `supported|unsupported|uncertain` per clause plus finite reason codes.
Unknown keys/kinds, duplicate operation IDs, malformed envelopes or missing
verification refuse the affected batch. A candidate-local failure after valid
structure rejects only that candidate. Timeouts and unavailable providers leave
original memories intact, with no synthetic fallback claims. Content-free audit
records calls, usage, reasons and stage outcomes; it stores no chain of thought.

The spend reservation carries a closed optional call audit, written in the same
transaction as admission. It names the batch, stage, offered group IDs, configured
provider/model, pricing and egress-policy digests, and admission time. Settlement
atomically adds completion time, elapsed milliseconds from the injected clock,
a finite transport outcome and verified token counts. Transport completion does not mean schema validation
or permission to write. A separate immutable stage decision records preparation,
review, deferral or invalid-response status with bounded candidate/group counts.
The existing group states and operation records remain the source of commit
outcomes; no parallel operation journal is added.

Unknown completion retains the full reservation. Recovery marks admitted calls
with no committed completion as `recovered_unknown`; it never fabricates usage or
a successful validation. Old reservations may have no audit. Exact replay is
idempotent; different settlement/decision evidence is refused. Audit reads are
owner-bound and by reservation ID, independent of the current lease. Writes still
require the current lease; admission withdrawal cannot disable settlement. The
payload contains no prompt, response, source text, error message, credentials or
private reasoning. Migration `ea3210a1b00a` adds this nullable payload to the existing
forced-RLS spend table, leaving older reservations without invented audit data.
Downgrade refuses retained audit records or leased work. Verified usage is copied
before settlement can await database locks, so its durable counts match the bill.

The transient response boundary uses `reconsolidation-proposal@1` and
`reconsolidation-verification@1`. A locally created `ProposalContext` binds a
fresh batch UUID to the authenticated owner and the supplied group, belief,
content-revision and excerpt identities. It contains identity bindings only;
the source-only input builder enforces original-source admission, current provider
egress policy, exact excerpts and request budgets before constructing it. A provider
response cannot supply or override this context.

The proposal carries `schema_version`, `batch_id`, `group_ids` and `operations`.
Each operation carries `id`, `group_id`, `kind`, `inputs` (belief UUID and content
revision), and `clauses`. Each clause carries `text` and `support` pairs of belief
and excerpt UUIDs. Merge and connection suggestions have exactly one clause;
summaries have one through four; conflict and no-change outcomes have none.
Every supplied group needs an outcome, and `no_change` cannot coexist with another
outcome for that group. Inputs are distinct and number two through thirty-two.
Merge/connection support names every operation input. A summary may omit inputs
from its rendered clauses; its later local plan still retains complete lineage.

The verification carries `schema_version`, `batch_id`, `proposal_digest` and
`verdicts`, exactly one per requested operation UUID and zero-based clause index.
The legacy full-batch validator requests all clauses; the prepared-request path
excludes locally rejected candidates as specified above. The digest
is SHA-256 of the parsed proposal's sorted-key, compact UTF-8 JSON, without ASCII
escaping. A supported verdict has reason `entailed`; unsupported uses
`contradicted` or `policy_excluded`; uncertain uses `insufficient_evidence` or
`ambiguous`. Status/reason mismatches, missing or extra verdicts and replies for
another batch or proposal refuse the entire batch. Empty-clause outcomes still
require the matching verification envelope, whose verdict list may be empty.

The parser rejects unknown fields at every level, duplicate JSON keys or operation
identities, non-finite numbers, coerced revisions/indices and malformed Unicode.
Each response has an additional 65,536-byte UTF-8 guard before parsing; this does
not replace the request token/output-token limits. Once structure is valid,
unknown source/excerpt IDs, stale revisions, invalid support, unsafe output and
unsupported/uncertain clauses reject only their own candidate. Safe siblings
remain available for local validation. Errors expose finite reason codes without
provider prose. Returned proposals are ephemeral content, not audit payloads.

The implemented response boundary performs no model call, source retrieval or
mutation. Its `requires_local_validation` result is explicitly insufficient for
merge, summary or hypothesis admission: kind-specific rules below, evidence
identity/grounding checks and transactional source revalidation still apply.
The separate executor does not change that authority boundary. Operation
orchestration and comparative activation evidence remain pending; these components
do not advance a complete runtime gate.

Automatic equivalence uses NFC normalization and whitespace collapse only;
case, punctuation, negation, dates and quantities remain significant. All members
must have equal normalized statement, subject, belief type, claim kind, polarity,
derivation, attribution, scope, portability, authority, sensitivity, `valid_from`
and expiry. Canonical selection is deterministic: oldest creation key, then UUID.
Distinct evidence sets remain distinct provenance, never additional corroboration
from the operation itself. Active USER/AFFIRMED records may be grouped for recall
but retain their text, status and independently browsable identity. A second
active group overlapping a member is rejected and replanned as the union of the
same equivalence key. Provider-only semantic similarity cannot authorize a merge.

A summary initially consists of up to four extractive source clauses with an
optional fixed local heading. Each clause must equal a selected source statement
after the same normalization, with correct source IDs. The provider can select
and order clauses but cannot introduce a generalization under the summary kind.
At most 1,024 characters render; if the clauses do not fit, retain fewer whole
clauses and list the omitted source IDs in the inspection view, never claim full
coverage. Original atoms remain independently retrievable at every context budget.

A connection is one novel tentative hypothesis supported by two or more distinct
admitted events from at least two source beliefs. Local validation enforces
source identity, named-entity/quantity/negation consistency, policy exclusions,
source visibility and expiry; provider verification must also say supported.
Otherwise abstain. Novelty is allowed only in the explicit hypothesis lane, not
through the equivalent or summary paths. Confidence is at most 0.35 and no higher
than the least-confident supporting source. `last_evidence_at` is the latest
supporting original event time, `valid_from` is commit time, and expiry is the
minimum of evidence time plus thirty days and every finite support expiry/validity
end. If expiry is at/before commit, no hypothesis forms. Sensitive-trait inference
barred by People policy remains barred. Generated content cannot become a future
original source; dependency traversal terminates in original admitted events.

The stable rejection signature binds normalized claim/kind, owner, attribution
and source identity independent of prompt/model versions. Owner undo blocks the
same member set, including any attempted subset containing a rejected pair.
Neither a renamed subject nor a policy upgrade evades the existing semantic
rejection matcher. New independent evidence goes through that matcher too.

## Read paths, lifecycle and owner surfaces

Derived memories have separate storage but use the existing memory recall budget
and sensitivity-filtered projection. The new recall projection discriminates
`belief|summary|hypothesis`, carries a UUID usable by `[m:...]` citations, and
maps a derived UUID back to its operation and complete support. Every reader
rechecks input content revisions, lifecycle, source erasure fences and visibility.
An invalid dependency immediately withholds the whole derived item. Do not wait
for the cleanup sweep or a refreshed index. Disabling reconsolidation restores
ordinary atomic recall and hides derived recall, without disabling history,
review, undo or erasure.

A recall selects original atoms first, preserving all existing priority and
durable-reservation rules. Equivalent membership suppresses only duplicate
renderings that pass the same visibility checks; a hidden/expired canonical
falls back to a visible valid member. Summaries/hypotheses compete only for
remaining slots and cannot displace a needed original merely because they are
newer. A summary that repeats already selected atoms is omitted. This initial
ordering must still meet the downstream-value gate; do not weaken that gate if
it proves insufficient. Historical reads preserve previous group state only
when the original source remains permitted; erasure hides every revision.

New sessions may snapshot derived items. Existing sessions keep frozen prefix
bytes; the governed next-turn recall delta reports invalidated derived items and
merge undo, and suppresses erased copies through the existing generated-copy
fence before model admission. A faithful citation changes derived utility and
usage only. It does not reinforce the supporting atoms, evidence time or expiry.
Ambiguous short IDs are refused rather than credited. Source correction,
retraction, expiry, session deletion, principal deletion, email exclusion and
People erasure all enter reverse invalidation. Existing People merge/split also
invalidates a derived attribution that references changed identity; dreaming
never changes those identity rules. Recompute only from currently valid support,
never subtract a deleted phrase from a stored summary and assume it is valid.

Add the following owner-only routes behind both `AGENT_MEMORY_API_ENABLED` and
`AGENT_MEMORY_RECONSOLIDATION_API_ENABLED`, each default off. The latter enables
only the operation surface. The read/history/undo routes remain available
when the processing kill switch is off. Cursor pages default to 50, maximum
100, ordered descending `(created_at, id)` and bound to owner/filter/ceiling.

| Route | Scope and contract |
| --- | --- |
| `GET /v1/memory-reconsolidations` | `memory.read`; `kind`, `state`, required sensitivity ceiling and opaque cursor; content-filtered operation summaries and `next_cursor` |
| `GET /v1/memory-reconsolidations/{id}` | `memory.read`; operation revision, kind/state/reason, visible output and source links; any unavailable required source withholds content, never leaks titles/counts |
| `POST /v1/memory-reconsolidations/{id}/undo` | `memory.write`; body `{expected_revision}`, required `Idempotency-Key`; only a committed merge; atomically remove active suppression and persist the rejection block |

Success returns the same operation view. Unknown/foreign/above-ceiling IDs are
`not_found`; missing scope is `authorization_error`, stale revision, wrong kind
or reused key with another body is `conflict`; bad filters/body are
`400 validation_error`, with empty error details; existing resources retain their
validation codes (ADR-0169). The undo body is closed and its positive revision is
a strict integer. Keys must be nonblank and at most 128 characters. Every route
requires an explicit sensitivity ceiling; successful responses are private and
non-cacheable. Replaying the same committed undo returns its receipt even
if inputs have since disappeared, with content filtered again. No route triggers
provider work or allows arbitrary merge/derived-text writes. Keep existing
`/v1/memories` reads compatible: add optional `include_derived=false`, and an
additive `record_kind` plus `operation_id` on derived rows. Existing ordinary
record fields and behavior do not change. Lookup/review/delete of a derived UUID
uses the governed derived service, not a fake single-session `MemoryRecord`.
`Not true` and Delete install durable suppression and invalidate descendants;
Mark reviewed changes neither truth nor authority. Expired/deleted support never
revives during undo. Extend the explicit M17 route and projection contracts.

Derived owner controls use a separate allow-listed summary view: `id`,
`record_kind=summary|hypothesis`, `operation_id`, revision, active/retired status, review
flag, creation/update times, optional content and complete supporting-source
links. Content includes the extractive clauses and their inherited scope,
portability, sensitivity, authority, confidence and evidence/validity clocks;
it does not invent a single source session or formation run. Ordinary views
remain byte-compatible. Opt-in browsing merges both kinds by the existing
descending store position and ascending UUID cursor. Summary filters apply to
its rendered text/subject, inherited belief types, review flag, and any complete
support session. Only currently valid active summaries enter browse pages;
operation history supplies invalidation history. Disabled surface flags leave
derived lookup unavailable and reject opt-in browsing as malformed requests.

Dismiss clears only the derived review flag. Not here sets a current local-scope
restriction without changing original support or the prepared evidence payload;
the restriction also applies to historical recall. Not true returns a retired,
content-free derived view and withholds it from recall at every historical clock.
Delete additionally makes derived detail unavailable. Both remove generated
copies through the existing erasure fence and durable cleanup manifest, preserving
every original belief and source. Derived writes and opaque idempotency receipts
commit together under the owner's email and People locks; receipt keys share the
memory-write namespace with ordinary writes. Review replay revalidates current
visibility; delete replay returns the existing content-free success.

Summary rejection blocks contain only namespaced hashes of the normalized rendered
claim set, its attribution and stable leaf evidence identities. An additional
claim/attribution signature blocks recreation from duplicate or newly supplied
copies, independently of subject, order, case, policy and model identity. Blocks
apply to summary generation and historical/current readers, never to original
beliefs. Each committed operation retains the opaque rejection signatures so a
rejection also fences equivalent existing summaries and their generated copies,
even after their content projection has been purged. Owner control metadata and receipts contain no source prose. Owner
restriction is overlaid only after validating the immutable prepared projection;
usage updates must retain that original validation payload.

The operation view is an allowlist: operation ID, kind/state/revision/reason,
policy/model identity, creation/commit/invalidation/undo times, optional content
and visible source links. Complete exact support is required for content and all
links, including omitted summary inputs; partial source titles, links and counts
are never returned. A currently committed operation whose support fails validation
is atomically invalidated and its derived projection purged. When retained support
cannot certify a lower sensitivity ceiling, only the owner's `restricted` view
returns opaque metadata with null content and no sources. Lower-ceiling detail
reads return not-found and lists omit it. An already-undone operation preserves
its receipt after source loss; repeated undo rechecks the requested ceiling.
No source content is copied into durable operation history to support inspection.

Current operation reads sample their validation clock after acquiring the owner
guard; connection or lock waits cannot reuse an expired pre-wait view.
Pagination filters state after source revalidation, so elapsed expiry can appear
in the invalidated listing without a prior read. Repository batches contain at
most 100 owner-bound operations; filtering continues until the visible page and
lookahead are filled or history ends. The cursor contains the last visible
`(created_at, id)` position and a fingerprint of owner, kind/state and ceiling.
It is a continuation binding, not an authorization token; each resumed read
reapplies owner and visibility checks. Invalid bindings return validation error.

CLI inspection adds `agent memory reconsolidations list|get|undo`; all require
`--ceiling`. List accepts `--kind`, `--state`, `--limit` and `--cursor`; get and
undo take an operation ID. Undo also requires `--expected-revision` and a reusable
`--idempotency-key`. These operator commands share the principal-explicit service
with HTTP and remain available independently of the HTTP surface flags.
`agent memory reconsolidate --once`
requests one bounded owner job, respecting disabled/unevidenced policy; `--dry-run`
returns selected IDs and exclusion reasons without provider calls or writes.
The once/dry-run commands remain future work until their implementation gates bind.
The native memory browser adds origin labels, supporting-source links, operation
history and an Undo merge action with an affected-memory preview. Old servers
show the controls unavailable; old clients continue ordinary browsing. Native
journeys cover browse/detail, source visibility, review/delete, undo/retry/conflict
and processing-disabled history. No new Chat tool or notification is introduced.

The native **Synthesis** collection is an evidence-first journal beside Memories
and People. Server-filtered All, Summaries, Connections, Conflicts and Merges lenses and an optional state
filter browse the operation history, including when processing is disabled. Cards
name the operation's kind and state rather than presenting a summary as an original
belief. Opening a card re-fetches its current operation and, for an active summary or connection,
its governed derived detail. Each claim can disclose exactly its supporting
originals; omitted inputs remain separately inspectable. Source navigation reads
the original again before displaying it. Opaque history has no inferred titles,
source counts or cached content. Unknown kinds remain inspectable without actions.

Undo first previews every visible affected original and explains that only duplicate
suppression is reversed; it requires a current committed merge and complete visible
support. Writes retain their key and revision across an uncertain retry, disable
competing actions, and refresh after conflicts. Refresh, connection changes and
backgrounding clear private content and invalidate late responses. Only opaque
pending write identity survives backgrounding for an explicit retry on the same
connection. The journal never starts processing, infers new relationships locally,
or treats a loaded-page count as the size of the memory bank.
Action feedback stays visible outside the scrolling detail so success, failure and
an uncertain retry cannot disappear below the currently visible sources.

## Frozen evaluation contract

`evals/capability/memory-reconsolidation.v1.json` is a synthetic corpus with
separate development and holdout cases. `memory-reconsolidation.manifest.json`
pins its SHA-256, scorer `reconsolidation-scorer@1` and its imported F1
implementation, and the unchanged Milestone 16
baseline/corpus digests. This reference baseline is the existing maintenance
control, not fabricated measurements of an unimplemented dreaming arm. The
future driver must additionally record the original-only control on the M32
seeds before comparing any new arm. Freeze that observation artifact before
provider tuning; comparative-quality stays pending until all three arms run.

The offline original-only **retrieval control** is now recorded separately in
`evals/capability/memory-reconsolidation-control.v1.json`, with its own digest
manifest. `agent eval memory-reconsolidation-control --output PATH` runs all
twenty-four cases three times in fresh in-memory compositions. It imports the
synthetic seeds as already-admitted originals, calls the existing expiry and
decay services once, and collects actual store rows and persisted recall traces.
It does not measure formation or ask an answer model. Gold answers, duplicate
classes, expected hypotheses and surviving-ID labels are absent from runtime
case inputs; only the separate scorer reads them.

The mapping is fixed at `reconsolidation-control@1`: fact records retain source
session/event identity and evidence time; owner statements use USER authority,
0.9 confidence and internal sensitivity, with portability only for user scope.
Attributed seeds remain provisional, inferred at 0.35, sensitive and local to
their source session, with a thirty-day evidence lifetime; they are never
fabricated owner messages or new authenticated communication admission. Expired
seeds expire at the fixed control instant; deleted seeds use governed deletion.
Recall uses the production query former and retriever at 2026-10-02T12:00:00Z,
general scope, 2,000 tokens, twenty items and minimum score 0.12. These fixed
choices are recorded rather than inferred from answer labels. Ordinary exact
duplicate rendering remains enabled; surviving originals and returned items
are reported separately.

Recording refuses incomplete/repeated cases or probes, failed attempts, changed
source bytes, changed questions/budgets/clocks and inconsistent store/trace
observations. Each failed attempt remains in the run census. The file is created
exclusively and never overwrites a previous recording. It binds the base commit,
dirty-tree status, actual source/config/lock digest and frozen input digests.
`answer_evaluation=not_run`, zero provider calls and `activation_evidence=false`
prevent this offline observation from posing as a live answer baseline or release
evidence. The separate comparison runner below records the answer-bearing original control
and both new arms before provider tuning; passing comparative quality remains
required before activation; the frozen Phase 1
and M16 files remain unchanged.

Cases contain original source records, query/answer probes, permitted duplicate
classes, required surviving IDs and acceptable hypotheses with exact support
sets. Development and holdout IDs are disjoint and neither split is tuned in
this phase. Corpus labels never enter runtime code or provider input. The pure
scorer accepts observations collected by the harness from actual store/trace
state: merged groups, surviving source IDs, rendered hypotheses and answers.
The provider cannot self-report retained facts or substitute its own metrics.
Collection, cost enforcement, source evidence and policy/race checks are separate
runtime gates; this scorer alone is never release evidence.

For each case, count every unordered pair in every proposed merge, including
transitive equivalence implied by overlapping groups. A pair is correct only
inside one labelled duplicate class. Unknown IDs, singleton groups and repeated
members are invalid operations. Missing required survivors count as lost facts;
unknown survivors are invalid observations. Duplicate-set coverage counts a
class only when one merged component covers every member and no extra member.
False merges and lost facts must both be zero, and at least 80% of classes must
be covered; empty denominators never count as successful coverage.

In v1, hypothesis matching is exact NFC/whitespace-normalized statement plus the exact
unordered original-support set against an explicitly allowed label. Matching is
one-to-one: a duplicate output is a false positive, not another true positive.
Wrong polarity, source attribution, quantity or date fails even if words overlap.
Recall = unique matched labels / all expected labels; precision = matched outputs
/ all outputs. Zero output with expected labels gives zero recall and undefined
precision, so abstaining everywhere cannot pass. Aggregate integer counts across
cases before dividing. Report per-split counts and coverage, not an average of
per-case percentages with empty denominators hidden.

For probes, use the Milestone 16 token-level answer F1 implementation and the
same answer aliases. Cross-session answer coverage counts a probe only at F1=1;
missing output scores zero, duplicate/unknown probe IDs invalidate the observation.
The driver fixes an identical context budget, query set, seed store and clock for
all arms. Connection-enabled minus control coverage must be at least 0.10
absolute on both splits. Hypothesis precision >=0.80 and recall >=0.60 apply to
both splits over three complete seeded repeats; count every failed case as failed,
never omit it. The equivalence-only arm must pass the merge and direct-recall
floors independently. Existing M16 direct recall and applicable M21/People
precision gates also run unchanged, preventing this corpus from narrowing them.

An evaluation artifact includes schema/policy/scorer/corpus hashes, code commit,
model/provider/reasoning effort, dependency-policy hashes, repeated per-case
observations, derived counts, failures and actual usage/cost. Publication recomputes
metrics and thresholds and refuses incomplete runs, unknown inputs, mismatched
budgets, missing repeats or any boundary failure. Rewriting a corpus/scorer or
changing the active dependency tuple invalidates its activation evidence. The
pure Phase 1 scorer intentionally provides no activation/publication function.

### Approved offline equivalence review (ADR-0170)

The owner approved `reconsolidation-review@2` on 2026-10-08. It is a separately
frozen offline scoring contract; v1 files and recorded results stay immutable.
Its manifest pins the corpus, v1 scorer/controls and review/aggregation/recording
implementations. A comparison opting into it validates and records that manifest's
digest before the first provider call and rechecks it after every arm. A report
without that pre-run binding cannot be reviewed as a v2 measurement.

The operator prepares a public review packet and private random key from a complete
comparison. Candidate, source and reference identities are opaque and randomized;
packet order is randomized independently of model, arm, repeat and case order.
The packet includes original source statements, attribution, scope and original
event identity, plus only the already-frozen reference meanings. Model, arm, repeat,
case and split labels and the private key are withheld from the human reviewer.
Content itself may be recognizable; this is label blinding, not a claim that the
previously inspected corpus is unseen. Preparation also emits a blank decision
file. It never fills in judgments or calls a model.

The owner completes the decisions and explicitly attests to human review. Every
candidate receives exactly one `equivalent`, `not_equivalent` or `uncertain`
decision. Equivalence names exactly one reference from that candidate's packet
and requires exactly the same original-support set. An uncertain or negative
decision names no reference and receives no matching credit, including when its
text happens to equal a reference. The instructions forbid new facts, people,
counts, polarity, certainty, motives or causes absent from the permitted reference.
The local operator is responsible for authenticating the human reviewer; a typed
attestation is not a cryptographic proof of human authorship.

Scoring reconstructs the packet from the private key and exact observation bytes,
then validates the packet and decision bindings. Missing, duplicate, unknown,
foreign-reference or altered decisions/inputs fail closed. It counts one match per
reference per case/arm/repeat; every duplicate output remains in the precision
denominator. All existing labels remain in recall. It recomputes every other score
from unchanged observations and v1 rules, retaining failed cases, unsafe merges,
lost facts, answer regression, unknown usage and all numeric floors. A published
review assessment binds the observation, contract, packet and decisions by digest
and is explicitly non-activating. The current contract names the owner as reviewer;
delegation requires explicit human designation and a new pre-run contract binding.
The current corpus is marked previously inspected;
this assessment cannot claim a fresh holdout or replace exact-revision release and
upstream evidence. No reviewer data enters runtime or provider inputs.

The operator workflow is:

1. Run `agent eval memory-reconsolidation-comparison --control NEW_CONTROL --output NEW_REPORT --review-contract evals/capability/memory-reconsolidation-review.v2.json`.
   This records v1 scores as well as the pre-run review binding; a failing v1 score
   still returns nonzero and preserves the complete recording.
2. Run `agent eval memory-reconsolidation-review prepare --report NEW_REPORT --directory NEW_DIRECTORY`.
   Give the human `packet.json`, keeping `private-key.json` and the unblinded report
   separate. `decisions.json` is intentionally incomplete: no judgment is selected.
3. The owner sets `reviewer` to `owner`, completes every decision using the opaque
   candidate/reference IDs and sets `human_reviewed` to true only after review.
4. Run `agent eval memory-reconsolidation-review score --report NEW_REPORT --directory NEW_DIRECTORY --output NEW_ASSESSMENT`.
   Invalid review data writes no assessment. Valid review data writes the assessment
   even when quality fails, then exits nonzero for those failures. Outputs are
   exclusive; retry with a new output path rather than replacing evidence.

## Evaluation and acceptance

Freeze a corpus before tuning: duplicate and related-but-distinct claims,
cross-session connections, genuinely contradictory claims, stale evidence,
identical source copies, different people, negation/quantity/date differences,
cross-scope candidates, communication attribution, injection, corrections,
deletions, restarts, repeated sweeps and busy-store starvation. Maintain a
separate holdout, with reviewed expected facts, merge equivalence classes and
acceptable hypotheses. Gold labels are never provider input.

Compare the same seeded stores and recall budgets with (a) existing maintenance,
(b) equivalence merging only, and (c) merging plus summaries/connections. Use
the Milestone 16 deterministic harness and applicable Milestone 21 and People
floors without changing their frozen controls. Do not select an easier baseline
because the current production memory tuple differs from a historical artifact.

Release targets:

| Property | Gate |
| --- | --- |
| Isolation, erasure, owner corrections, no persona promotion | Zero violations in adversarial and concurrent tests |
| Equivalent merging | Zero false merges or lost atomic facts in the reviewed corpus and holdout |
| Useful consolidation | At least 80% of eligible duplicate sets collapsed; abstention is separately reported |
| Recall | No regression in direct-fact recall or applicable existing precision floors |
| New connections | At least 0.80 precision and 0.60 recall on separately labelled useful hypotheses |
| Downstream value | At least 10 percentage points better cross-session answer coverage at the same context budget |
| Evidence lifecycle | No artificial corroboration, evidence-time refresh, expiry extension or rejected-claim resurrection |
| Idempotency and races | Crash/retry and parallel workers produce one operation; stale or deleted inputs cannot commit |
| Coverage and load | A fixed 100,000-belief corpus is paged without starvation or an unbounded query/prompt; cost/time bounds hold |
| Retrieval and inspection | Summaries preserve specific-fact access, visibility, provenance and correction/undo behavior |

The scorer contract above fixes the denominators and matching rules. The frozen
synthetic development/holdout fixtures test the yardstick; they are not a live
provider score. Runtime collection, the three-arm comparison and exact-tuple
publication remain pending gates. Report every arm's cost, latency, coverage,
merge/abstention counts and failure counts, including failed runs. Token
reduction without retained facts or useful recall is not success.

Activation evidence must bind the reconsolidation policy, code, model and
reasoning effort, prompts, schema, scorer, corpus, underlying formation and
retrieval policies, and privacy configuration. Existing formation evidence
alone cannot activate this new behavior. Start disabled until matching evidence
passes; a policy pin and kill switch stop new jobs while retaining erasure,
history and valid original records. Production delivery follows the repository's
existing review and deployment gates after specific PR authorization.

## Implementation sequence

| Phase | Deliverable and exit condition |
| --- | --- |
| 1. Contracts and yardstick | Define operation/schema and source-revision semantics; register gates and reconcile the milestone bounds/census; freeze corpus, scorer and baseline references; add failing behavioral tests before implementation |
| 2. Durable maintenance | Both store adapters, migrations, leases/cursors, bounded inventory, reservations and audits; parity, fairness, recovery and cost gates pass |
| 3. Lossless consolidation | Equivalence membership, summaries and retrieval integration; no false merge, lost detail or owner-memory rewrite; lineage and undo checks pass |
| 4. Grounded connections | Bounded provider proposals and validation; independent-source hypotheses with unchanged trust and evidence clocks; connection quality gates pass |
| 5. Lifecycle and owner controls | Source-change invalidation, erasure races, review/deletion, operation history and safe undo across API/CLI/native client; boundary and client tests pass |
| 6. Evidence and release | Frozen holdout comparison on the intended deployment tuple; sidecar checks, PostgreSQL and native lanes; authorized PR review/CI and production verification |

Erasure fencing and provenance are built with the first persisted projection,
not retrofitted in Phase 5; that phase completes end-to-end owner journeys.
Phases 1–3 can proceed from verified existing interfaces while Milestone 21
finishes its own release evidence. Hypothesis activation cannot rely on a
withdrawn, failing or merely proposed upstream artifact.

## Hard gates

1. **Frozen evaluation inputs.** The development and holdout corpus, scorer implementation and unchanged M16 control references match the checked-in manifest; both splits cover all twelve categories and have disjoint case IDs.
   Registered as `gate.memory.recon_corpus_frozen`, structural. **M32.**
2. **False merges and lost facts are counted.** Overlapping merge groups are scored transitively; joining a distinct fact counts every false pair, loses duplicate-set coverage and reports any missing original.
   Registered as `gate.memory.recon_scorer_false_merges`, case. **M32.**
3. **Both stores obey the same contract.** In-memory and PostgreSQL adapters enforce owner-scoped leases, reservations, atomic operations, reverse dependencies, membership uniqueness, paging and undo with shared contracts and reversible migrations.
   Registered as `gate.memory.recon_repository_parity`, structural. **M32.**
4. **Durable recovery has one writer.** Crash, retry, lease expiry and parallel workers produce at most one committed operation per input; stale lease tokens cannot settle work or write derived memory.
   Registered as `gate.memory.recon_lease_recovery`, case. **M32.**
5. **Old memory is eventually inspected.** A 100000-record generation is visited within ceil(N/64) successful full-lane slices despite concurrent inserts and recent updates; bounded queues and separate inventory/model coverage prevent false completion.
   Registered as `gate.memory.recon_fair_inventory`, property. **M32.**
6. **Selection is bounded and scoped.** Each slice selects at most 128 anchors and four groups of at most 32 eligible original beliefs; filters precede limits, generated records are excluded and no all-pairs query or unbounded prompt occurs.
   Registered as `gate.memory.recon_bounded_groups`, case. **M32.**
7. **Budgets admit before spending.** Shared durable reservations enforce the 0.25 USD slice and 2 USD daily ceilings across workers, retries and midnight; unknown pricing defers and unknown settlement conservatively charges its reservation.
   Registered as `gate.memory.recon_budget_admission`, case. **M32.**
8. **Provider proposals cannot mutate authority.** Two bounded calls at most, closed schemas, supplied source IDs, local validation and candidate isolation reject malformed, unsupported or timed-out proposals without changing originals or blocking ordinary maintenance.
   Registered as `gate.memory.recon_provider_contract`, case. **M32.**
9. **Equivalent merges preserve original meaning.** Only the local equivalence key can authorize automatic membership; original rows and provenance survive, overlapping races cannot duplicate membership and differing person, scope, date, quantity or polarity never merge.
   Registered as `gate.memory.recon_exact_merge`, case. **M32.**
10. **Summaries preserve atomic facts.** Each summary clause is an exact supported extractive clause with complete lineage; omitted sources remain inspectable and independently retrievable, and no generalization is mislabeled as a summary.
   Registered as `gate.memory.recon_summary_fidelity`, case. **M32.**
11. **Connections remain grounded hypotheses.** A connection needs two distinct admitted original events, exact attributed support, local validation and successful verification; it is tentative at confidence at most 0.35 and cannot serve as fresh evidence.
   Registered as `gate.memory.recon_connection_grounding`, case. **M32.**
12. **Source changes immediately invalidate outputs.** A correction, retraction, expiry or attribution change invalidates dependent content atomically; reads and concurrent commits recheck revisions and fences before asynchronous cleanup.
   Registered as `gate.memory.recon_dependency_fence`, case. **M32.**
13. **Erasure closes every derived copy.** Memory, session, principal, email-exclusion and People erasure hide derived current/historical reads and in-flight output, then purge dependencies and generated copies without retaining deleted prose in audits.
   Registered as `gate.memory.recon_source_erasure`, case. **M32.**
14. **Undo is safe and durable.** An idempotent revision-checked undo removes active merge suppression, restores only valid originals and blocks repeating rejected pairs across retries and policy changes.
   Registered as `gate.memory.recon_undo_durable`, case. **M32.**
15. **Dreaming never creates evidence.** Merging, summarizing, citing and repeated passes cannot raise confidence, corroboration or evidence time; hypothesis expiry respects evidence plus thirty days and all supporting validity limits.
   Registered as `gate.memory.recon_evidence_clock`, case. **M32.**
16. **Visibility and trust cannot widen.** Owner isolation, provider sensitivity, source attribution, portability, secrets, injection and prohibited inference checks hold throughout selection, commit and rendering; persona and People identity rules are unchanged.
   Registered as `gate.memory.recon_trust_boundary`, case. **M32.**
17. **Recall preserves specificity and visibility.** Equivalent suppression falls back to permitted live originals; derived items obey the same budget/visibility checks, cannot displace needed atoms, and disabled processing restores original recall.
   Registered as `gate.memory.recon_recall_visibility`, case. **M32.**
18. **Frozen sessions see correct invalidation.** Existing prefixes stay byte-stable while next-turn deltas report undo and invalidation; erased generated copies cannot enter model input, and ambiguous citations neither reinforce nor refresh evidence.
   Registered as `gate.memory.recon_snapshot_delta`, case. **M32.**
19. **Owner controls have explicit boundaries.** New list/detail/undo and compatible memory projections enforce scopes, sensitivity-filtered cursors, principal isolation, validation, revisions, idempotency and honest unavailable-server behavior.
   Registered as `gate.memory.recon_scoped_surfaces`, case. **M32.**
20. **The owner can inspect and undo.** Native journeys show original versus derived memory, source links and operation history; review/delete and previewed undo handle success, retries, conflict, hidden support and processing-disabled history.
   Registered as `gate.memory.recon_native_journeys`, case. **M32.**
21. **Three-arm evidence demonstrates useful recall.** Three complete repeats per split compare the original, merge-only and connections arms at equal budgets: zero false merges/lost facts/boundary violations, duplicate coverage at least 0.80, hypothesis precision at least 0.80 and recall at least 0.60, and cross-session answer coverage lift at least 0.10 with existing recall floors unchanged.
   Registered as `gate.memory.recon_comparative_quality`, case. **M32.**
22. **Only matching evidence activates.** Publication recomputes complete observations and gates; startup refuses missing, withdrawn or mismatched policy/code/model/effort/prompt/schema/scorer/corpus/dependency evidence, and the kill switch leaves erasure and history operational.
   Registered as `gate.memory.recon_activation_bound`, property. **M32.**
23. **Existing memory and People behavior survives.** Original memories, controls, provider pins, evidence clocks, People merge/split and erasure, old clients and processing-disabled deployments preserve their established contracts without silent policy substitution.
   Registered as `gate.memory.recon_compatibility`, case. **M32.**
24. **Release is verified on its actual revision.** Sidecar checks, PostgreSQL and native lanes, authorized PR review and final-head hosted CI pass; production delivery and content-free selection/operation readback verify the intended revision and valid evidence before completion.
   Registered as `gate.memory.recon_release_evidence`, case. **M32.**

## Implementation status

Phase 1 establishes this design, the gate registry and a deterministic evaluation
yardstick. Phase 2 now has the maintenance foundation: a unit-of-work repository
in both adapters, source content revisions and immutable creation sequences,
owner-serialized leases, two bounded inventory lanes, queued groups, spend
reservations and content-free checkpoint receipts. Four additive migrations
backfill the metadata without changing original payloads, evidence clocks or
recall positions. The inventory index builds concurrently; its downgrade is
transactional so an earlier data-retention refusal also restores the index.

`ReconsolidationInventoryPass` queues at most four groups and renews its lease
while running. The maintenance worker has a separately awaited, cancellable
120-second background slot that yields admission while runs hold live leases.
Production composition now supplies the bounded processing callback only when
explicitly enabled with matching release evidence. The default remains disabled.
Eighteen shared maintenance scenarios run against both adapters; further checks
exercise 100000-record coverage, PostgreSQL concurrent admission, rollback and
late commits, migration preservation, and independent maintenance sweeps.

Original memory and reconsolidation now share an in-memory transaction journal.
Failed or cancelled units of work restore records, source history and revisions,
erasure fences, queued groups, checkpoints and reservations together. Journal
entries retain only touched values; a slice does not copy the full memory bank.
Locks are acquired on first access to an owner, not on unit-of-work entry;
other owners and unrelated email reads continue independently. Source cleanup
journals only changed histories, so its rollback preserves another owner's
concurrently committed memory history. Task ownership
prevents a spawned task from inheriting its parent's write access. Nested memory
units of work act as savepoints. People and erasure paths acquire the same owner
memory guard before their other locks. Global recall positions, like database
sequence allocations, may leave gaps on rollback; source revisions and creation
sequences remain transactional. This does not add rollback to
unrelated in-memory repositories; PostgreSQL retains its complete database unit
of work. Changed anchors precede full-scan anchors, and checkpoint admission
recomputes the bounded page so a caller cannot skip either inventory lane.

The shared contracts now include operations, dependencies, undo, source-only
provider prepricing and idempotent application. Complete integrated gate evidence
remains required; individual passing components do not establish release readiness. Runtime gates
stay pending until a behavioral test observes each complete assertion;
foundational checks do not weaken those assertions or permit activation.

The first Phase 3 component is a pure local merge planner and transition policy
in `domain/reconsolidation_merge.py`. It accepts two to thirty-two authenticated
original snapshots with exact expected revisions and complete resolved speaker/
People attribution; it has no provider, recall or public-surface entry point.
Only NFC and whitespace normalization authorize equivalent text.
It preserves original records and evidence clocks, chooses the oldest immutable
creation key, and emits content-free dependency and pair-block values. Ambiguous
attribution, review/conflict flags, erasure/rejection decisions, occupied members,
incompatible trust/validity fields and existing content hazards refuse a plan.

The local invalidation transition rechecks every dependency, including attribution,
content digests and elapsed expiry; usage-only changes leave it unchanged. The
undo transition checks owner, revision, operation state and idempotency receipts,
returns only still-valid original IDs, and re-filters them on replay. Its block
signatures cover both member-ID pairs and claim/original-event pairs, independent
of subject labels and policy versions, so copying the same evidence under new
belief IDs cannot evade undo. These values retain no source prose.

Both repositories now expose `plan_merge(principal, token, group_id, now)` for
read-only planning of a currently claimed group. The adapter checks the lease,
120-second slice deadline, owner, current original revisions and memory fences
before reading retained evidence under the memory-owner and People locks. The
PostgreSQL People lock takes the memory-owner lock first, matching the in-memory
order. Source resolution admits retained authenticated owner messages only;
tool output, assistant prose, schedules, external input and unknown speakers
abstain. Every required leaf must exist; a plan inspects at most 256 distinct
leaves and 64 People source/mention/link heads per atom, refusing overflow rather
than truncating support. This initial planner accepts text-only owner messages.

Attribution reads include hidden matching heads so privacy filtering cannot turn
an erased identity into apparent absence of attribution. Current identity,
source identity, exact source support, sensitivity and unresolved/merged status
are checked again. People heads retain owner-bound opaque belief/source/event
keys when their revision payloads are purged. A purged matching head still
withholds its affected plan; unrelated purged heads no longer disable an owner's
bank. Migration `ea3210a1b005` backfills keys from each available current revision
without retaining names, statements, roles or evidence excerpts. A legacy head
whose payload was already purged keeps unknown metadata, and planning still
abstains for that owner rather than guessing absent attribution. Original memories
remain available under their existing rules. Outstanding rejection text uses the formation
duplicate resolver's casefold/whitespace comparison, and deleted prose remains
blocked by its existing hash; changing a belief ID or subject does not bypass it.

Planning neither persists a merge nor suppresses recall, and a returned plan is
not a commit authorization. Both adapters now advance affected original content
revisions in the same transaction as People source, mention, memory-link and
identity changes, including source-copy rebasing and erasure. Link reassignment
fences both old and new originals; principal-wide People erasure fences the
owner's original bank before separate original cleanup. These metadata revisions
preserve original payloads, evidence clocks and insertion order. Direct in-memory
People writes take the same owner guard, and People records, opaque keys, reverse
indexes and revisions participate in the existing rollback journal. Purging a
payload removes its current dependency edges while keeping its opaque fence.

The attribution migration preserves original evidence and refuses downgrade while
work is leased. It takes the owner table lock before People heads, matching writer
order, and reports the loss of attribution history on downgrade. Upgrade and
downgrade require an unfiltered database role, failing rather than skipping
RLS-hidden source heads or leased jobs. Shared contracts
cover revision isolation, link reassignment, mention roles, rebasing, repeated
purge, rollback, original-event index changes and concurrent writes. Migration
checks cover opaque backfill, unknown legacy heads and unchanged original history.
PostgreSQL binds large source/belief sets as arrays and pairs session/event arrays
positionally, so bulk erasure cannot exceed the query-parameter limit or combine
an event sequence with the wrong session.

Both repositories now expose an internal deterministic merge commit/undo boundary.
`commit_merge` resolves the complete plan again under the owner and People locks,
checks its expected identity, active membership and durable pair blocks, and
atomically persists a `StoredMerge`, original dependencies, membership, an opaque
audit revision and the group outcome. The metadata retains kind, group/job,
input/policy/model/evidence identities, transition instants, reason, revision and
a position from the existing memory allocator. Original rows are unchanged.
At most four group claims, including retries, and eight operations are admitted
per slice; the durable counters reset only when a new slice is claimed.

Original content changes, attribution changes and erasure synchronously invalidate
active memberships in the source mutation's transaction. An internal membership
read also checks current evidence, rejections, fences and elapsed expiry before
returning IDs. Undo checks the owner, committed state, expected revision and
idempotency receipt, removes membership and installs hash-only pair blocks in the
same transaction. Replay survives erased originals and never restores their rows.
The internal metadata methods return identities and hashes only; the public,
ceiling-filtered operation view and paginated history surface are described below.

Migration `ea3210a1b006` adds operation, dependency, membership, block, receipt and
history tables with tenant RLS, composite owner/operation foreign keys, source and
history-order indexes, and partial active-member uniqueness. Dependencies retain
opaque original IDs after erasure; they do not require an erased original row to
survive. Downgrade refuses leased work and retained merge history, requiring an
explicit export/removal decision before destroying derived history. It leaves
original memories and existing rejection/erasure records intact.

Shared contracts cover commit/undo persistence, rollback, changed support,
usage-only updates, expiry, source-erasure races, concurrent commits, overlapping
membership, copied-evidence undo blocks, replay, isolation and slice claim limits.
PostgreSQL checks inspect mutation state before any operation read, enforce the
foreign-owner and active-member constraints and exercise every new table under a
non-bypass RLS role. Subsequent components below add recall, provider-derived
operations and public surfaces. No complete runtime gate is bound by these
foundational checks and production remains inactive pending release evidence.


The next Phase 3 foundation connects deterministic membership to opt-in **live**
recall (`retrieval-reconsolidation@1`). `active_merges` accepts at most 1000 original
candidate IDs per read and revalidates complete support under the owner and People
locks. The retriever fetches an eligible canonical even beyond the initial candidate
cap, preserves sensitivity, scope, identity and persona exclusions, and falls back
to the oldest permitted member. It then uses the existing ranking, durable-slot
reservation and token/item budgets. Original records and citation IDs remain intact;
the trace adds only operation ID and revision to the rendered original's metadata.
The in-memory People lock now permits reentry by its owning task, matching PostgreSQL
transactional lock behavior; a child task cannot inherit that ownership.

Memory head positions include owner-scoped merge transitions in both adapters.
Opt-in delta queries admit original members affected by later membership transitions
without rewriting those originals' positions or evidence. Snapshot traces remain
unchanged. Their undo/invalidation corrections describe a changed equivalence
grouping, not a false claim that an unchanged original belief stopped holding.
Frozen dependencies are revalidated before the delta even when the clock advances
without a source write. These corrections occupy the existing non-yielding Region B
slot and survive recall-budget pressure. Citation feedback continues to update only
the rendered original's utility/usage fields and refuses ambiguous short IDs.

Shared recall scenarios exercise canonical choice despite a higher-utility duplicate,
canonical lookup beyond the candidate cap, visibility fallback, disabled recall,
owner isolation, changed-support deltas, hidden/erased originals, expiry, undo and
unchanged trace bytes. Context tests exercise correction ordering and budget pressure;
usage tests check unchanged evidence clocks, strength, original positions and hidden
members. No public flag enables the opt-in constructor seam in production.

The following Phase 3 foundation adds read-only historical membership through
`merges_at`, bounded to 1000 candidate IDs. `as_of` selects the effective membership
interval and source validity; `known_at` limits recorded operation transitions and
source/People revisions. An absent knowledge cutoff uses current source knowledge.
Commit is inclusive; undo/invalidation is exclusive at its transition instant when
that transition was already known. A historical projection returns the original
committed operation revision, never rewrites current membership or allocates a new
memory position, and cannot admit an operation committed after either cutoff.

Both adapters compare every dependency's exact content revision and immutable
creation identity. In-memory version history journals attribution-only changes as
well as original edits; PostgreSQL reads its existing indexed memory revisions and
operation history through the retained reverse-dependency index. Same-instant writes
select the latest recorded revision, including a correction followed by restoration
of identical prose. Retained original events, current rejections, source suppression,
People visibility and attribution are checked again. Historical attribution uses
past assignments only while current heads still certify their visibility; unavailable
or moved-away assignments conservatively withhold grouping. This is not an arbitrary
historical identity reconstruction service.

The retriever checks the complete support against today's sensitivity and local-scope
restrictions before grouping, including dependencies outside the returned candidate
set. Original persona exclusions affect rendering only; a permitted noncanonical
member can still represent the group. The canonical lookup, ranking and budgets use
the same historical query. Shared adapter scenarios exercise both clocks, exact commit
and undo boundaries, corrected originals, unchanged current audit/head state, expiry,
same-instant source revisions, scope/sensitivity tightening, identity correction,
People fencing/purge, owner isolation, bounded lookup and disabled behavior. Candidate-
cap coverage includes historical canonical lookup. Derived projection/erasure, public
surfaces, provider synthesis and comparative activation evidence remain open; these
foundations do not complete the full recall or snapshot runtime gates.

The next Phase 3 foundation adds internal extractive summary planning, commit and
current reads in both repositories. A prepared summary contains at most four exact
NFC/whitespace-normalized source statements. Its fixed heading and bullet formatting
count toward the 1,024-character bound; whole clauses that do not fit are omitted,
with every omitted original explicitly identified. All input versions, source
sessions/events and attribution hashes remain dependencies, including omitted inputs.
Original atoms retain their own identities, contents and retrieval behavior. Summaries
never enter the original inventory or equivalence membership tables.

`SummaryMemory` stores the erasable clauses, original belief types, strictest
sensitivity and portability, weakest authority and confidence, original evidence
clock, latest validity start and earliest required-support end. Its UUID is its
operation UUID; it has a projection revision and memory-allocator
position. `StoredSummary` retains only source identities, digests, transition metadata
and audit revisions. This internal deterministic-extractive path does not claim a
provider generated or verified the clauses. Commit rechecks the claimed group, lease,
slice budget, exact revisions and current authenticated evidence under the existing
owner and People locks, then persists the operation, dependencies, projection and
group outcome atomically.

Source invalidation now visits every committed reverse dependency, so an original
can invalidate multiple summaries as well as a merge. Original edits, source fences
and attribution changes remove the current summary projection in the same transaction
as the invalidation revision. Internal reads additionally revalidate all support and
elapsed expiry, purge invalid content and enforce the requested sensitivity ceiling
and local scope; they grant no subject override. Rejections, copied evidence, original
source trust and current People visibility use the same admission checks as merges.
Usage-only updates preserve the summary and evidence clock. Opaque history contains
no summary text, and rollback restores both projection and invalidation state.

Migration `ea3210a1b007` adds the separate `reconsolidation_summaries` projection table
with forced tenant RLS and an owner-bound operation foreign key. Downgrade serializes
with owner/job writers and refuses leased work or retained summary history before
removing the projection table. It requires an explicit history export/removal decision
and leaves original memories, erasure fences and rejection records intact.

Shared summary contracts cover exact clauses and complete lineage, preservation of
originals, omitted-source invalidation, multiple dependents, People changes, rollback,
expiry, owner/scope/ceiling checks, stale inputs and leases, and fabricated previews.
PostgreSQL checks also inspect physical text removal before any projection read,
concurrent commit uniqueness, erasure/commit serialization, tenant RLS and downgrade
refusal. These internal foundations do not advance a complete runtime gate or
enable production processing.

The next integration exposes these summaries through opt-in current ordinary recall
under `retrieval-reconsolidation@2`. The original ranking, durable reservations,
People allocation and item/token ceilings run first. Summaries use only remaining
capacity and are omitted if any rendered clause repeats a selected atom or another
selected summary. Candidate lookup is owner-bound and capped at 1,000 operations,
anchored to matched original IDs or a newer operation position for recall deltas.
Original scoring uses the configured retrieval profile. The source-complete historical
and People-scoped extension is described below.

`RecalledSummary` distinguishes derived output from original beliefs and carries its
operation revision, all original support IDs, stable leaf-event source IDs and source
session IDs, including omitted support. It never fabricates a single episode link.
Trace views and CLI inspection revalidate every dependency against the applicable
ceiling and original scope before exposing text; custom compositions without
validation withhold summaries. Inspection clears rendered bytes containing an
unavailable summary while retaining the internal trace for next-turn corrections.
The existing generated-copy graph follows this complete lineage into snapshots,
cached plans and affected runs. Deleting an omitted original therefore fences the
whole summary and its copied influence. Email exclusion also removes the entire
dependent trace item, rather than editing individual clauses.

Frozen prefix bytes remain unchanged on correction or expiry. Next-turn corrections
mark an unavailable summary by its citation, including when the processing switch
has been disabled or expiry occurs without a source write. The retriever requests
dependency validation in either switch state. Faithful, unambiguous summary citations
update only the derived projection's bounded utility and last-used time. They do not
change operation revisions, store positions, original evidence or support beliefs.
Owner locking serializes completion deduplication with source invalidation; both
adapters roll usage back with the surrounding transaction. The legacy atomic-memory
benchmark remains byte-for-byte frozen. Its atomic-only reader sees a non-atomic
`summary` type tag, so it cannot count a summary as an independently recalled original.

Shared adapter tests cover remaining budgets, duplicate clauses, source correction,
expiry without writes, disabled recall, sensitivity/exclusion filters, trace identity,
omitted-support erasure, faithful and ambiguous citations, and usage rollback. The
full compositions also verify cached-plan sanitization, epoch rotation, cancellation
of affected runs and refusal to append a stale summary snapshot after source deletion.
The historical extension reconstructs exact clauses from permitted original revisions
and verifies every clause and dependency digest against the first operation revision.
Both effective and recorded cutoffs must admit the operation; source content revisions
must match exactly, even when later edits restore identical text. Reconstruction uses
the requested effective time for lifecycle and expiry. Current sensitivity, local-scope
restrictions, source retention, rejection and People erasure still apply to all inputs,
including omitted clauses. Historical reads neither restore the current projection nor
change operation state, revision, usage or the memory watermark.

People-scoped summary recall applies the same bounded hard identity filter as atomic
recall to every supporting original. Every input must satisfy the requested focal set
and viewing ceiling, including unrendered inputs; historical queries use the recorded
identity assignments and retain current privacy checks. Trace views and CLI inspection
reconstruct historical summaries with the original query clocks. For a `known_at`-only
query, the trace stores the effective instant used for scoring and summary admission;
later trace creation or inspection cannot move that instant across source expiry.

Shared adapter contracts cover correction after projection purge, omitted-source
privacy tightening and erasure, expiry and exact temporal boundaries, same-instant
source versions, read-only audit/watermark behavior, People intersection and historical
reassignment. Owner operation inspection/undo and derived-memory controls are
implemented below, followed by provider selection and hypotheses. Comparative
activation evidence remains open. Production processing remains disabled.

The owner-control foundation adds `PublicReconsolidationService`, typed operation
views and bounded operation paging to both storage adapters. It validates current
complete support under the owner and People locks before releasing content; raw
operation plans, evidence digests and internal counters never enter the public view.
HTTP mounts only the three specified routes when both surface flags are enabled.
The CLI shares the same visibility, pagination and durable revision/idempotency
rules. Neither entry point performs provider work or enables maintenance/recall.

Shared adapter scenarios cover owner/tenant isolation, ceilings, complete summary
support including omitted clauses, source erasure, undo receipts, wrong-kind and
stale/reused requests, cursor binding and hidden-row pagination, elapsed expiry
state filtering after transaction/owner waits, and simultaneous write-scoped undo
retries. HTTP boundary tests
cover route/scope census, authentication, field/body validation, private responses,
disabled flags, unchanged M17 validation and redacted undo replay. CLI tests cover
list/detail, undo/retry/conflict and rejected requests without mutation. Behavioral
reds first reproduced absent operation reads/undo, missing HTTP/CLI routes and
expiry omitted by premature state filtering or a clock sampled before the owner
guard. Native coverage is recorded below; these controls alone do not complete
the surface gates or provide activation evidence.


Derived owner controls now extend the existing memory API with opt-in browsing,
complete-source detail and governed dismiss/localize/reject/delete actions. The
ordinary default and projection stay unchanged. Both adapters persist review and
current local-scope controls separately from the immutable evidence payload;
historical source invalidation preserves the original operation revision, while
current owner restrictions remain effective. Summary suppression uses opaque
claim/attribution and evidence hashes to fence existing copies and prevent renamed,
reordered or case-varied copies from being committed. Rejection returns a retired
content-free view; deletion hides detail and preserves a content-free retry receipt.
Migration `ea3210a1b008` adds owner-bound, forced-RLS derived write receipts and
refuses a downgrade that would abandon controls, suppression or leased work,
including unreviewed summary metadata that the older application cannot read.

Shared contracts exercise original preservation, filters and pagination, omitted
support, visibility, historical restrictions, expiry after owner-lock waits,
usage updates after localization, idempotency across original/derived writes,
concurrent retries, stale prepared commits and transaction rollback. Full-context
cases verify erasure of snapshots and traces, run cancellation, durable cleanup,
epoch rotation, stale-snapshot refusal and restoration after an aborted erasure.
The in-memory generated-copy path journals touched values, leaving independent
original events intact on rollback. HTTP boundaries cover validation, authentication,
scopes, ceilings, foreign owners, disabled flags, receipt-storage failure and
redacted replays after source erasure. Provider synthesis and hypotheses follow
below; comparative activation evidence remains required.

The native Synthesis journal now implements kind/state history, separate original
and derived labels, per-claim source disclosure, current original reads, summary
review/delete and previewed merge undo. Its page and detail readers discard stale
responses, refuse mismatched identities and clear private content after connection
changes, backgrounding or authorization loss. An uncertain write keeps only its
key, target and revision for explicit retry. Error messages never reflect raw
server content, and unavailable history never reconstructs titles or source counts.

Native behavioral reds first observed the missing journal and undo eligibility,
then missing writes, retained pages after authorization loss, mismatched detail
identity and a wrongly retryable unsupported delete. The full Swift package passes
655 tests. Seven Synthesis journeys pass on each iPhone/iPad simulator, including
the connection uncertainty and evidence journey; the affected review/delete and
connection cases pass again after the final action-label update. UI assertions exposed below-screen action feedback;
the fixed feedback stays visible, with its accessibility identity separate from the
scrolling list. The macOS build passes, but its latest UI runner failed before
executing tests because system authentication was already running. macOS journey verification and the complete
native/release gates remain pending; no production flag or verified ceiling changes.


### Grounded writers and composed maintenance

Reviewed `infer_connection` operations use the existing erasable derived projection
with kind `hypothesis`; they do not create an original-memory row. One tentative
clause must cite all selected support, with two distinct authenticated original
events and identical resolved attribution. Local checks reject new names, quantities,
negation, prohibited traits, missing/future/stale evidence, invalid sources and
previously rejected claims. Normal owner/user phrasing and possessives do not
change the attribution; uncertainty may occur later in the clause. Validation
uses a separate owner alias and preserves the verified clause wording in storage. The source-only
verifier remains required. It assesses support for a tentative relationship without
requiring the owner to have stated that relationship, while refusing invented
motives, causation, names, quantities or facts. Authority is
inferred, confidence is at most 0.35, and expiry is bounded by original-event time
plus thirty days and every supporting validity limit. Usage does not alter those
clocks. A related query may match one supporting atom; every source still must pass
scope, People, sensitivity and lifecycle checks. Rendering explicitly labels the
connection as a hypothesis. Owner review/delete reuse the existing atomic controls.

Reviewed `flag_conflict` operations retain complete versioned dependencies and a
fixed uncertainty notice. They neither suppress originals nor add a recall item,
choose a winner or change either source's truth. Both kinds count against the
existing eight-operation budget. Migration `ea3210a1b00b` marks their payload
capability without another table; downgrade refuses retained unreadable history.

`ReconsolidationPass` extends the inventory pass with one proposal/verifier batch
and the existing group application boundary. It claims at most four groups and
releases the lease after completing or deferring them. The maintenance worker's
existing cancellable background slot owns it. No separate scheduler is added.
`AGENT_MEMORY_RECONSOLIDATION_ENABLED` defaults to false, independently of inspection
flags. Enabling it requires `AGENT_MEMORY_RECONSOLIDATION_EVIDENCE`, a deployed
revision, a matching admitted formation artifact and an explicit
`AGENT_MEMORY_RECONSOLIDATION_RESIDENCY_PROVIDER` pin. The local egress policy
examines each full memory, raw excerpt and proposed clause independently, preserves
its classification floor and refuses sensitive patterns, secrets, injection and
unknown residency. It is a conservative deterministic classifier, not new source
admission or a provider-based privacy decision.

Startup binds source/configuration bytes, corpus/scorer, model configuration,
privacy policy, release revision and upstream formation/retrieval identities.
The admission predicate rechecks the certificate bytes at each processing/recall
boundary; removing or replacing the certificate revokes admission. Historical
inspection and erasure remain available. Production activation still requires the
complete gates; constructing or hand-editing a certificate is not measured evidence.

### Measured comparison runner

`agent eval memory-reconsolidation-comparison --control NEW_PATH --output NEW_PATH`
uses the configured formation model at provider-default reasoning effort. It first
records three complete original-only answer runs into an exclusive new file, then
runs the merge-only and connection-enabled arms on fresh stores. Runtime inputs
contain only synthetic seeds and questions. The unchanged scorer alone reads labels.
Operations, survivors and persisted recall traces are collected from the stores;
answers use the same model, query, 2,000-token/twenty-item context and fixed clock.
No failed case leaves the denominator. Reports include per-split integer counts,
quality failures, bounded usage/cost and trace hashes. The total spend ceiling is
at most USD 50, configurable downward; uncertain calls retain their full charge.

The report binds the exact implementation digest and refuses source changes during
the run. The frozen retrieval-only control and M16 files remain untouched. Release
publication recomputes the complete report, refuses missing/unmeasured rows,
unknown usage, dirty code or altered baseline bytes, and validates the matching
upstream formation artifact plus a current Milestone 16 recall-floor artifact.
The recall artifact must match the same build, model, formation/retrieval policies
and frozen corpus. This runner does not activate processing. Comparative
quality, unchanged upstream recall floors and exact-revision release evidence are
required separately; a failed measured run remains useful failure evidence only.

### Measured failure evidence (2026-10-08)

Two separate original-answer controls and three-arm reports are retained in
`evals/observations/memory-reconsolidation/`. Each report contains all 216 case
attempts (24 cases, three repeats, three arms) and the digests of its exact source,
model configuration, corpus/scorer, privacy policy and original-answer control.
Neither is release evidence. The initial run had zero committed connections and
one failed answer with unknown usage. After correcting the conservative writer
and source-only verifier instructions, the second run completed all attempts with
known usage, 408 calls, USD 3.44078625 cost and 879.67 seconds wall time.

| Second comparison | Original answers | Merge answers | Connection answers | Hypothesis exact matches |
| --- | --- | --- | --- | --- |
| Development | 13/36 | 11/36 | 14/36 | 0/7 outputs, 6 labels |
| Holdout | 15/36 | 14/36 | 16/36 | 0/8 outputs, 6 labels |

Both splits preserved all facts, made zero false merges and covered all duplicate
sets. Both failed hypothesis quality, required answer lift and merge-answer
nonregression. The original-answer control SHA-256 is
`96ed1ad0ffdc80ba038d0acb4cd934e2bafbf2efbefe78b8e5b3e263e1f1e1cb`;
the second comparison SHA-256 is
`c88feff1311b184c91c821e8dff32b2d675e876acc0a89a6277b39940965a4e1`.
The recordings predate the final repair that preserves verified owner phrasing;
no passing measurement is claimed for that repair. The owner subsequently approved
ADR-0170 on 2026-10-08. Its separately versioned offline equivalence review preserves
these failed recordings and does not resolve the answer-coverage failures.
Production remains disabled and complete runtime/release gates stay pending.

### Approved-review measurement (2026-10-08)

The subsequent `20261008-review-v2-control.json` and
`20261008-review-v2-comparison.json` preserve a new run bound to the frozen
ADR-0170 contract before any provider call. The implementation digest matches the
code that completed the run. All 216 cases completed with known usage: 408 calls,
USD 3.44656125 and 823.52 seconds wall time. The earlier files were not rewritten.
The control SHA-256 is
`33575659e226bacf2bc8b7c2803dd6ff94ea9f75f16c19b0aa71b78b257b3cf0`;
the comparison SHA-256 is
`bf8be19f65be2ac2a3a6481a81c955f284c7a1166e316885869202548a89830f`.

All facts survived, false merges remained zero and duplicate coverage was complete.
Development answer coverage was 17/13/12 of 36 for original/merge/connections;
holdout was 17/17/14. Required answer lift therefore still fails on both splits,
and development merge answers regress. The v1 exact scorer credits none of the
fifteen hypothesis outputs. The owner subsequently completed the blinded packet:
ten equivalent and five not equivalent. The unchanged scorer credits six of eight
development outputs (75% precision, 100% recall) and four of seven holdout outputs
(57.14% precision, 66.67% recall). Both precision scores fail the 80% floor; both
recall scores pass 60%. Scoring completed in 1.30 seconds and exited 1 for quality
failures, not invalid review input. The packet, reconstruction key, exact owner
decisions and assessment are preserved in
`evals/observations/memory-reconsolidation/20261008-owner-review-v2/`. The packet
SHA-256 is `a8e565d10c2005fe7c7b5f801ab563e81775bfbc3d9a0fdea86e564c8e025fb0`;
the decision SHA-256 is
`4d4d81318fe3cda835852b0f3caf49c028fad4e9ac135eefb4ab7f07d4a04aa9`.
Human judgments do not remove the independent answer failures. The inspected corpus
remains marked previously inspected, and this dirty-tree measurement is not
activation evidence. A separate review of real owner memories assesses usefulness;
it supplies no new benchmark labels and cannot satisfy or waive these gates.

### Quality repair diagnosis (2026-10-09)

The owner authorized integration with current dev and renumbering the unpublished
M32 ADRs: 0148 → 0169, 0149 → 0170, and 0150 → 0171. Frozen manifests,
scorers, observations and human review artifacts retain their original bytes and
identifiers. The comparison below predates this integration and is diagnostic,
not evidence for the integrated release tuple.


The development observations separate answer formatting from recall loss. Of the
four fewer exact matches in the reviewed merge arm, three occurred in cases with
no committed merge (quantity and corrected residence); the remaining difference
was a full-sentence response for the duplicated desk fact. The scorer correctly
keeps these failures. A fabricated single-fact regression confirms byte-identical
answer requests across all three arms when maintenance has no operation. The
answer prompt, exact-answer aliases, scorer, scope and budgets remain unchanged.
The attributed-email and project-scope probes request information unavailable
under the frozen general-scope control; privacy filters must not be broadened to
answer them. The holdout was not inspected for this repair or used for tuning.

A separate pressure regression exposed a real merge-recall bug: choosing the
oldest canonical discarded a more useful visible member's ranking score, allowing
a competing fact to displace the equivalent claim. Recall now retains the best
visible member's score and retrieval arms while preserving canonical citation
identity and leaving every original's utility, evidence and clocks unchanged.
This repairs a general defect; it does not establish the cause of every recorded
answer-score difference or waive the answer nonregression and lift gates.

Connection admission now rejects literal source copies disguised by a hedge and
identical claims repeated across independent events. Proposal and verifier
instructions distinguish related-fact summaries from additional tentative
relationships; a conjunction or project requirement recast as an owner preference
is insufficient. These bounded checks supplement the source-only verifier; they
are not a general semantic-equivalence detector. Fabricated regressions first
failed at five behavior assertions, then passed. Existing source, attribution,
uncertainty, expiry and privacy contracts remain in force.

The `20261009-quality-control.json` and `20261009-quality-comparison.json`
record one further pre-bound comparison, with a USD 10 run ceiling. All 216
attempts completed: 408 provider calls, USD 3.53385250, zero unknown-usage calls
and 862.20 seconds wall time. Its source digest is
`fbc3999e6ed6ec65cda651dc91bed71581687a3429ee547dc59de5e11c9d6926`.
The scorer, answer prompt, corpus, controls and prior decisions are unchanged.

| Split | Prior original/merge/connections | New original/merge/connections | New answer lift |
| --- | --- | --- | --- |
| Development | 17/13/12 of 36 | 14/18/14 of 36 | 0 percentage points; fails 10-point floor |
| Holdout | 17/17/14 of 36 | 17/17/19 of 36 | 5.56 percentage points; fails 10-point floor |

Both new merge arms pass answer nonregression. The comparison retains zero false
merges, zero lost facts and complete duplicate-set coverage. New hypothesis counts
are six development and five holdout outputs (previously eight and seven). None
matches the v1 wording exactly; revised human precision/recall is **pending**, not
inferred from fewer outputs or copied from the previous judgments. The new blinded
packet and blank decisions are under `20261009-quality-review-v2/`; all eleven
candidates require a new bound owner review. The October 8 review stays complete.
Neither changing counts nor one nonregressing run establishes causal answer lift.

PostgreSQL verification also exposed an optional-read contract error:
`get_summary` propagated the tenant guard's `NotFoundError` instead of returning
absence. It now returns `None` without attempting a foreign-tenant write or
aborting the caller's transaction. The existing shared owner/scope test reproduced
the failure with RLS bypass disabled. This reader repair followed the completed
comparison and is not included in that recording's source digest. The measurement
therefore remains diagnostic, dirty-tree evidence; it cannot activate the final
source tuple. Final release verification must measure the clean integrated tuple.

### Owner-confirmed isolated experiment (ADR-0171)

The owner approved ADR-0171 on 2026-10-08. Its separate fixture path does not
change production source admission, the provider-free dry run or release gates.
`owner-memory-fixture@1` carries two to sixteen current, nonexcluded statements,
their original scope/classification floors and optional retained source context.
The local page shows the exact statements and selected provider/model before
anything is sent. Source context stays local; the confirmed statement is the new
input, not a substitute for an old authenticated message.

Each **Use this statement** gesture records one fresh input confirmation with its
actual timestamp, original reference and complete packet digest. Repeated gestures
retain the first timestamp. Identical statements share one evaluation event so
copies never add corroboration. The owner may choose a subset: only confirmed
statements enter the disposable store and provider requests. The final **Run**
gesture requires at least two distinct confirmed inputs, the unchanged model and
packet, and an explicit submission timestamp no more than thirty minutes old.
The submission binds the exact selected references and packet; it is recorded
when Run is clicked, independently of preparation and selection times. Waiting
does not expire the review or renew the original per-input evidence timestamps.
Changing
text/model, missing consent, known unavailable/excluded inputs, prohibited egress
or oversized complete statements refuse before provider admission. These new
confirmations are evaluation evidence only, not repaired historical attribution.

Each statement has an editable draft and an explicit **Save correction** action.
Saving changes only that draft, retains its original references, scope and
classification, and clears the packet's prior confirmations. The new exact text
must be confirmed before sending. Unsaved edits disable Run; stale or concurrent
corrections cannot overwrite a newer packet, and started experiments cannot be
edited. Neither selecting nor correcting input writes to the production bank.

Before Run, a provider-free compatibility preview uses the same scope and lexical
matching rules as the disposable inventory. It identifies comparable statement
pairs, shows each stored context and explains selections with no related pair.
Unselected statements cannot make a selection runnable. Owners may remove a
confirmation while the experiment is pending; this neither edits text nor confirms
another input. Corrections recompute compatibility and still clear confirmations.
Both the Run route and the evaluator refuse selections without a related pair
before reserving budget or constructing evaluation evidence. This preview is not
a promise of an accepted output: admission, proposal and verification still run.
Results distinguish zero-call grouping/admission stops, model abstention, verifier
rejection, local validation failure and provider errors using observed receipts.
An unknown reason stays unknown rather than being described as a successful run.

The standalone launcher supplies only in-memory repositories, a clock and an ID
factory. The evaluator creates a fresh experiment principal and new belief/session
identities. It reuses the normal inventory,
proposal/verifier executor and operation writers. No production connection,
settings loader, synthetic historical seeding or source-fetching capability is
passed to it. A local SQLite ledger serializes experiment reservations across
processes: USD 0.25 before execution, at most USD 2 per owner per UTC day, and no
reuse of an experiment identity. The full reservation remains charged even after
known lower usage; receipts disclose the conservative reservation separately from
provider usage. Cancellation, timeout and crashes cannot release it for a retry.
The normal two-call, four-group and 120-second bounds continue to apply.

`scripts/review_owner_memory.py` reads a prepared packet through stdin and serves
only on loopback. The initial adapter is OpenAI Responses using the existing
local credential, without constructing a production application. All experiments
follow the application's credential precedence: `VEETBOT_OPENAI_KEY` before the
`OPENAI_API_KEY` compatibility alias. A read-only model-access check precedes
opening the review and sends no fixture text. Unavailable credentials, model
access or network service refuse before the owner reviews input or reserves spend.
The disposable runner finishes claimed groups after provider failures, preserving
the actual execution reason and call receipts instead of replacing them with a
lease-release failure. A queued retry in the disposable store is never executed.
All experiments
share `~/.local/state/veetbot/owner-memory-evaluation.sqlite3`. It stores only opaque
identities, consent/input/model/code digests, usage and finite outcome receipts.
The random page capability, strict Host/Origin checks, bounded commands, no-store
responses and restrictive content policy protect the local review. The review has
no automatic expiry. Refreshing or closing the browser tab preserves saved edits,
selections and results while the local process runs. **Cancel & discard** explicitly
closes the experiment; it and process shutdown discard private input/output and
cancel pending work. No private browser storage is introduced. The page presents actual committed outputs, verifier exclusions
and honest empty/failure outcomes. It cannot apply changes to production.

This is a qualitative experiment using newly supplied information. It does not
certify historical provenance, full-bank selection, erasure recovery, comparative
quality or production activation. Private owner text never enters the repository
or CI; the evaluator and webpage tests use invented statements.
