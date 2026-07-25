---
title: Questions for Review
---

# Decisions taken without review

Andy is away from 2026-07-24 for roughly 72 hours, with the instruction to
continue the plan to the point where coding can begin, to make an intelligent
decision at each obstacle rather than stopping, and to record every one of those
decisions for review.

This file is that record. Each entry states what was decided, why, what the
alternative was, and what it costs to reverse. Nothing here is settled; every
item is open for a different answer.

The reversal cost is the only column that should drive urgency. Items marked
**cheap** are prose changes. Items marked **moderate** touch a migration or a
schema that has not been written yet, so they are cheap now and expensive after
Milestone 2 ships. Items marked **expensive** would require rewriting a
committed spec and its dependents.

## Process and environment

### The plan is committed but not pushed

**Decided:** work happens on a branch, `docs/plan-completion`, cut from `main`
at `dba12a5`. Commits are made at each module boundary with the identity
`Claude <noreply@anthropic.com>`, passed per-command so the global git config is
untouched.

**Why:** the push cannot be performed from here. The bridge to the local machine
has no network access — SSH to `github.com:22` is refused at the proxy — and the
cloud container that does have network has no write credential for the
repository. Committing locally preserves the work in git with real history
rather than leaving it as uncommitted working-tree changes.

**Action required on return:** review the branch, then run one `git push -u
origin docs/plan-completion`. Everything else is done.

**Reversal cost:** none. The branch can be rebased, squashed, or discarded.

### Module order was chosen by accumulated dependency debt

**Decided:** the specs are written in the order event log and persistence,
policy and approvals, model gateway and adapters, tool system and MCP,
evaluation harness, runtime loop — not in milestone order.

**Why:** each of those is depended upon by specs already written. The context
engine (ADR-0020) assumes a `policy_version` that nothing yet defines; the
memory specs assume projections that rebuild. Writing the depended-upon layers
first means the existing specs get validated against them rather than drifting.

**Note:** the plan's own rule that evaluations are built before advanced
features argues for pulling the evaluation harness earlier than fifth. It was
kept at fifth because the harness needs the four specs above it to know what it
is gating. This is worth a second opinion.

**Reversal cost:** cheap, and mostly moot once all six exist.

## Event log and persistence (ADR-0003, ADR-0004)

### `payload_schema_version` is added to the `events` table

**Decided:** the column is added in the first migration, `SMALLINT NOT NULL`,
even though nothing reads it until the first schema change.

**Why:** Section 6.8 and the Milestone 2 acceptance list both require payload
versioning; Section 15's table omits the column. It is one column now and an
unmigratable retrofit once a production log exists, because there is no correct
value to backfill for events written before versioning.

**Alternative:** add it when the first event shape changes.

**Reversal cost:** moderate — trivial before Milestone 2, effectively impossible
after.

### One appender per session is treated as load-bearing for correctness

**Decided:** Section 27.5's "one active run per session" default is kept as
written, but ADR-0003 records that projection correctness now depends on it, and
the `runs` table enforces it with a partial unique index.

**Why:** a monotonic sequence watermark is only safe if writes to a session are
serialized. With concurrent appenders, a projection can advance past a sequence
whose transaction has not committed and never see it — the log stays consistent
and the loss reproduces identically on every rebuild.

**The question:** should Section 27.5's *default* be promoted to an
*invariant*? Making it an invariant closes the hazard permanently but forecloses
concurrent runs per session, which may matter for multi-device use (ADR-0011).

**Interim position:** keep the wording, document the coupling, and specify the
companion change — snapshot-aware watermarking with
`pg_snapshot_xmin(pg_current_snapshot())` — that must land in the same commit if
the default is ever relaxed.

**Reversal cost:** cheap as prose; moderate if relaxed after projections exist,
since every projection's cursor logic changes at once.

### No outbox table

**Decided:** event publication uses `LISTEN`/`NOTIFY` with no outbox, and every
consumer polls from a watermark.

**Why:** `NOTIFY` is transactional — delivered on commit, discarded on rollback
— so the atomicity an outbox provides is already present. What `NOTIFY` does not
provide is delivery, and a watermark poll covers that more cheaply than a second
table with its own drainer.

**Alternative:** a standard transactional outbox.

**Reversal cost:** cheap. Adding an outbox later is additive; consumers that
already poll keep working.

### Three priority classes, capacity reserved rather than aged

**Decided:** interactive (0), async (10), maintenance (20), with worker slots
reserved per class.

**Why:** the revision summary requires claim-priority ordering and Section 14's
body has none. Reserved capacity was chosen over aging because aging makes a
run's latency a function of the queue's history, which cannot be reconstructed
from a single run's record when explaining an incident.

**Alternative:** strict priority with aging, or separate queues per class.

**Reversal cost:** cheap. `priority` is a `SMALLINT`, so classes can subdivide
without a migration, and the scheduling policy is worker-side configuration.

### `max_attempts` is 3, and `runs.failure` is the dead letter

**Decided:** only lease expiry requeues a run; permanent Section 13
classifications fail immediately; there is no separate dead-letter table.

**Why:** the plan specifies no queue-level retry semantics at all, so some
number had to be chosen. Three is conventional and low enough that a
systematically failing run does not consume a worker for long. Keeping failures
in `runs` keeps them attached to their own events and checkpoints.

**Alternative:** a configurable per-priority-class attempt cap.

**Reversal cost:** cheap — it is a constant and a status value.

### Trajectory export is split across Milestone 2 and Milestone 3

**Decided:** the projection scaffold and its watermark are built in Milestone 2;
the export itself is Milestone 3.

**Why:** the plan contradicts itself. Section 21's Milestone 2 Implement list
says "Trajectory-export projection scaffold"; Section 21.1's sequencing table
places trajectory export in Milestone 3. Both readings are satisfied by the
split, and the scaffold is a second consumer that exercises the watermark
machinery early, which is worth having.

**The question:** which of the two statements in the plan is the intended one?
The other should be corrected so the document stops disagreeing with itself.

**Reversal cost:** cheap.

### Ambiguous non-idempotent tool executions are reported as `UNCERTAIN`

**Decided:** a tool left `RUNNING` by a crash, whose idempotency class does not
permit a safe retry, resolves to `UNCERTAIN` and is reported to the model as an
unknown outcome rather than as a failure.

**Why:** "it failed" is a claim the runtime cannot support about a payment that
may have been made. Models handle stated uncertainty better than they handle a
confident falsehood, and the alternative invites a retry of an operation that may
already have succeeded.

**Alternative:** report as failed, or block the run for human review.

**Reversal cost:** moderate. It is a tool-protocol outcome value, so changing it
later means changing every tool contract and the prompt text that explains it.

## Policy and approvals (ADR-0005, ADR-0006)

### Five undefined types were defined rather than deferred

**Decided:** `ProposedAction`, `ApprovalStatus`, `SideEffectClass`,
`RiskLevel`, and `IdempotencyClass` each appear exactly once in the plan, as the
type of a field, and are defined nowhere. All five are now specified.

**Why:** Milestone 4 cannot start without them, and each one silently decides
what the engine is capable of deciding. Every value was derived from a statement
the plan already makes — the fifteen `SideEffectClass` values are Section 9.2's
fifteen action categories, and the four `IdempotencyClass` values are Section
8.4's four crash-recovery bullets — so that nothing was invented that the plan
did not already imply.

**Alternative:** leave them for the coding agent to decide at implementation
time.

**Reversal cost:** moderate. Renaming a value is cheap now and a migration
later, since three of them are persisted on `tool_invocations`.

### Section 9.2's three non-enum decision strings were resolved as conditions

**Decided:** "Allow with restrictions" and "Allow only in sandbox" become
`ALLOW` guarded by a predicate, denying when the predicate fails. "Deny
initially" becomes `DENY` in the `default` profile, with "initially" read as a
statement about which profile is loaded.

**Why:** three of the matrix's sixteen cells hold strings that are not
`PolicyDecisionType` values, so the matrix cannot be looked up by a program as
written. Both readings preserve the stated outcome exactly; no cell's behaviour
changes.

**The question:** is "Deny initially" about a later *profile* (a deployment can
turn it on) or a later *milestone* (the default changes for everyone)? The two
differ in who holds the switch.

**Reversal cost:** cheap.

### `policy_version` is a content hash, and rules are files rather than rows

**Decided:** `policy_version` is `{profile}@{profile_sha256[:12]}+h{hardline_
sha256[:8]}`. Profiles and hardline rules are version-controlled YAML packaged
in the distribution, loaded once at process start and frozen. The
`policy_profiles` table records that a ruleset was loaded; it does not store
rules.

**Why:** the field is declared as a bare `str` on `PolicyDecision` with no
producer, format, or storage, and the context engine's `ContextPlan` already
consumes it. A counter depends on someone remembering to increment it; a stale
counter asserts an equality that does not hold. Section 22 classifies policy
rules as trusted, and a table anyone with a connection string can edit is not a
trust boundary.

**The cost, stated plainly:** an urgent policy change requires a deploy. It
cannot be made from a console.

**Alternative:** a rules table with an admin UI, or a monotonic counter.

**Reversal cost:** moderate. The format is embedded in every persisted decision,
so changing it later means either a migration or a parser that understands two
formats.

### Approval was generalized beyond tool calls

**Decided:** `ActionKind` covers tool calls, memory writes, skill authoring, and
artifact export. `approvals.tool_invocation_id` widens from `NOT NULL` to
nullable and a general `action_id` carries the reference.

**Why:** Section 30.4 requires skill authoring to be approval-gated and the
memory specs require governance on the write path, but the approval object is
structurally tool-only. The alternative — fabricating a tool invocation for each
non-tool action — puts rows in `tool_invocations` that no tool executed and
corrupts every metric computed over that table.

**Reversal cost:** moderate. It is a nullable column and an enum, both cheap
before Milestone 4 ships.

### The denial tool result deliberately tells the model very little

**Decided:** a denial carries a stable `reason_code`, a fixed message per code,
and a remediation hint, enforced by a field allowlist. The rule that fired, the
pattern, and the profile name never reach the model. Three identical denied
proposals fail the run.

**Why:** Milestone 4 requires that "denial becomes a structured tool result" and
no shape exists anywhere. The model is a partially trusted consumer under
Section 22; naming the rule that blocked it hands it a search gradient. The
circuit breaker exists because a model that re-proposes a denied action will do
so until the run budget is gone.

**The cost:** some legitimate self-correction will be slower than it would be
with a richer message.

**Reversal cost:** cheap for the message content; moderate for the breaker
threshold, which is a constant.

### `MEMORY` and `KNOWLEDGE` were placed in Section 22's trust tiers

**Decided:** Section 11.2's seven trust labels map onto Section 22's three
tiers. `MEMORY` is partially trusted content that cannot authorize;
`KNOWLEDGE` is untrusted as instructions. Only `PLATFORM`,
`TRUSTED_CONFIGURATION`, and `USER` can authorize anything, and `USER` only
within the principal's own scopes.

**Why:** the two lists never map onto each other and two labels had no tier at
all, which matters because Section 11.2 makes an authorization claim about trust
labels — a tool result must never change approval requirements. Memory is
agent-authored from prior conversation, so a belief that the user always
approves deletions is a belief and not a grant.

**Reversal cost:** cheap as prose; moderate once rules key on it.

### Self-approval is permitted by default

**Decided:** the principal who started a run may resolve their own approval.
Requiring a distinct resolver is available as a rule, not a hardcoded condition.

**Why:** Section 6.2 anticipates single-user deployments, where there is no one
else to ask, and a gate nobody can pass is not a safety control.

**Alternative:** require a second principal for `HIGH` and `CRITICAL` risk.

**Reversal cost:** cheap — it is a rule in a profile.

### Two API endpoints were added because the CLI has no endpoint to call

**Decided:** `GET /v1/approvals` and `GET /v1/approvals/{id}`, tenant-scoped
from the authenticated principal and never from a query parameter.

**Why:** Section 17 requires `agent approval list` and Section 16 defines only
the resolve endpoint. Section 17 also forbids a second runtime loop in the CLI,
so the command must call an application service through an endpoint that does
not exist.

**Reversal cost:** cheap.

### A second `REQUIRE_APPROVAL` at revalidation becomes a denial

**Decided:** revalidation after approval voids outright on an argument, scope,
or agent-version change, and re-evaluates only when `policy_version` changed. If
re-evaluation returns anything other than `ALLOW` — including a second
`REQUIRE_APPROVAL` — the call is denied rather than asked again.

**Why:** Section 9.3's step 8 says to revalidate and does not say what the
comparison is or what happens when it fails. Asking twice is defensible, but it
admits a loop where a ruleset that always escalates parks a run forever. A
denial the user can deliberately retry is better than a pause the system cannot
leave.

**Reversal cost:** cheap.

### ADR-0006 was written as already amended

**Decided:** the record states the original prohibition and carries ADR-0007's
amendment inside it as decision 5, rather than presenting an unamended decision
that four sections of the plan already contradict.

**Why:** Section 6.8 cites "ADR-0006 as amended" and Section 10.6 records the
amendment, but the ADR itself was never written. Writing the pre-amendment
version would have created a record that the plan already disagrees with on the
day it was committed.

**Reversal cost:** none — it is a record of a decision already made elsewhere.
