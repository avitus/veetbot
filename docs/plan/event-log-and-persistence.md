---
title: Event Log and Persistence
status: design
canonical: true
---

# The event log and persistence layer

This expands Sections 2.2, 5, 6.8, 6.9, 12.2, 14, 15, 23, and 24 of the
[engineering plan](engineering-plan.md), and Milestone 2. It does not replace
them. Where this document adds a table, a column, or a rule, it is an addition
of the kind Section 15 already sanctions when it says to create migrations for
*at least* the tables it lists.

Recorded as [ADR-0003](../adr/0003-event-log-and-projections.md),
[ADR-0004](../adr/0004-postgres-run-queue.md), and
[ADR-0031](../adr/0031-persistence-authoring.md). ADR-0050 later extends the
schema and append projection for authoritative conversation history and
deletion without changing the Milestone 2 guarantees.

## Why persistence is not a persistence problem

Three specifications written before this one treat the event log as authoritative
and say so in almost the same words. Memory formation is
"a governed projection over the episodic log." Session history is reconstructed
from the log rather than from the previous run's checkpoint (Section 27.4).
Working-state carry is "computed from the log, not copied from the previous
checkpoint" (context engine).

That is three specs whose correctness is downstream of this one. If the log can
lose a write, memory silently forgets. If a projection is not deterministic,
re-derivation produces a different agent than the one the user corrected. If a
sequence can be consumed out of order, a projection can skip an event forever
and no test that reads the log directly will ever notice, because the log is
fine — it is the reader's watermark that is wrong.

So the properties this layer owes the rest of the system are not "durability"
and "performance." They are:

- **Every committed event is eventually observed by every projection, exactly
  once.** Not "written durably" — *observed*. A durable event that a projection
  skipped is, to memory, an event that never happened.
- **A projection rebuilt from an empty state over the same log prefix equals the
  projection built incrementally.** This is what makes re-derivation meaningful
  and what makes a projection safe to throw away.
- **A checkpoint is an optimization, never a source of truth.** Deleting every
  checkpoint must cost time, not information.
- **A run executes at most once, even when the system believes it has crashed
  and is wrong about that.**

The rest of this document is those four sentences made mechanical.

## What the persistence layer must respect

Inherited constraints, all of which predate this document:

- **Never hold a database transaction across external I/O** (Section 12.2). The
  transaction shape is: persist intent, commit, do I/O, persist result, commit.
  This is why the append path below is designed around many short transactions
  rather than one correct long one.
- **Events are append-only** (Section 6.8). Append-only is a statement about
  bytes, not about rows: a stored payload is never rewritten, including by a
  migration. See "Versioning without rewriting" below.
- **The per-session sequence is monotonic and unique** (Section 6.8, Section 15
  `UNIQUE(session_id, sequence)`), and is allocated inside the same short
  transaction that appends the event (Section 27.5).
- **Raw reasoning text is never persisted** (Section 6.8, ADR-0006). Provider-opaque
  continuation lives in the checkpoint for the life of an active tool loop and
  nowhere else.
- **Repository methods that read user-owned data require tenant and principal
  context** (Section 6.2). Every query in this document is implicitly scoped;
  scoping is not optional and is not a ranking input.
- **One active run per session by default** (Section 27.5). This document shows
  that the default is load-bearing for projection correctness, and what has to
  change if it is ever relaxed.

## The append path

### One event, one short transaction

Appending an event is a single transaction containing exactly three statements
and no I/O:

```sql
BEGIN;

UPDATE sessions
   SET next_event_sequence = next_event_sequence + 1,
       updated_at = GREATEST(updated_at, $event_created_at)
 WHERE id = $session_id
RETURNING next_event_sequence - 1 AS sequence;

INSERT INTO events (session_id, run_id, sequence, event_type,
                    payload_schema_version, actor_type, actor_id,
                    payload, trace_id, created_at)
VALUES (...);

-- optional, same transaction: the state change this event describes
UPDATE runs SET status = $new, updated_at = now()
 WHERE id = $run_id AND status = $expected AND lease_owner = $worker
   AND lease_epoch = $epoch;

COMMIT;
```

Section 27.5 offers two mechanisms for sequence allocation, `SELECT ... FOR
UPDATE` on the session row or an atomic increment of `next_event_sequence`. This
document pins the atomic increment. Both take the same row lock, and both hold
it until the transaction ends: PostgreSQL releases a row lock at `COMMIT` or
`ROLLBACK`, never at the end of the statement that acquired it. The increment is
pinned for a different reason — it is a single atomic read-modify-write with no
application round-trip inside the lock, which is what makes the "no I/O" rule
above checkable by reading the transaction rather than by reasoning about it.
`UNIQUE(session_id, sequence)` remains the backstop, and a violation of it is a
defect to be fixed rather than a conflict to be retried.

That the lock is held to the end of the transaction is load-bearing rather than
incidental: it is what serializes appends within a session, and "Gaps are
normal; missing writes are not" below rests on it.

The state change that an event describes belongs in the same transaction as the
event. An event that says `run.completed` while the `runs` row still says
`RUNNING` is a lie the log tells forever.

The same statement advances `sessions.updated_at` to the persisted event's
timestamp. ADR-0050 makes that field the authoritative conversation-history
sort key, so activity ordering is a projection of committed events rather than
client selection or a later best-effort write. A transaction that rolls back
the event also rolls back the timestamp.

The guard by itself does not prevent that lie, and it is worth being exact about
why. The state change is a conditional `UPDATE` guarded by expected status and
lease ownership, so a writer that has lost the race matches zero rows — but a
zero-row `UPDATE` is not an error and aborts nothing. The sequence allocation
and the `INSERT` would still commit, and the log would carry precisely the
statement this section forbids. **The writer therefore inspects the affected-row
count of the guarded `UPDATE` before `COMMIT`, and rolls the transaction back
when that count is zero**, surrendering the sequence to the gap rule below. This
is the same "zero rows updated means stop, not retry" discipline that
`lease_epoch` fencing imposes everywhere else.

An append that carries no state change has no guarded `UPDATE` to inspect and is
not subject to the check. The diagnostic `run.fenced` is the case in point: a
fenced worker performs no transition, no lease release, and no checkpoint write,
and appends exactly one event, which must commit (`runtime-loop.md:821-830`).

### Gaps are normal; missing writes are not

A transaction can consume a sequence and then roll back. The sequence is not
returned to the pool, so the log contains a gap. This is fine, and every
consumer must be built to tolerate it: **a reader asks for events after a
watermark; it never waits for a specific next sequence to appear.** A projection
that blocks until `N+1` materializes will hang forever on the first rolled-back
append.

The dangerous case is the mirror image, and it is subtle enough to be worth
stating as a scenario rather than a rule. It presumes that two appends to one
session can commit out of allocation order; the defences below are the reason
they cannot, and the first of them is easy to credit to the wrong mechanism.

Two transactions are appending to the same session. Transaction A takes sequence
5. Transaction B takes sequence 6. B commits at 10:00:00.100. A commits at
10:00:00.180. A projection polls at 10:00:00.150, sees sequence 6 and not 5,
processes 6, and stores watermark 6. Event 5 commits thirty milliseconds later
and **is never observed by that projection again**, because the projection only
ever asks for sequences greater than its watermark. The log is perfectly
correct. The `UNIQUE` constraint is satisfied. Memory has silently lost a fact,
and it will be lost identically on every rebuild, so the
rebuild-equals-incremental check passes too.

There are two defences and the system already has the stronger one:

1. **Serialized allocation.** Every event writer allocates its sequence with the
   `UPDATE sessions` shown above, inside the same transaction that inserts the
   event. That statement takes the session row lock and holds it until the
   transaction ends, so a second writer blocks on it until the first commits or
   rolls back. Sequences therefore commit in allocation order and the
   interleaving above cannot arise. This is the primary defence, and the
   discipline it rests on is the one ADR-0003 already records: allocation
   happens *inside* the appending transaction, never ahead of it.
2. **Snapshot-aware watermarking, if that discipline is ever broken.** A
   projection may only advance its watermark past sequences whose transactions
   are visible to every future snapshot — in PostgreSQL, those with `xmin` below
   `pg_snapshot_xmin(pg_current_snapshot())`. Concretely, the projection reads
   events after its watermark *and* below that horizon, and leaves the rest for
   the next poll.

Two mistaken readings of defence 1 are worth closing off, because both are easy
to arrive at. The first is that the partial unique index provides it: `UNIQUE
INDEX (session_id) WHERE status NOT IN (...)` constrains the `runs` table to one
non-terminal run per session, which is a statement about runs and not about
appenders. The second is that a session therefore has only one appender. It does
not — the submit handler appends the user message from its own transaction,
alongside the run insert (`http-api-and-streaming.md:687`), while a worker may
be appending to the same session. That is safe, and it is safe because both
writers allocate the same way, not because either the index or the one-active-run
default forbids the concurrency.

The rule for implementers: the hazard returns the moment a writer takes a
sequence outside the transaction that inserts its event — by reserving a number,
performing I/O, and inserting afterwards, say. Such a writer does not hold the
session row lock across its own append, commit order stops matching allocation
order, and defence 2 becomes mandatory, changing every projection's cursor logic
in the same commit. Hard gate 1 asserts defence 1, and asserts it within a single
session and not only across sessions, because one session is where the two
appenders meet. Section 27.5's one-active-run
default remains in force for contention and for the run model, and if parallel
branches are wanted later it already directs them to separate sessions or child
runs.

### Notification is a hint, never a delivery

ADR-0010 makes PostgreSQL `LISTEN`/`NOTIFY` the transport for worker wakeup and
for live stream delivery. `NOTIFY` is transactional — payloads are delivered at
commit and discarded on rollback — so it does not need an outbox table to avoid
announcing events that never happened.

It does need a rule, because it is at-most-once. A listener that is
disconnected, still connecting, or slow enough to overflow its queue misses the
notification permanently, and nothing retries it.

**No consumer may depend on receiving a notification.** Every consumer is a
poller that a notification makes faster:

- The worker polls the claim query on an interval (250 ms interactive) and
  `NOTIFY` collapses that latency toward zero. If notifications stop entirely,
  throughput degrades to the poll interval and nothing is lost.
- Projections advance from their watermark on a timer, notified or not.
- SSE clients reconnect with `Last-Event-ID` and receive the persisted gap
  before the live stream resumes (Section 16), so a missed notification costs a
  reconnect, not a message.

The failure this rule prevents is the one where everything works in development
and a production connection blip silently drops a turn's worth of memory
formation, because the only thing that would have triggered it was a
notification nobody received.

## Versioning without rewriting

Section 6.8 requires `payload_schema_version` on the envelope and an explicit
upcasting step in the read path. Section 15's `events` table does not currently
carry the column; Milestone 2 requires it, so the migration that creates the
table includes it as `SMALLINT NOT NULL`.

The read path is a registry of pure upcasters keyed by `(event_type,
from_version)`, chained from the stored version to the current one:

```python
class Upcaster(Protocol):
    event_type: str
    from_version: int
    to_version: int

    def upcast(self, payload: dict[str, Any]) -> dict[str, Any]: ...
```

Four rules make this survivable:

- **Stored payloads are never rewritten.** There is no data migration for
  events, only upcasters. A migration that rewrites payloads destroys the
  ability to replay history as it actually was, which is the only reason to keep
  an append-only log in the first place.
- **Upcasters are total and pure.** No I/O, no clock, no lookups against current
  state. An upcaster that reads today's configuration produces a different
  history every time it runs.
- **An upcaster may never invent a value.** If a field did not exist at version
  1, the upcast fills an explicit `None` or a typed `Unknown` sentinel and every
  consumer handles it. Filling a plausible default is how a projection comes to
  assert something the user never said — and memory formation will happily turn
  that into a belief with full provenance pointing at an event that never
  contained it.
- **An unknown *higher* version is an error, not a best-effort decode.** A
  reader running old code against a log written by new code must fail loudly.
  Silent partial decoding of a newer payload is indistinguishable from data
  loss.

Every historical version keeps a recorded fixture in the eval corpus, and the
upcaster chain is exercised against all of them on every build. The gate is
totality: every fixture at every version decodes to the current shape.

## Projections

A projection is a derived, rebuildable read model with a watermark and no
authority of its own. Three exist:

| Projection | Source | Consumer | Rebuild cost |
| --- | --- | --- | --- |
| Session history | `session.*`, `user.message.*`, `assistant.message.*`, `tool.call.*` | Context builder at run seed (Section 27.4) | Per session, bounded by session length |
| Memory | Episodic events at session boundary (ADR-0018) | Retrieval, snapshot assembly (ADR-0019) | Global, expensive, opt-in per principal |
| Trajectory export | Whole runs (Section 31) | Eval fixtures, training data | Per run, cheap |

### The properties every projection has

**Deterministic.** The same log prefix produces the same projection state, byte
for byte, on the same `builder_version`. This is not an aesthetic preference: it
is the precondition for ADR-0018's re-derivation, which replays rejections over
a rebuilt memory and expects the corrections to still hold. A projection that
consults the wall clock, iterates a set in hash order, or reads current
configuration is not rebuildable, and the agent's memory becomes a function of
when it was last rebuilt.

**Watermarked.** Each projection stores its position as `(projection_name,
scope, watermark_sequence, builder_version, updated_at)`, written **in the same
transaction as the projection state it justifies.** If the state and the
watermark can diverge, a crash between them either double-applies or skips.

**Rebuildable from zero.** Rebuild is a first-class, tested operation, not a
recovery script someone writes during an incident. CI rebuilds every projection
over a synthetic log and asserts equality with the incrementally built state.

**Never authoritative.** A projection may be dropped and rebuilt at any time
without user-visible loss beyond the rebuild's duration. Anything that cannot
survive that is not a projection and belongs in the log.

### Derived events and the rebuild loop

Memory formation both consumes the log and emits events onto it —
`memory.formed`, `memory.superseded`, `memory.promoted`. Naively this makes
rebuild non-idempotent: replaying the log re-runs formation, which appends a
second `memory.formed` for the same fact, which the next rebuild replays again.

Derived events therefore carry a **deterministic derivation key**: a hash of the
contributing `source_event_ids`, the rule that fired, and the `builder_version`.
Appends of derived events are conditional on that key being absent
(`ON CONFLICT DO NOTHING` against a unique index). A rebuild re-derives the same
keys and writes nothing new, so it converges instead of multiplying. This is the
same identity discipline ADR-0018 already adopted when it matched rejections by
content rather than by belief id, for the same reason: re-derivation mints new
ids, so ids cannot be the identity.

A consequence worth stating plainly: **changing `builder_version` changes every
derivation key**, so a rebuild after a formation-rule change appends a parallel
set of derived events rather than deduplicating against the old ones. That is
correct — they are different derivations — but it means a rule change is a
migration with a cost, and superseding the old derivations is part of shipping
it, not an afterthought.

## The trajectory export

Section 31 adds a projection that turns finished runs into evaluation
fixtures and training data, and ADR-0016 records the decision. Between them
they fix four acceptance criteria and one exclusion list. Neither states a
format, a redaction procedure, or what "consent-gated" means mechanically,
and [evaluation-harness.md](evaluation-harness.md) has already built the
consuming half against the assumption that this half is redacted before a
converter ever sees it (`evaluation-harness.md:1322`). This is the
producing half, and it is the third projection in the table above.

### The export is an artifact, not a query

The projection maintains, per finished run, only what is cheap to maintain
incrementally: the contributing sequence range, the `builder_version` that
would produce the document, and whether the run has reached a terminal
status. The document itself is materialized once, on demand, into the
artifact store, and every later reader reads the artifact.

Materializing into the artifact store rather than into a table is the whole
of the decision, and it is made for four properties the store already has
and a table would have to grow: content addressing by SHA-256, a key
derived from platform-generated values rather than composed from caller
input (`sandbox-isolation.md:1103`), an authorized read path that ADR-0028
already puts in front of both metadata and bytes, and `expires_at` with a
sweeper behind it. Every one of those is load-bearing for a governed
export. A second bytes-holding mechanism inside PostgreSQL would be a worse
version of a thing that already exists, and it would be the version whose
deletion path nobody tested.

`ArtifactOrigin` gains `TRAJECTORY_EXPORT` for this
(`sandbox-isolation.md:1055`). The origin matters because it is the one
whose contents are a function of an entire run rather than of a single act
inside it, and an operator reviewing what a run produced should not have to
infer that from a filename.

Its trust label is the floor of the run. `trust` is inherited and never
assigned by the producer, and an export flattens platform, user, and
external-untrusted content into one document, so the label the document
carries is the lowest label any contributing span carried. For any run that
called a tool that is `EXTERNAL_UNTRUSTED`, which is the correct and
slightly uncomfortable answer: an export is a file of recorded text, some
of it written by a system nobody here controls, and anything that reads one
back into a context window must treat it that way.

### The format

One JSON document, versioned, with the conversation in the shape the
normalized protocol already uses. ADR-0016 says "ShareGPT / messages" and
this picks `messages`, because the internal protocol is already
messages-shaped and ShareGPT is then a rename of the role vocabulary — a
consumer's transformation, not a producer's obligation.

```text
{
  "schema_version": 1,
  "export_id":      "<uuid7>",
  "run_id":         "<uuid>",
  "tenant_id":      "<uuid>",
  "agent_id":       "research-assistant",
  "agent_version":  4,
  "outcome":        "COMPLETED" | "FAILED" | "CANCELLED",
  "failure":        {"kind": "...", "at_step": 7} | null,
  "recorded_on":    "2026-07-28",
  "builder_version": "trajectory@3",
  "redaction": {
    "ruleset_version": "...",
    "replacements":    {"provider_key": 1, "dsn_password": 2}
  },
  "messages": [ ... ],
  "tools":    [ {"name": "...", "schema_sha256": "..."} ]
}
```

`outcome` is what makes Section 31.3's fourth criterion — failed runs
captured and labeled distinctly — true by construction rather than by
convention, and `failure` carries the classification without carrying the
error text, which is the field most likely to have a path, a host, or a
query string in it. Its three values are `RunStatus`'s terminal subset
(`runtime-loop.md:276`) and not a vocabulary of the export's own, so a
consumer filtering exported runs and a reader querying `runs.status` ask the
same question in the same words.

`recorded_on` is a date, not a timestamp, and there are no per-message
timestamps at all. Per-message timing is the highest-entropy correlatable
field an export could carry, no stated consumer needs it — the harness
discards timestamps at conversion (`evaluation-harness.md:1299`) and a
training corpus has no use for them — and a field that is dropped by every
consumer and re-identifies a user is a field that should not have been
written. `tools` records the name and schema hash of every tool the run
touched, which is what lets a converter tell a missing fixture from a tool
that has since changed shape.

### What is left out, and why each

| Left out | Because |
| --- | --- |
| Reasoning, raw or summarized | ADR-0006; never stored, so never exported |
| Usage, cost, and prices | Not a property of the trajectory; commercially sensitive per tenant |
| Per-message timestamps | Correlatable, and dropped by every consumer |
| Internal identifiers | Event ids, sequences, checkpoint ids; a converter seeds its own |
| Provider metadata | `request_id` and friends are support correlation, not conversation |
| Checkpoints and queue events | Execution mechanics, not the trajectory |
| Artifact bytes | The export carries an `ArtifactRef`, never the content |

The reasoning row deserves a sentence because it is the one that looks like
an omission and is not. ADR-0006 rejected persisting reasoning with
redaction applied, on the grounds that reasoning paraphrases its input and
redaction is pattern-based. That reasoning applies to the export with more
force, not less: an export is the artifact most likely to leave the system,
and a paraphrase is exactly what a pattern cannot catch.

### Redaction is a pipeline that ends in a refusal

Three stages, in order, and the third is the one that makes the first two
trustworthy.

1. **Structural exclusion.** Everything in the table above is dropped by
   the builder, by construction. No pattern is involved and none could
   help; these fields are excluded because of what they are, not because
   of what they contain.
2. **Pattern replacement.** The secret scanner's five rule families
   (`bootstrap-and-composition.md:1128-1136`) run over every message body,
   every tool argument, and every tool result, and a match is replaced with
   `[redacted:<rule_name>]`. The key-name families the log-redaction
   processor already uses (`development-toolchain.md:153-155`) run over
   structured tool arguments and results, where a value's key is better
   evidence than the value's shape. A tenant may add patterns; it may not
   remove one.
3. **Verification.** The scanner runs again, over the finished document.
   A hit here fails the export.

The third stage is not belt-and-braces, it is the contract. Redaction that
reports success is a claim, and a claim with no check behind it decays the
first time a message body acquires a shape stage two does not cover. So the
export **fails closed**: a verification hit raises `ExportRedactionError`,
writes no artifact, and reports the rule name and the message index. It
never reports the match, for the reason the scanner already gives — a
report that echoes the secret has moved the secret somewhere worse
(`bootstrap-and-composition.md:1141-1143`).

Failing rather than repairing is deliberate. A verification hit means stage
two has a gap, and silently redacting the same string a second time hides
the gap while shipping the artifact. The failure is a defect report with a
run attached.

Two things this pipeline cannot do, stated here rather than discovered
later. It cannot recognize content that is sensitive because of who it is
about: a user describing a third party in prose produces no pattern, and no
version of this will. And it cannot recognize a secret with no shape — a
password that looks like a word is a word. Policy-restricted PII is
therefore a per-tenant declared pattern set that rides in stage two, not a
classifier, and the residual is a limit of the mechanism that belongs in
whatever a tenant is told before consent is asked for.

### Consent is stamped forward and withdrawn backward

Section 31.3 requires that export honor tenant scope and per-principal
consent. No mechanism for either exists anywhere in the corpus, so this is
it.

Two conditions, both required. The tenant must have export enabled, which
is operator configuration and covers the deployment that wants none of
this. The principal on the run's session must have granted export consent,
which is a stored grant with a scope and two timestamps. Neither implies
the other: a tenant enabling export does not consent on its users' behalf,
and a principal's grant does nothing in a tenant where export is off.

The grant is evaluated **when the run starts**, and its answer is stamped
onto the run as `export_consent`. Export reads the stamp. Withdrawal is
evaluated **at export time and again by the sweeper**, over every run the
principal ever produced.

That asymmetry is the design, and it reduces to one sentence: a grant is a
statement about data the principal has not produced yet, and a withdrawal
is a statement about data they have. So a grant is prospective — it does
not reach back and authorize conversations the principal had before they
were asked, and cannot, because nobody consents meaningfully to the
contents of a conversation they have forgotten. A withdrawal is total. It
blocks export of every run, stamped or not, and it expires every export
artifact already produced from that principal's runs by setting
`expires_at` to now, after which the artifact sweeper deletes them on its
next pass.

Reusing `expires_at` rather than writing a deletion path is the point.
Withdrawal is rare, deletion-on-withdrawal is the operation most likely to
be written once and never exercised, and routing it through the sweeper
that already runs every day means the withdrawal path is tested by every
ordinary expiry.

A subagent run inherits its parent's stamp at creation, and **an export
never descends into child runs.** Each run is exported separately or not at
all. The alternative — a parent export that inlines its children — makes
the redaction surface recursive, makes the consent question ambiguous when
a child ran under a different principal, and produces a document whose size
is unbounded in a system that otherwise bounds everything.

What consent does not solve: an export of a principal's run contains
whatever other people said inside it, by way of tool results and quoted
content. The pipeline redacts what has a shape and the export is
tenant-scoped, and beyond that this is a governance boundary rather than an
engineering one, named here so that nobody reads "consent-gated" as
"complete".

### Retention, and why promotion is the durable step

An export expires like any other artifact — thirty days by default, per the
artifact retention this corpus already sets (`sandbox-isolation.md:1167`).
It is not special-cased to live longer, and the reason is that the two
things Section 31.2 wants exports for do not actually want a long-lived
export.

An eval fixture wants a case, and `agent eval promote` already produces
one: a converted case is marked `source: trajectory`, carries the export
id, and does not enter the blocking suite until a person has read it and
written its assertions. That reviewed case lives in source control under
review, which is where a durable artifact belongs. A training corpus wants
a corpus, assembled deliberately, with its own governance and its own
retention, and it is a consumer of exports rather than a pile of them.

So promotion is the durable step and the export is the perishable one.
That is the property that keeps the governance question small: at any
moment the set of undeleted exports is roughly the last thirty days of
deliberate export commands, not the entire history of the platform.

### The commands and the endpoint

```text
agent run export <run-id>          write the redacted export
agent run export <run-id> --json   the ArtifactRef on stdout
```

`export` becomes the fourth reserved word after `agent run`
(`bootstrap-and-composition.md:969`), which is cheaper than a thirteenth
top-level command and follows the precedent `agent eval`'s five
subcommands already set. It reuses the existing exit codes without
addition: a refused consent check exits 1, an unknown run exits 2, an
`ExportRedactionError` exits 1 with the rule name on stderr.

`POST /v1/runs/{run_id}/export` is the same application service, returns
the `ArtifactRef`, and is idempotent per run — a second call against a run
whose export exists and has not expired returns the existing reference
rather than rebuilding, because rebuilding under a changed
`builder_version` would silently hand a caller a different document under
the same run id.

`agent eval promote <run-id>` requires an export to exist and fails naming
this command when none does. That is the mechanical form of "the converter
consumes the redacted artifact and has no access to the raw log": the
converter cannot trigger production, so there is exactly one path through
which conversation content becomes a file, and exactly one place to audit.

## Checkpoints

Section 6.9 already identifies the problem: a checkpoint is written after every
model response and every tool call, and each stores the full conversation, which
is superlinear in run length. It also states the fix. This pins it.

**A checkpoint is a delta against a base.** Full snapshots are written at run
start, at every compaction boundary, and at terminal status. Every other
checkpoint stores the changes since the previous checkpoint. Reconstruction
walks back to the nearest full snapshot and applies deltas forward; the walk is
bounded because compaction forces a full snapshot.

**The conversation is stored as event references.** A checkpoint holds
`event_id` references resolved through the session-history projection, not
inlined message bodies. Two exceptions stay inline because they cannot be
reconstructed from the log:

- **Provider-opaque continuation** (`ProviderContinuation`, `ProviderReasoningItem`).
  Never in the log by rule (ADR-0006), so the checkpoint is the only place it can
  live. It is dropped at run boundaries and when a run is routed to a different
  provider (Section 27.4, ADR-0007).
- **Compacted summary text.** It is authored by a model call, not derived from
  the log, so replaying the log would not reproduce it. It is content, and it is
  covered by the context engine's elision rules.

**Losing checkpoints costs time, not information.** With the conversation stored
as references and the log authoritative, deleting a run's checkpoints and
resuming reconstructs the same state, modulo the two inline exceptions above:
losing an active `ProviderContinuation` forces the current tool loop to restart
from the last full snapshot rather than resume mid-loop. This is a test, not a
hope — "delete the last three checkpoints, resume, assert the same terminal
state" belongs in the resilience suite next to the kill-the-worker test that
Section 14.2 already requires.

**Retention.** After a run reaches a terminal status, prune to the final
checkpoint plus the last full snapshot. Before that, prune deltas older than the
most recent full snapshot. Checkpoint bytes per run is a tracked metric because
it is the one that quietly grows until an incident.

## The run queue

### Claiming, with priority

Section 14.1 specifies `FOR UPDATE SKIP LOCKED`. The revision summary and
ADR-0010 additionally require priority ordering so asynchronous jobs cannot
head-of-line-block interactive turns; Section 14's body does not yet carry it.
The claim query:

```sql
UPDATE runs r
   SET status          = 'RUNNING',
       lease_owner     = $worker_id,
       lease_epoch     = r.lease_epoch + 1,
       lease_expires_at= now() + $lease_duration,
       attempts        = r.attempts + 1,
       updated_at      = now()
  FROM (
        SELECT id
          FROM runs
         WHERE status = 'QUEUED'
           AND (scheduled_for IS NULL OR scheduled_for <= now())
           AND priority = ANY($eligible_classes)
         ORDER BY priority ASC, created_at ASC
           FOR UPDATE SKIP LOCKED
         LIMIT 1
       ) AS c
 WHERE r.id = c.id
RETURNING r.*;
```

Three priority classes, low number first:

| Class | Value | Contents | Latency budget |
| --- | --- | --- | --- |
| Interactive | 0 | A user is waiting on this turn | Seconds |
| Async | 10 | Scheduled and long-running work (Milestone 10) | Minutes |
| Maintenance | 20 | Consolidation, rebuilds, exports | Hours |

Strict priority starves the bottom of the queue, so **capacity is reserved by
class rather than allocated by strict order**: a worker pool is configured with
a minimum concurrency per class, and `$eligible_classes` for a given claim
reflects which of that worker's slots are free. A pool that reserves one slot
for maintenance guarantees consolidation progresses during a busy day, at the
cost of one slot of interactive capacity. Aging was the alternative and it is
worse here — it makes latency depend on queue history, which is exactly the
property that makes a starvation bug reproduce only in production.

The claim query needs `(status, priority, created_at)` as a partial index on
`status = 'QUEUED'`; Section 15's `(status, created_at)` does not serve the
ordering.

### Leases, and the worker that is not dead

The lease protocol is Section 14.1's: claim, set expiry, refresh periodically,
release, and reclaim after expiry. Refresh at one third of the lease duration,
so two consecutive missed heartbeats do not lose the run.

The part that needs adding is what happens when the reclaim is *wrong*. A worker
that is garbage-collecting, swapping, or partitioned from the database is not
dead, and its lease expires anyway. The sweeper reclaims the run, a second
worker starts executing it, and now two processes believe they own the same run.
Lease expiry is a timeout, and a timeout is a guess.

The defence is a fencing token. `lease_epoch` increments on every claim, and
**every write a worker makes is conditional on its own epoch**:

```sql
UPDATE runs SET ... WHERE id = $run_id
  AND lease_owner = $worker_id AND lease_epoch = $my_epoch;
```

An `UPDATE` affecting zero rows means the worker has been fenced. It stops
immediately, does not retry, does not append, and does not treat the zero-row
result as a transient failure — it is the only correct signal that another
worker owns this run now. The same predicate guards the event append's state
change, so a fenced worker cannot write an event either.

This does not prevent a fenced worker from having already begun a side-effecting
tool call, which is why fencing is necessary but not sufficient, and why
recovery leans on tool-level idempotency below.

### Queue-level retry, and what is not a retry

Two different things get called retry and they have different rules:

- **Step-level retry** — a model call or tool call fails and is retried inside
  the run, per Section 13's table, within the run deadline, with exponential
  backoff and jitter. The run never leaves `RUNNING`.
- **Run-level requeue** — a run is returned to `QUEUED` because its lease
  expired and the sweeper reclaimed it. `attempts` increments on claim.

Only lease expiry requeues. A run that fails with a permanent classification
from Section 13 (`ModelPermanentError`, `ToolPolicyDenied`, `AuthorizationError`,
`BudgetExceeded`) transitions to `FAILED` immediately with a typed `failure`
payload; requeueing it would burn attempts against an error that cannot succeed.

`max_attempts` defaults to 3. A run exceeding it goes to `FAILED` with
`failure.reason = "max_attempts_exceeded"` and the accumulated per-attempt
failures retained. There is no separate dead-letter table: `runs` already has a
`failure JSONB` column and a terminal status, and a dead-letter queue whose
entries nothing reads is a table that only ever grows.

`scheduled_for` carries the backoff for a requeued run and doubles as the
scheduling primitive Milestone 11 needs, which is why it lands here rather than
being invented twice.

### Recovery at a safe boundary

On reclaim, Section 14.2's procedure runs against the tool-invocation table
(Section 8.4), which is the authority on what actually executed:

1. Load the latest checkpoint and reconstruct through the delta chain.
2. Read `tool_invocations` for the run at or after the checkpoint's
   `last_event_sequence`.
3. For each invocation not in a terminal state, decide by `IdempotencyClass`:
   read-only and idempotent tools are re-executed; conditionally idempotent
   tools are re-executed only if the stored `idempotency_key` can be replayed
   against the external service; a non-idempotent tool left in `RUNNING` is
   **never** automatically retried.
4. Mark ambiguous non-idempotent executions `UNCERTAIN`, emit
   `tool.call.uncertain`, and surface the run for review rather than guessing.
5. Resume at the first incomplete safe boundary.

`UNCERTAIN` is a terminal state for that invocation, not a transient one. The
model is told, in a structured tool result, that the call's outcome is unknown —
which is true, and which is information it can act on — rather than being told
the call failed, which is a claim the system cannot support and which invites a
duplicate write.

## Schema additions

Section 15 requires migrations for *at least* the tables it lists. These
additions are what Milestone 2's own Implement list and Section 16's contract
already require, plus the columns this document's mechanisms need. No existing
column is removed or retyped.

```text
events
  + payload_schema_version SMALLINT NOT NULL
  -- no new index: a projection scans `session_id = ? AND sequence > watermark`,
  -- which Section 15's UNIQUE(session_id, sequence) already serves. An index on
  -- (session_id, id) would not support that predicate, and commit order needs
  -- no index of its own because allocation inside the appending transaction
  -- makes commit order and sequence order the same order.

runs
  + priority        SMALLINT     NOT NULL DEFAULT 0
  + attempts        SMALLINT     NOT NULL DEFAULT 0
  + scheduled_for   TIMESTAMPTZ  NULL
  + lease_epoch     INTEGER      NOT NULL DEFAULT 0
  + export_consent  BOOLEAN      NOT NULL DEFAULT FALSE  -- at run start
  + INDEX (status, priority, created_at) WHERE status = 'QUEUED'
  + UNIQUE INDEX (session_id) WHERE status NOT IN
      ('COMPLETED','FAILED','CANCELLED')    -- 27.5 single active run

idempotency_keys                            -- M2 "Idempotency records"
  key             TEXT PRIMARY KEY          -- Section 16 Idempotency-Key
  tenant_id       UUID NOT NULL
  principal_id    UUID NOT NULL
  request_hash    TEXT NOT NULL
  run_id          UUID NOT NULL
  created_at      TIMESTAMPTZ NOT NULL
  expires_at      TIMESTAMPTZ NOT NULL

projection_watermarks
  projection_name TEXT NOT NULL
  scope           TEXT NOT NULL DEFAULT ''  -- '' global, else session_id
  watermark_seq   BIGINT NOT NULL
  builder_version TEXT NOT NULL
  updated_at      TIMESTAMPTZ NOT NULL
  PRIMARY KEY (projection_name, scope)

derived_event_keys
  derivation_key  TEXT PRIMARY KEY         -- source ids + rule + version
  event_id        BIGINT NOT NULL
  created_at      TIMESTAMPTZ NOT NULL

export_consent                             -- Section 31.3, per principal
  tenant_id       UUID NOT NULL
  principal_id    UUID NOT NULL
  granted_at      TIMESTAMPTZ NOT NULL
  withdrawn_at    TIMESTAMPTZ NULL
  PRIMARY KEY (tenant_id, principal_id)

trajectory_exports                         -- one row per exported run
  export_id       UUID PRIMARY KEY
  tenant_id       UUID NOT NULL
  principal_id    UUID NOT NULL            -- denormalized from session
  run_id          UUID NOT NULL
  artifact_id     UUID NOT NULL
  builder_version TEXT NOT NULL
  ruleset_version TEXT NOT NULL
  created_at      TIMESTAMPTZ NOT NULL
  UNIQUE (run_id)                          -- the endpoint's idempotency
  INDEX (tenant_id, principal_id)          -- the withdrawal sweep
```

`trajectory_exports` carries `principal_id` even though it is reachable
through `run_id` and `session_id`, because consent withdrawal is a sweep
over one principal's exports and a two-join sweep on the rarest write path
is how a governance operation acquires a query plan nobody has looked at.
The `UNIQUE (run_id)` constraint is what makes the export endpoint
idempotent per run at the schema rather than at the service.

`idempotency_keys` stores `request_hash` so that a repeated `Idempotency-Key`
carrying a *different* body is a `ConflictError` rather than a silent return of
an unrelated run. Section 16 requires that a repeated key return the original
run; it does not say what a reused key with new content means, and returning
someone else's run because a client reused a key is worse than an error.

### Post-Milestone 9 deletion records

ADR-0050 adds two small tables. They are not session projections and do not
retain conversation content:

```text
session_deletions
  session_id      UUID PRIMARY KEY
  tenant_id       TEXT NOT NULL
  principal_id    TEXT NOT NULL
  deleted_at      TIMESTAMPTZ NOT NULL
  INDEX (tenant_id, principal_id, deleted_at)

session_deletion_artifacts
  session_id      UUID NOT NULL REFERENCES session_deletions ON DELETE CASCADE
  artifact_id     UUID NOT NULL
  tenant_id       TEXT NOT NULL
  artifact        JSONB NOT NULL
  PRIMARY KEY (session_id, artifact_id)
```

Both tables have forced tenant row-level security. `session_deletions` is the
content-free ownership tombstone that makes a repeated delete idempotent.
`session_deletion_artifacts` is a transactional outbox containing the minimum
serialized artifact reference needed to remove bytes from external storage.
The delete request tries every queued reference after commit; the maintenance
worker retries remaining rows and deletes each row only after byte deletion
succeeds. Removing a tombstone cascades its pending queue, although normal
operation retains the tombstone.

## Authoring migrations

The plan requires migrations in four places. Section 15 says *"Create
Alembic migrations for at least these tables"*. Section 23 rule 16 says
*"Add a migration for every schema change"*. Section 24 makes two of
them conditions of every milestone: migrations upgrade from a clean
database, and migrations upgrade from the previous revision. Section 25
lists *"Run migrations"* as a step and
[development-toolchain.md](development-toolchain.md) gives it `make
migrate`. Between all of that there is a directory name, a Makefile
target, and no statement of what a migration looks like when someone
writes one.

That is a cheap gap to leave and an expensive one to close later,
because the first six migrations set the conventions for the rest
whether or not anyone decided them.

### One head, always

The revision graph is linear. Merge revisions are not written, and
`alembic merge` is not used. Two branches that both descend from the
same revision are resolved by rebasing the later one onto the earlier
before it merges — that is, by changing its `down_revision` and
re-testing it — not by adding a third revision that joins them.

The reason is that two of the plan's own statements stop being
well-defined otherwise. `alembic upgrade head` in Section 25 and `make
migrate` both name *a* head; with two, the command is ambiguous and
resolves by luck. And *"migrations upgrade from the previous
revision"* has no referent in a graph where a revision has two
predecessors.

Linearity is a merge-time cost paid by whoever loses the race, which is
the correct place for it, and it is asserted by
`gate.structure.migration_graph`.

### Revision identifiers carry no order, and neither do file names

A migration file is named `<revision>_<slug>.py`, where `<revision>` is
the hex identifier Alembic generates and `<slug>` is an imperative
phrase naming the change: `a3f19c2b7d04_add_lease_epoch_to_runs.py`.

Ordering lives in `down_revision` and nowhere else. Hand-numbered
prefixes — `0007_`, `0008_` — are specifically not used, because two
revisions written the same week both become `0007` in two branches, and
the file listing then shows an order that the graph does not have. A
name that cannot express order cannot express the wrong order.

The slug names the change, not the milestone and not the ticket. A
reader of `git log --name-only` three years from now is looking for the
migration that added a column, and *"m2 schema"* does not help them.

### Autogenerate drafts; a person writes

`alembic revision --autogenerate` is where a migration starts and never
what gets committed. Every generated revision is read and edited before
it is reviewed, because autogenerate does not see:

1. Server defaults and the backfill an existing row needs.
2. Partial and conditional indexes. This document requires three of
   them — the queued-run index, the single-active-run unique index, and
   the events commit-order index — and a generated migration will
   produce the unfiltered form of at least the first two.
3. Enum value additions, which PostgreSQL applies with `ALTER TYPE ...
   ADD VALUE` and which cannot run inside a transaction block.
4. Anything about data.

What keeps the edited file honest is not review but
`gate.event.migration_clean`: after `upgrade head` against an empty
database, an autogenerate run against the same metadata must produce an
empty diff. A hand-edit that drifts from the mapped tables fails that
gate rather than being discovered by the next person to autogenerate.

### Structure and data are separate revisions

A revision either changes structure or moves rows. It does not do both.

Three consequences make this worth the extra file. A backfill over a
large table takes minutes or takes a lock, and a structural change
mixed into it cannot ship until the backfill finishes. A structural
change is reversible and a backfill usually is not, so a combined
revision's `downgrade()` is a partial truth. And the two want different
transaction shapes: structure in one transaction, a backfill in batches
that commit as they go.

The three-step pattern for a column that must end up `NOT NULL` is a
structural revision that adds it nullable, a data revision that fills
it, and a structural revision that sets the constraint. At Milestone 2
no table has production rows in it and all three could be one
statement; the pattern is written down now because the migration that
needs it is written by someone reading the ones that came before.

### Downgrade is written, and it is not an operational promise

Every structural revision implements `downgrade()`. The reason is the
round-trip in `gate.event.migration_clean` rather than an intention to
run it: a downgrade that was never written is a downgrade that was
never checked, and the check is what catches a revision that creates an
object it does not name.

Rolling a deployed schema backwards is a restore from backup, not a
downgrade, and this document does not pretend otherwise. A data
revision's `downgrade()` raises `NotImplementedError` with a sentence
saying what would be lost.

### Lock-taking DDL is split, and says so

`CREATE INDEX CONCURRENTLY` cannot run inside a transaction, and neither
can `ALTER TYPE ... ADD VALUE`. A revision containing either declares
itself non-transactional rather than relying on Alembic's default, and
contains nothing else, so that a failure leaves one incomplete object
and not a half-applied revision.

An index on a populated table is created concurrently. An index on a
table created by the same revision is not, because there are no rows
and no lock to avoid.

### A migration may add to `events`; it may never rewrite one

The failure-modes table above names *"History rewritten by
migration"* and answers it with immutable payloads and upcasters. It is
repeated here because the person who would write that `UPDATE` is a
migration author reading migration conventions, and this is the page
they are on. A revision may add a column to `events`, add an index, or
create a table that reads from it. A revision that writes to
`events.payload` is a defect, whatever it is trying to fix.

### The revision the code expects is a constant

ADR-0024 decision 6 fixes the behavior — the composition root never
runs migrations, asserts the schema revision, and refuses to start on a
mismatch — and leaves open where the expected value comes from. Two
answers are available and only one of them is a check.

`EXPECTED_REVISION` is a module-level string constant in the
persistence adapter, updated by the same commit that adds the
migration. Startup phase 3 reads the single row of `alembic_version` and
compares. On mismatch it names both revisions and exits non-zero; it
does not migrate, and it does not start in a degraded mode.

The alternative is to read the head out of the migrations directory at
runtime with `ScriptDirectory`. It is rejected because it makes the
assertion vacuous in the one deployment that matters: the code and its
migrations always ship together, so the computed head always equals the
code's expectation, and the mismatch the assertion exists to catch —
new code against an un-migrated database — is exactly the case where
both sides come from the same image and agree.

A static check asserts that `EXPECTED_REVISION` equals the single head
of the revision graph, so the constant cannot drift from the
migrations in the repository. That check is part of
`gate.structure.migration_graph`; the runtime refusal is
`gate.event.revision_pinned`.

### Where the plan's four statements land

| Statement | Source | Where it becomes checkable |
| --- | --- | --- |
| Migrations exist for the tables | §15 | The tables are created by revisions, not by `create_all` |
| A migration for every schema change | §23 rule 16 | `gate.event.migration_clean`: a mapped table with no revision fails the empty-diff check |
| Upgrade from a clean database | §24 | `gate.event.migration_clean` |
| Upgrade from the previous revision | §24 | `gate.event.migration_stepwise` |

`create_all` deserves its own sentence, because it is the shortcut every
test suite reaches for. Schema in tests is created by running the
migrations, not by `metadata.create_all`. A suite that creates its
schema the fast way is a suite in which no migration is ever exercised
until deployment, and Section 24's two criteria become statements
nothing evaluates.

## Ports and data model

Section 7's `RunRepository` and `EventRepository` are unchanged. Section 7 names
the checkpoint, session, tool-invocation, and usage repositories without typing
them; these are the additions this layer needs.

```python
class NewEvent(BaseModel):
    session_id: UUID
    run_id: UUID | None
    event_type: str
    payload_schema_version: int
    actor_type: str
    actor_id: str | None
    payload: dict[str, Any]
    trace_id: str | None
    derivation_key: str | None = None   # set for derived events only


class CheckpointRepository(Protocol):
    async def write(
        self, run_id: UUID, checkpoint: RunCheckpoint, *, full: bool
    ) -> int: ...
    async def latest(self, run_id: UUID) -> RunCheckpoint | None: ...
    async def prune(self, run_id: UUID, *, terminal: bool) -> int: ...


class ProjectionCursor(BaseModel):
    projection_name: str
    scope: str
    watermark_seq: int
    builder_version: str


class Projection(Protocol):
    name: str
    builder_version: str

    async def apply(
        self, events: Sequence[EventEnvelope], cursor: ProjectionCursor
    ) -> None: ...
    async def rebuild(self, scope: str) -> ProjectionCursor: ...


class RunQueue(Protocol):
    async def enqueue(
        self, run: Run, *, priority: int, scheduled_for: datetime | None
    ) -> None: ...
    async def claim(
        self, worker_id: str, eligible_classes: Sequence[int]
    ) -> tuple[Run, int] | None: ...      # (run, lease_epoch)
    async def heartbeat(
        self, run_id: UUID, worker_id: str, lease_epoch: int
    ) -> bool: ...                        # False means fenced
    async def release(
        self,
        run_id: UUID,
        worker_id: str,
        lease_epoch: int,
        status: RunStatus,
    ) -> None: ...
    async def reclaim_expired(self, limit: int) -> int: ...
```

`heartbeat` returning `False` rather than raising is deliberate: being fenced is
an expected outcome of a normal race, not an exceptional one, and the worker's
handling of it is to stop cleanly rather than to unwind through error paths that
might themselves try to write.

New event types, added to Section 6.8's list:

```text
run.requeued
run.fenced
projection.rebuild.started
projection.rebuild.completed
```

## The ORM surface

Section 2.2 chooses SQLAlchemy's async interface and states the rule that
governs it: *"never share one `AsyncSession` across concurrent tasks. Each
request, worker operation, or parallel tool invocation must receive its own
unit of work and database session."* Section 4 names four modules under
`adapters/persistence/`. Section 7 names the repositories. Rules 1, 7, and
13 constrain what may cross out of them.

What none of that says is what a mapping looks like. That is decided once,
by whoever writes the first repository, and then copied into every
repository written after it — which is the argument for deciding it here.

The answer below is mostly forced. Two of the dependency rules, read
together, eliminate every option but one, and this section is largely the
work of showing that.

### Row classes are not domain types

The tempting shape is to map `Run`, `Session`, and the event envelope
directly, so that one class is both the domain object and the row. Both
ways of doing that are ruled out already.

**Declarative mapping** of a domain type puts `from sqlalchemy.orm import
...` inside `domain`, and rule 1 confines `domain` to the standard library
and Pydantic. This is the version that fails loudly: the import walk that
is a Milestone 0 deliverable rejects it on the first run.

**Imperative mapping** — `registry.map_imperatively(Run, runs_table)` —
avoids the import, which is what makes it worth naming rather than
dismissing. It fails a different rule and fails it silently. Mapping
attaches instrumentation to the class: the mapped type gains identity-map
membership, lazy-load behavior on attribute access, and a relationship to
the session that loaded it. The domain object *becomes* the ORM object.
Rule 7 — *"SQLAlchemy ORM objects must never be returned from
repositories"* — is then unenforceable, because every repository return
value is one, and the static check ADR-0001 assigns to that rule resolves
signatures, so it has nothing left to reject at the exact moment the
violation is total. Rule 1's own note anticipates this by naming *"ORM
modes"* among the Pydantic-only behaviors to keep out of the domain.

The runtime agrees with the rules, as it happens: Pydantic's `BaseModel`
and SQLAlchemy's declarative base carry different metaclasses, so a class
that is both does not exist without writing a third metaclass to reconcile
them.

So row classes are separate types. One declarative class per table, in
`adapters/persistence/sqlalchemy_models.py`, and no instance of one is ever
returned, yielded, logged, or passed out of that package.

### Translation is a function, not a feature

Each table gets two translation functions, and they are written by hand:

```python
def to_domain(row: EventRow) -> EventEnvelope: ...
def values(event: NewEvent) -> dict[str, Any]: ...
```

They live in `adapters/persistence/mappers.py`, beside the row classes and
inside the same confinement — one module more than Section 4's tree lists,
added because the alternative is translation code scattered through the
repository bodies where nothing can find it.

The direction is asymmetric on purpose. Reading produces a domain object.
Writing produces a `dict` of column values, which the repository passes to
an `insert()` or an `update()`; it does not produce a row object for the
caller to hold, because a row object in a caller's hand is the thing this
whole section exists to prevent.

Three shortcuts are specifically not taken. `model_validate` with
`from_attributes=True` is the ORM mode rule 1 excludes, and it works by
attribute access, which on a mapped class is what triggers a lazy load.
A generic field-name-matching mapper turns a column rename into a missing
key at runtime instead of a type error at check time. And returning
`Row` or `RowMapping` and letting the caller subscript it puts the schema
in the caller.

The cost is real and worth stating plainly: roughly twenty tables, two
functions each, all of them boring, all of them needing a test. What it
buys is that the database schema and the wire contract move independently
— adding a column is not an API change until someone writes the line that
makes it one — and that the upcaster has somewhere to stand.
`to_domain` for an event is where `payload_schema_version` is read and the
upcaster chain runs, which is only possible because there is a function
there at all.

### A repository is constructed with a session, not with a factory

ADR-0024 settles the module-scope half of rule 13: a factory is
constructed in phase 3, a session never is. The call-scope half is this
one, and it is the more consequential of the two.

If a repository holds the `async_sessionmaker` and opens a session per
method, then every method is its own transaction. Two writes that must
commit together cannot; the *"persist intent, commit, do I/O, persist
result, commit"* shape Section 12.2 requires cannot be expressed from
outside, because the caller has no way to say which of its calls belong to
the same commit; and the append path above — three statements, one
transaction — has no way to be three statements in one transaction.

So a repository is constructed with a live `AsyncSession` and holds no
factory. The caller that owns the unit of work opens the session and
constructs the repositories over it. `unit_of_work.py` holds one async
context manager that does exactly that: open a session from the factory,
expose the repositories built over it, commit on clean exit, roll back on
exception.

Repository methods do not commit. A method that commits internally makes
every caller's transaction boundary a fiction, and it is invisible at the
call site, which is the combination that makes it expensive later.

The benefit beyond correctness is that the transaction boundary becomes a
construction site — a literal `async with` with a body — rather than an
emergent property of which methods happened to commit. That is what makes
`gate.structure.txn_hygiene` decidable: the check has a syntactic region to
look inside, and the question *"is there provider, tool, or sandbox I/O in
this region"* has an answer.

### Where a unit of work begins and ends

Three shapes cover the system, and naming them is cheaper than
rediscovering them:

1. **An API request.** One unit of work, committed before the response is
   written. Dispatch happens after it commits, never inside it —
   [bootstrap-and-composition.md](bootstrap-and-composition.md) makes that
   absolute, and the inline dispatcher asserts no unit of work is open when
   it is called.
2. **A claim.** The claim query is its own unit of work and commits before
   execution starts. It must: holding it open would hold a row lock across
   the entire turn, which is Section 12.2's prohibition in its most
   expensive form.
3. **A turn.** Not one unit of work but a sequence of short ones —
   read and commit, call the provider, append and commit. Section 12.2 is
   the rule; the append path is what it looks like when followed.

Parallel tool invocations get one unit of work each rather than sharing
the turn's, which is Section 2.2's sentence about `AsyncSession` and
concurrent tasks arriving at the implementer.

### What crosses the boundary

Rule 7 states the prohibition. Its positive form, which is what someone
writing a repository method actually needs, is: **a repository method
returns a domain type, a Pydantic model declared in `domain`, a scalar, or
`None`.**

That covers the case rule 7 does not obviously reach. A projection query
computes something no domain aggregate names — a per-session count, a lag
figure, three columns from a join. The answer is not a tuple and not a
`dict`; it is a small Pydantic read model declared in `domain` beside the
aggregates. Declaring it there costs a class and buys a name that the
`api` layer and the harness can both refer to.

Return annotations are concrete for the same reason. `-> dict[str, Any]`
and `-> Any` are not violations of rule 7 so much as evasions of the check
that enforces it, which resolves signatures and can only reject what a
signature names.

`gate.structure.orm_confined` makes the confinement mechanical: no module
outside `adapters/persistence/` imports `sqlalchemy`, and no name defined
in `sqlalchemy_models.py` appears in a signature outside that package.

There is a second reason for all of this, beyond hygiene. Milestone 1
ships five in-memory repository adapters that Section 21 calls *"production
adapters run against the same contract suites as their PostgreSQL
counterparts, not test doubles."* A port whose return types are the
domain's has two implementations. A port whose return types are rows has
one, and the contract suite that was supposed to hold both honest becomes
a test of the only adapter that can satisfy it.

## Failure modes and defenses

| Failure | How it happens | Defense |
| --- | --- | --- |
| Silent missing write | A projection advances its watermark past a sequence whose transaction has not committed yet | Every writer allocates its sequence inside the transaction that appends the event, so the session row lock serializes commits into allocation order; snapshot-aware watermarking if that discipline is ever broken |
| Event committed without its state change | The guarded `UPDATE runs` matches zero rows and the surrounding transaction commits anyway, leaving an event that describes a transition that did not happen | The writer checks the affected-row count before `COMMIT` and rolls back on zero; an append carrying no state change, such as `run.fenced`, is the explicit exception |
| Projection stall on a gap | A reader waits for the next contiguous sequence after a rolled-back append | Readers ask for events after a watermark, never for a specific sequence |
| Split-brain worker | A stalled worker's lease expires; the sweeper hands the run to a second worker | `lease_epoch` fencing on every write; zero rows updated means stop, not retry |
| Duplicate turn | A client retries a submit that already succeeded | `idempotency_keys` with `request_hash`; repeat returns the original run, mismatch is a `ConflictError` |
| Notification loss read as event loss | A consumer treats `LISTEN`/`NOTIFY` as delivery | Every consumer polls from a watermark; notification only collapses latency |
| Upcaster invents data | A missing field is filled with a plausible default | Sentinels only; consumers handle unknown explicitly; unknown higher version is a hard error |
| History rewritten by migration | A payload shape change is applied as an `UPDATE` over `events` | Stored payloads immutable; change is expressed only as an upcaster |
| Derived-event multiplication | A rebuild re-emits `memory.formed` for facts already derived | Deterministic derivation key, conditional append, convergent rebuild |
| Checkpoint growth | Full conversation inlined at every tool call | Deltas against periodic full snapshots; conversation as event references |
| Head-of-line blocking | A long async run occupies the workers a user is waiting on | Priority classes with capacity reserved per class |
| Non-idempotent double write | A tool left `RUNNING` by a crash is retried | Idempotency class decides; ambiguous cases become `UNCERTAIN` and stop |
| Watermark/state divergence | Projection state committed separately from its cursor | Both written in one transaction |
| Export leaks a secret | A message body carries a shape the replacement stage does not cover | A verification scan over the finished document; a hit fails the export and writes nothing |
| Consent granted retroactively | A grant read at export time covers runs recorded before anyone asked | Consent is stamped on the run at start; export reads the stamp, never the table |
| Withdrawal leaves exports behind | Deletion-on-withdrawal is a bespoke path exercised once a year | Withdrawal sets `expires_at` to now and the daily artifact sweeper does the deleting |

## Hard gates

Milestone 2 does not pass until every one of these holds, with three
exceptions, each noted where it occurs. The migration-graph walk registers
at Milestone 0, because the empty migration Milestone 0 already requires is
a graph, and a walk that only starts once there are twelve revisions is a
walk that has already missed the branch it exists to prevent. The two
export gates register at Milestone 3, because Milestone 2 builds the
projection's scaffold and Milestone 3 builds the export itself.

1. **Sequence integrity.** A fuzz test appending concurrently, with injected
   rollbacks, produces no duplicate `(session_id, sequence)` and no event that
   any projection failed to observe. It appends concurrently *within* one
   session as well as across sessions — a submit-handler append racing a
   worker's — because a single session's sequence space is where the
   out-of-order-commit hazard lives, and appending only across sessions would
   exercise the serialization that matters nowhere. **M2.**
2. **Projection determinism.** For every projection, rebuild-from-zero over a
   recorded log equals the incrementally built state, field for field, on the
   same `builder_version`. **M2.**
3. **Upcaster totality.** Every recorded historical fixture, at every version,
   decodes to the current shape. An unknown higher version raises. **M2.**
4. **Exactly-once execution.** Two workers racing on one run: one executes, the
   other is fenced and writes nothing. Asserted with the sweeper's reclaim
   interval driven to zero. **M2.**
5. **Crash recovery.** Terminate a worker after a checkpoint; the run resumes and
   reaches the same terminal state. Section 14.2 already requires this; it is
   restated here because the delta-chain reconstruction is new and it is what the
   test now exercises. **M2.**
6. **Checkpoint dispensability.** Delete a run's non-terminal checkpoints,
   resume, and reach the same terminal state. Registered as
   `gate.event.checkpoint_dispensable`, which `runtime-loop.md` #9 restates:
   this document owns it. **M2.**
7. **Transaction hygiene.** A static check plus a runtime assertion: no
   transaction is open across an `await` that performs provider, tool, or sandbox
   I/O. Registered as `gate.structure.txn_hygiene`, which `runtime-loop.md` #6
   restates: this document owns it. **M2.**
8. **One migration head.** A static walk of the revision graph finds exactly
   one head, no merge revision, and no revision whose `down_revision` is a
   tuple. The same walk asserts that the adapter's `EXPECTED_REVISION`
   constant equals that head. Registered as
   `gate.structure.migration_graph`. **M0.**
9. **Migrations upgrade from a clean database.** `upgrade head` against an
   empty database succeeds, and an autogenerate run against the resulting
   schema produces an empty diff. Section 24 states the criterion; this is
   what makes it decidable, and the empty diff is what keeps a hand-edited
   revision honest. Registered as `gate.event.migration_clean`. **M2.**
10. **Migrations upgrade from the previous revision.** For every revision,
    `upgrade` from its predecessor succeeds and `downgrade` back to the
    predecessor succeeds, against a database that already holds the earlier
    schema. Section 24's second migration criterion. Data revisions are
    exempt from the downgrade half by declaration, not by omission.
    Registered as `gate.event.migration_stepwise`. **M2.**
11. **The pinned revision is refused, not repaired.** Started against a
    database at the wrong revision, the process exits non-zero naming both
    revisions, does not migrate, and does not serve a request first.
    ADR-0024 decision 6 requires the behavior; this asserts it. Registered
    as `gate.event.revision_pinned`. **M2.**
12. **The ORM stays in the adapter.** No module outside
    `adapters/persistence/` imports `sqlalchemy`, and no name defined in
    `sqlalchemy_models.py` appears in a signature outside that package. This
    is dependency rule 7 in the form a static check can evaluate. Registered
    as `gate.structure.orm_confined`. **M2.**
13. **The export is redacted, and refuses when it is not.** A run seeded
    with one instance of each of the scanner's five rule families, plus a
    tenant-declared pattern, exports with every one replaced by its
    placeholder and a clean verification scan over the finished document.
    A second case disables the replacement stage and asserts that the
    export raises `ExportRedactionError`, writes no artifact, and names
    the rule that fired without printing what it matched. Registered as
    `gate.event.export_redacted`. **M3.**
14. **Consent is stamped forward and withdrawn backward.** Four states —
    tenant disabled, principal never granted, granted then withdrawn, both
    present — produce three refusals and one export. The withdrawal case
    additionally asserts that an export produced before the withdrawal has
    `expires_at` in the past and is gone after one sweeper pass, and that
    a grant made after a run does not make that run exportable. Registered
    as `gate.event.export_consent`. **M3.**

## Tracked metrics

Claim latency p99 by priority class, lease reclaim rate (a rise means leases are
too short or workers are stalling), projection lag in sequences and seconds,
checkpoint bytes per run, and rebuild duration per projection.

## Build sequence

1. **Schema and migrations.** Section 15's tables plus the additions above,
   created by revisions written to the conventions in "Authoring migrations"
   rather than by `metadata.create_all`. The round-trip gates belong here and
   not at the end: `migration_clean` and `migration_stepwise` have something
   to assert as soon as the second revision exists, and a convention adopted
   at revision two costs nothing where the same convention retrofitted at
   revision twenty costs a rewrite of everything before it. Nothing consumes
   `payload_schema_version`, `priority`, or `lease_epoch` yet; they are cheap
   now and are retrofits that break replay later.
2. **Append path.** Sequence allocation, envelope insert, conditional state
   change, all in one transaction. Fuzz for sequence integrity before anything
   depends on it.
3. **Upcaster registry.** With the first two versions of one event type
   recorded, so the mechanism exists before it is needed under pressure.
4. **Checkpoints.** Full snapshots first, then deltas, then reference-based
   conversation storage. Each step keeps the dispensability test passing.
5. **Queue.** Claim, lease, heartbeat, fencing, sweeper. Then priority classes
   and reserved capacity.
6. **Session-history projection.** The one Section 27.4 requires for run seeding;
   it makes cross-run continuity real and is the first consumer to exercise
   watermarks.
7. **Recovery.** Tool-invocation-driven resume, idempotency classes, `UNCERTAIN`.
8. **Trajectory-export projection scaffold.** Structure, watermark, and the
   `export_consent` stamp on `runs`, which costs nothing at Milestone 2 and
   is a replay-breaking retrofit afterwards, because a run that started
   before the column existed has no honest value to backfill. The document
   builder, the redaction pipeline, the consent tables, and both export
   gates are Milestone 3.
9. **Rebuild.** Rebuild-from-zero for every projection, wired into CI as a gate
   rather than as a script.

## Decisions

1. **The log's contract is observation, not durability.** A committed event that
   a projection never observed is, to every consumer, an event that did not
   happen; the layer's gates are written against that.
2. **Sequence allocation is an atomic increment of `sessions.next_event_sequence`**
   inside the appending transaction, with `UNIQUE(session_id, sequence)` as a
   backstop whose violation is a defect rather than a retryable conflict.
3. **Sequence gaps are legal and readers tolerate them.** Consumers read after a
   watermark and never wait for a specific next sequence.
4. **Allocating the sequence inside the appending transaction is load-bearing
   for projection correctness**, not only for contention. The session row lock
   is held to `COMMIT`, which serializes appends into allocation order even
   though a session has more than one appender. Allocating outside that
   transaction requires switching projections to snapshot-aware watermarking in
   the same change.
5. **`LISTEN`/`NOTIFY` is a latency optimization and never a delivery
   guarantee.** Every consumer is a poller first.
6. **Stored event payloads are immutable**; schema evolution is expressed only
   as pure, total upcasters, and an upcaster may never invent a value.
7. **An unknown higher payload version is a hard error.** Old code must not
   partially decode new events.
8. **Projections are deterministic, watermarked, rebuildable, and never
   authoritative**, with state and watermark written in one transaction.
9. **Derived events carry a deterministic derivation key** and append
   conditionally, so rebuilds converge instead of multiplying.
10. **Checkpoints are deltas against periodic full snapshots**, with the
    conversation stored as event references; only provider-opaque continuation
    and compacted summary text stay inline.
11. **Losing checkpoints costs time, not information**, and that is a test.
12. **The claim query orders by priority then age**, across three classes, with
    capacity reserved per class rather than strict priority, so maintenance work
    cannot starve.
13. **Lease expiry is a guess, so every worker write is fenced by `lease_epoch`**;
    a zero-row update means stop, not retry.
14. **Only lease expiry requeues a run.** Permanent Section 13 classifications
    fail immediately; `max_attempts` is 3; `runs.failure` is the dead letter.
15. **`scheduled_for` carries both retry backoff and Milestone 11 scheduling**,
    so the primitive is built once.
16. **Ambiguous non-idempotent tool executions become `UNCERTAIN`** and are
    reported to the model as unknown-outcome rather than as failed.
17. **Row classes are separate declarative types confined to the persistence
    adapter.** This is forced rather than chosen: declarative mapping of a
    domain type violates dependency rule 1 by import, and imperative mapping
    violates rule 7 in a way no static check can see.
18. **Translation between rows and domain types is two hand-written
    functions per table**, never a generic mapper and never Pydantic's
    `from_attributes`, which is the ORM mode rule 1's note excludes.
19. **A repository is constructed with a live session, never with a
    factory**, and repository methods do not commit. The unit of work owns
    the boundary, which is what makes that boundary a construction site a
    static check can look inside.
20. **A repository returns a domain type, a `domain` read model, a scalar, or
    `None`**, under a concrete return annotation, because the check enforcing
    rule 7 resolves signatures and cannot reject what a signature does not
    name.
21. **The revision graph is linear.** No merge revisions, no `alembic
    merge`; a branch is resolved by rebasing `down_revision`. Two of the
    plan's own statements — `upgrade head` and "upgrade from the previous
    revision" — are not well-defined otherwise.
22. **Structure and data are separate revisions**, with `downgrade()`
    written for structural revisions and raising `NotImplementedError` for
    data revisions.
23. **Autogenerate drafts and a person edits**, kept honest by an empty-diff
    round trip rather than by review, because the four things autogenerate
    misses are the four that matter here.
24. **The expected schema revision is a constant in the adapter**, not the
    head computed from the migrations directory at runtime — a computed head
    always agrees with the code that ships beside it, which makes the
    assertion vacuous in exactly the case it exists to catch.
25. **Test schema is created by running the migrations**, never by
    `metadata.create_all`, or Section 24's two migration criteria become
    statements nothing evaluates.
26. **A trajectory export is an artifact, not a table**, so it inherits
    content addressing, a derived key, an authorized read path, and
    `expires_at` with a sweeper behind it, rather than growing a second
    and less tested version of each inside PostgreSQL.
27. **The export carries no per-message timestamps** and a date rather
    than a timestamp at the top, because timing is the most correlatable
    thing an export could carry and no stated consumer keeps it.
28. **Redaction fails closed.** A verification scan runs over the finished
    document, and a hit raises rather than redacting a second time,
    because a second pass hides the gap in the first and ships the
    artifact anyway.
29. **Consent is stamped forward and withdrawn backward.** The grant is
    evaluated at run start and stamped on the run; a withdrawal blocks
    every run, stamped or not, and expires every artifact already
    produced.
30. **Withdrawal deletes through `expires_at` and the existing sweeper**,
    never through a bespoke deletion path, so the rarest governance
    operation runs on the most exercised code in the system.
31. **An export never descends into child runs.** Each run exports
    separately, which keeps the redaction surface flat, keeps the consent
    question single-principal, and keeps the document bounded.
32. **Promotion is the durable step and the export is perishable**, so the
    undeleted set is a thirty-day window rather than a history of every
    conversation the platform has held.
33. **A persisted event advances its session's activity timestamp in the append
    transaction.** History ordering therefore cannot claim activity that did
    not commit and cannot miss activity because a later projection failed.
34. **Authoritative session deletion uses a content-free tombstone and an
    artifact-deletion outbox.** The database graph disappears atomically while
    external byte deletion remains retryable across process failure.

## Open questions

None blocking Milestone 2. Four recorded for Andy's review, with the interim
decision noted in
[questions for review](../status/questions-for-review.md):

- Whether single-active-run-per-session should be promoted from Section 27.5's
  "default" to an invariant, given that projection correctness now depends on
  it. The interim position keeps Section 27.5's wording and documents the
  coupling.
- Whether the trajectory-export projection belongs in Milestone 2 or Milestone 3.
  Section 21's Implement list says Milestone 2; Section 21.1's sequencing table
  says Milestone 3. The interim split is scaffold in 2, export in 3.
- Whether export consent is one grant or two, split between evaluation
  fixtures and training data, which Section 31.2 lists as different uses
  with plausibly different answers. The interim position is one grant,
  because Section 31.3 says consent in the singular and a scope vocabulary
  invented here would collide with the principal scopes Milestone 4 owns.
- Whether an export should be rebuildable under a new `builder_version`
  while the previous artifact still exists. The interim position is no: a
  second call returns the existing reference, because a changed builder
  would otherwise hand a caller a materially different document under the
  same run id and nothing in the document says which builder made it —
  except `builder_version`, which is exactly the field a caller who has
  already stored the first one will not re-read.
