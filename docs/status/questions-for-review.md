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

## Model gateway (ADR-0002)

### `ProviderReasoningItem.trust_level` still defaults to `PLATFORM`

**Decided:** the default was left as Section 6.6 states it, and the spec bounds
it instead — the payload is never parsed, never rendered as prompt text, never
reaches the policy engine, and never enters memory or a user-facing renderer as
trusted content.

**Why:** `PLATFORM` is the highest trust tier and the policy engine reads trust
tiers to set restrictiveness, so on its face this gives model output the standing
of platform configuration. `AssistantMessage` correctly defaults to
`EXTERNAL_UNTRUSTED`. Fixing the default means editing a plan sentence, which the
conversion constraints forbid doing unilaterally. The four bounding properties
leave the label no consumer able to act on it, which turns a live privilege
inversion into a naming defect.

**This is the one item on this page worth reading first.** The bounding is sound
but it is a fence around a mislabelled thing, and fences are maintained by people
who remember why they are there.

**Reversal cost:** cheap now — it is a default value and four sentences.
Expensive after any code reads the field.

### `server_tool_use` is refused rather than mapped

**Decided:** an Anthropic `server_tool_use` content block raises
`ModelProtocolError` and fails the attempt in 0.1.

**Why:** Section 10.2 has no row for it. A tool executed inside the provider
never passes the policy engine, so mapping it onto an ordinary tool call would
admit a class of side effects with no policy decision. We never request server
tools, so the refusal is unreachable in normal operation and exists to make the
day someone enables one loud rather than quiet.

**Alternative:** a configuration flag permitting specific server tools with an
explicit policy mapping. That is the right eventual answer and it needs a policy
design first.

**Reversal cost:** cheap.

### Retry ownership splits on whether output was already emitted

**Decided:** the adapter retries only before the first event reaches the caller,
at most three times; after any output it fails and the caller decides.
`max_attempts = 3` lives in application code.

**Why:** plan line 1233 puts retries in the adapter and line 1556 says to keep
retry decisions in application code "not in provider adapters alone". The word
alone implies a split and does not say where. Pre-output failures are invisible
and safely repeatable; post-output failures need to know whether partial output
was shown to a user, which only the caller knows.

**Reversal cost:** cheap.

### `UsageEvent` was made advisory rather than terminal

**Decided:** `UsageEvent` may appear zero or more times, carries provisional
figures, and is always superseded by `ModelCompletedEvent.turn.usage`.

**Why:** Section 10.2 lists `UsageEvent` in the neutral vocabulary with no OpenAI
source row, and Section 10.4 requires exactly one completed-or-failed event per
attempt. Making usage advisory resolves both at once: OpenAI emits none,
Anthropic emits an early one for live cost meters, and neither competes with the
terminal event.

**Reversal cost:** cheap.

### Provider pinning was resolved temporally, not architecturally

**Decided:** provider selection happens once at run start and the pin is absolute
and persisted for the life of the run. Milestone 10's availability routing
chooses the pin; it never re-routes a live run. A provider outage fails a pinned
run.

**Why:** Section 10 line 1266 requires pinning and Milestone 10 line 2722 wants
availability routing, which read as a contradiction. They are not, if selection
and execution are separated in time. Mid-run switching would either discard
provider continuation state invisibly or attempt a translation that cannot be
done.

**Question for Andy:** is failing a run on a provider outage acceptable, or
should a run be allowed to restart against a different provider from its last
checkpoint? The second is a real option and it is a different feature.

**Reversal cost:** moderate — the pin is a persisted column.

### Two schema tables were added for usage

**Decided:** `model_calls` (one row per attempt) and `model_prices`
(append-only). `runs.usage` keeps its shape and becomes a rollup maintained in
the same transaction.

**Why:** Section 15 has no usage table and `runs.usage JSONB` cannot answer which
attempt burned the tokens, which is the question a run that retried three times
raises. Section 6.5's cost precedence needs a place to record which source a
figure came from.

**Reversal cost:** moderate — a migration that has not been written yet.

### Failed attempts count against budget

**Decided:** budget is checked before each attempt using usage that includes
failed and crashed-out attempts, and a run that exhausts its budget this way
stops with `BUDGET_EXCEEDED`.

**Why:** Section 12.3 accepts duplicate provider cost on crash recovery and
Section 6.5 enforces `max_cost`; together they permit a crash-looping run to
overspend on work it never kept. Charging failed attempts makes the overspend
visible to the check rather than after the invoice.

**Reversal cost:** cheap.

### Model-call timeouts were invented

**Decided:** `ModelRequest.timeout_seconds` defaults to 600 and
`stream_idle_seconds` to 60.

**Why:** no document defines a model-call timeout, while `ToolSpec` has had one
since Section 8. Without them a hung connection stalls a run until the worker
deadline fires, if it has one. The idle timeout is the load-bearing half.

**Question for Andy:** 60 seconds between events is comfortable on both vendors
today, including during extended thinking, but it is a guess until real traces
exist.

**Reversal cost:** cheap — both are configuration.

### The model registry is a hashed file, not a table

**Decided:** the model registry is a YAML file per provider profile, validated
and hashed at load, with `registry_version` recorded on every attempt.

**Why:** it matches how the policy profile is treated, keeps 0.1 free of another
migration, and makes a run reproducible against the exact catalogue it resolved
against.

**Alternative:** a database table, which would allow per-tenant model catalogues
sooner.

**Reversal cost:** moderate.

### The contract suite runs against five adapters, not the one the fixtures name

**Decided:** fake, recorded, OpenAI, Anthropic and `chat_completions`, with the
suite written against the fake before any real adapter exists.

**Why:** Milestone 3 line 2202 names OpenAI fixtures only and line 2430 requires
the suite against three providers. The acceptance criterion is treated as
controlling and the fixture list as an incomplete enumeration. Writing the suite
against the fake first is what stops it being shaped around whichever provider
was implemented first.

**Reversal cost:** cheap.

### Unknown provider stream events are ignored rather than fatal

**Decided:** an unrecognized event type is logged once per process per type and
skipped.

**Why:** providers add event types without warning, and a strict adapter breaks
on a vendor deploy we did not perform. Logging once keeps the addition visible in
telemetry without one line per token.

**Reversal cost:** cheap.

### Reasoning display is filtered per session

**Decided:** `ReasoningDeltaEvent` is published to the live transport only for
sessions with reasoning display enabled.

**Why:** reasoning is the highest-volume event class and the least often wanted.
Per-session is more flexible than per-tenant and marginally more work.

**Reversal cost:** cheap.

## Tool system and MCP (ADR-0021)

### `mark_effect_sent` is a tool-author obligation, not a type-checked one

**Decided:** a `NON_IDEMPOTENT` or `CONDITIONALLY_IDEMPOTENT` tool must call
`ctx.mark_effect_sent()` immediately before its first outbound operation. The
contract suite asserts it for every registered tool against a fake target, and
the executor records `tool.contract.no_watermark` when a tool returns `ok`
without having called it.

**Why:** the alternative is wrapping every outbound call in a decorator the
framework controls, which only works if the framework knows what "outbound"
means — and for an MCP tool, a sandbox command, or a device call it does not.
A contract the suite checks is weaker than a type, and stronger than the
current design, which has no mechanism at all.

**Alternative:** make the executor write the watermark unconditionally before
step 10, which is safe but marks every call, so every interrupted call becomes
`UNCERTAIN` again and the column buys nothing.

**Reversal cost:** cheap now, moderate after tools exist.

### `UNCERTAIN` is reserved for non-idempotent calls with a set watermark

**Decided:** the recovery table re-executes everything else, including a
non-idempotent call whose watermark is `NULL`.

**Why:** it is the whole point of the column. The risk is a tool that performs
an effect before calling the method, which is a contract violation the suite
catches, versus the current behaviour where every crash during a consequential
call reaches a person.

**Reversal cost:** cheap — deleting one row of the table restores today's
pessimism.

### A batch of parallel tool calls admits or rejects as a whole

**Decided:** if any call in a batch fails the five conditions, the entire batch
runs sequentially. No splitting into a parallel read group and a sequential
write group.

**Why:** splitting requires deciding that no read in the first group depends on
a write in the second, and Section 12.4's closing sentence is a warning against
exactly that inference. The cost is latency on mixed batches.

**Reversal cost:** cheap.

### Operators classify MCP servers, and an unclassified server is maximal risk

**Decided:** `side_effect`, `risk`, `idempotency`, and `required_scopes` are
per-server operator configuration. The default is `EXTERNAL_WRITE`, `HIGH`,
`NON_IDEMPOTENT`.

**Why:** MCP has no field for these, and a field would be a claim by the party
the classification constrains. Per-server rather than per-tool because the
configuration surface is a place to move slowly.

**Alternative:** per-tool overrides, which is one more column and is the obvious
next step once a real server makes the coarseness hurt.

**Cost of this choice:** onboarding an MCP server is now a deliberate act with a
classification step, and a server with one destructive tool makes its nine
harmless ones expensive. This will be felt as friction.

**Reversal cost:** cheap.

### Tenant-configured MCP servers are HTTP-only; stdio is operator-only

**Decided:** a tenant may not name a stdio command.

**Why:** a stdio server is a child process of the worker, so a tenant-supplied
command line is remote code execution in the worker's trust zone. This one is
not really a judgement call, but it is a restriction Milestone 8 does not state
and someone will ask why their local server will not connect.

**Reversal cost:** none; it should not be reversed.

### MCP resources become a tool, not a context source

**Decided:** one synthetic `mcp.{server}.read_resource` per server with a
resources capability. Milestone 8 says to map resources to "the context-source
abstraction", and this maps them to the tool abstraction instead.

**Why:** an automatic context source puts externally controlled text into
assembled context on a schedule the platform does not control, in the region
ADR-0020 keeps stable. Making the model ask means the request passes policy,
size limits, and trust labelling like anything else. This is the one place this
document knowingly reads a milestone line differently from its literal wording,
and it is flagged here for that reason.

**Alternative:** implement the context-source mapping with a per-server
allowlist of URIs. More faithful to the milestone text, more surface.

**Reversal cost:** moderate.

### Sampling and roots are declined at capability negotiation

**Decided:** the client does not advertise them, so a server requiring them
fails to connect.

**Why:** sampling is an external party spending a tenant's model budget on a
prompt the platform did not compose. Declining at negotiation rather than at
request time means a server never gets to try.

**Reversal cost:** cheap for roots, expensive for sampling — it would need its
own budget, policy, and context rules.

### A server's catalog change is recorded and not applied mid-session

**Decided:** `tools/list_changed` emits `mcp.catalog.changed` and does nothing
else until the next session opens.

**Why:** applying it hands an external server the ability to invalidate a
tenant's byte-stable prefix at will, which is unbounded cost imposed by a third
party plus a cache-timing channel. It generalizes ADR-0020's existing rule.

**Cost:** a genuinely new tool is unavailable until the next session.

**Reversal cost:** cheap.

### The excerpt split is 60% head, 20% tail

**Decided:** a truncated result shows the first 60% and last 20% of the byte
budget with an explicit elision marker, and the whole output becomes an
artifact.

**Why:** most large tool outputs are logs whose beginning says what ran and
whose end says whether it worked. It is a guess and the eval harness should
measure it.

**Reversal cost:** cheap.

### `approval_hold_seconds` defaults to 300

**Decided:** the bridge blocks for up to five minutes on an in-script approval,
then tears the sandbox down and re-executes the script from the start on
resume, deduplicating the calls that already ran.

**Why:** long enough for an approver at their desk, short enough that a held
sandbox is not a leak. Per-tenant configurable.

**Reversal cost:** cheap.

### Replay safety for orchestration scripts is conditional on determinism

**Decided:** stated as a caveat in the document rather than engineered around.

**Why:** a script that branches on the clock or on changed tool output issues a
different call, derives a fresh key, and executes for real — which is correct,
but it means replay does not guarantee no duplicate work, only no duplicate
identical work. Engineering around it would mean recording and replaying the
script's own control flow, which is a much larger project than the bridge.

**Reversal cost:** n/a — this is a documented limit, not a decision to undo.

### Events carry classification; rows carry payload

**Decided:** no tool event contains arguments, results, error detail, or
external text.

**Why:** the event stream is replayed to SSE consumers, retained on a different
schedule, and exported to observability stacks. Tenant data in three places with
three retention policies is a compliance problem waiting to be discovered.

**Cost:** the SSE stream stops being useful for debugging a failing tool call.

**Reversal cost:** cheap now, expensive once events are retained.

### No `tool_registry_snapshots` table

**Decided:** the context plan's `tool_names` and `tool_schema_sha256`, plus the
`mcp_tool_catalog` history, answer what a session advertised.

**Why:** a third record of the same fact is a third place for it to disagree.

**Reversal cost:** cheap.

## Evaluation harness (ADR-0001, ADR-0022)

### ADR-0001 was written now, and the runtime-loop ADR moves to 0023

**Decided:** `docs/adr/0001-modular-monolith.md` was written as part of this
task rather than left as a gap, and the evaluation harness took ADR-0022. The
runtime-loop ADR planned as 0022 becomes ADR-0023.

**Why:** Section 4's repository layout names `0001-modular-monolith.md`, the ADR
index carried an apologetic paragraph explaining its absence, and twenty ADRs
referred to a foundational decision documented nowhere. It also had real content
to add rather than being a formality: Section 5 closes its fourteen dependency
rules with *"verify these constraints where practical"*, and the harness spec's
structural-gate mechanism is what lets "where practical" be resolved rule by
rule. Writing it alongside the harness is what made it worth writing at all.

**Alternative:** fold the monolith decision into ADR-0022 and leave 0001 unused.
Rejected because the two decisions have different lifetimes — the architecture
outlives any particular harness — and a permanently skipped 0001 invites the
question forever.

**Reversal cost:** cheap now; the renumbering touches two files. Expensive once
anything outside this repository cites ADR-0023.

### The harness is treated as larger than the case runner

**Decided:** Section 20's harness is read as including a gate registry, property
gates, corpus gates, and structural gates, not only the YAML case runner it
describes.

**Why:** the seven specs declare roughly forty-nine hard gates and name Section
20 as their enforcer, but Section 20's sixteen assertion types can express
perhaps eight of them. Either the gates are decoration or the harness is bigger
than the case format. Nothing in Section 20 was changed; four gate kinds were
added around it.

**Cost:** the harness becomes a Milestone 0 deliverable rather than a Milestone
1 one, and it is more machinery than the plan implies.

**Reversal cost:** cheap as prose. Expensive as a decision, because the
alternative reading makes six spec documents' closing sections unenforceable.

### Roughly a third of the declared gates are not case gates

**Decided:** each spec's gates were sorted into the four kinds, and the
per-spec counts are published in the harness document as a table.

**Why:** the counts are the useful output, not the individual assignments. They
say which harness facility each spec needs, and they say that a case-only
harness would report green with a third of the plan's stated invariants
unchecked.

**Caveat worth your eye:** the individual assignments are a judgement call in a
handful of cases, particularly where a gate could be written either as a
parameterized case sweep or as a property. The counts are robust to those; the
row-level assignments are not authoritative.

**Reversal cost:** cheap.

### There is no test mode

**Decided:** no environment variable, no configuration flag, and no
`if settings.testing` branch in the policy engine, the approval service, or the
tool executor. Evaluation identity is data: a `tenant_eval` tenant created by an
ordinary migration, named principals, and policy profile files loaded by the
production loader and subject to the same totality gate.

**Why:** a flag that disables the gate is a code path in the shipped binary and
is therefore reachable from production. ADR-0005's third gate says exactly one
function performs the `PROPOSED` to `AUTHORIZED` transition, and a test bypass
would be a second one.

**Cost:** setting up an evaluation is more work than flipping a flag, and the
production loader gains a startup check that must be maintained.

**Reversal cost:** cheap now. Expensive after Milestone 4, since the alternative
is a bypass that would then have to be removed from working code.

### The eval tenant is created by a migration in every deployment

**Decided:** unconditionally, in production too, with the production loader
check as the thing standing between an eval profile and a production process.

**Why:** a migration that runs only outside production introduces a schema
difference between environments, which is its own class of bug and a
particularly annoying one to diagnose.

**This is the open question most worth your answer.** The alternative is
defensible and I do not have a strong view. Recorded as open question 1 in the
harness document.

**Reversal cost:** cheap now, moderate after the migration ships.

### `interventions` was added to the case schema

**Decided:** six of them — approve, deny, cancel, kill the worker, answer, and
disconnect — fired at named points in a run.

**Why:** Section 20.3 requires cases for approval requested, approval granted
and resumed, approval denied, cancellation, restart after checkpoint, restart
after an idempotent success, the ambiguous non-idempotent call, and SSE replay
after disconnect. None of them is writable in a format whose only input is the
opening user message. This is an addition to Section 20.1's format, not a change
to it; every field it already had is unchanged.

**Reversal cost:** cheap.

### Case 18 was split into 18a and 18b

**Decided:** kill before the watermark asserts re-execution; kill after asserts
`UNCERTAIN`, a human-review row, and that the model is told the outcome is
unknown rather than that the call failed.

**Why:** the case was untestable while "ambiguous" was an undefined word, and
ADR-0021's `effect_sent_at` made it two cases. Only the second has a safety
consequence — telling a model a non-idempotent write failed is the fastest route
to a duplicate write.

**Note:** this makes the count twenty-six, not twenty-five. Section 20.3 says
"at least these twenty-five", so the floor is respected.

**Reversal cost:** cheap.

### `resilience` was named as the sixth test category

**Decided:** Section 20.4 lists five categories; a sixth is named here.

**Why:** Section 4's repository layout already has `tests/resilience/`, the
event-log spec places its kill-the-worker recovery test there, and the tool
system spec calls its crash-recovery gate "a resilience test". The category
existed in the layout and in two specs' expectations; only the list omitted it.
This is reconciliation, not a new requirement.

**Reversal cost:** cheap.

### The capability track defaults to five repeats

**Decided:** five, not one.

**Why:** a single run of a stochastic system against a rubric produces a number
of unknown variance, and comparing two such numbers across a release is how an
evaluation programme generates false alarms until people stop reading it.

**Cost:** five times the track's spend, and the number is chosen for variance
estimation rather than measured against this suite's actual spread.

**Reversal cost:** cheap — it is a default. Recorded as open question 2.

### A ceiling hit is excluded from the score, not scored zero

**Decided:** a scenario that exhausts its cost ceiling records `ceiling.hit`,
is excluded from the distribution, and is counted separately.

**Why:** it conflates "we stopped paying" with "the agent failed", and it does
so in the direction that corrupts the distribution most. A rising ceiling-hit
rate is its own signal, usually of the agent becoming less efficient rather than
less capable.

**Reversal cost:** cheap.

### Judges are versioned as a unit and cross-version comparison is refused

**Decided:** a judge is a model, a prompt, and a rubric under one identifier,
pinned to a provider version, replaced only alongside a bridge run publishing a
calibration offset, never reusing an identifier after deprecation. The tooling
refuses to compare scores across judge versions rather than footnoting them.

**Why:** a judge change and a subject change are indistinguishable in the
resulting score. A footnote is not read at the moment the comparison is made.

**Cost:** the first published score waits for judge governance to exist.

**Reversal cost:** cheap now; expensive after scores are published, since the
comparability of the historical series depends on it.

### Trajectory conversion replays tool results rather than re-executing tools

**Decided:** each recorded tool result becomes a fixture keyed by tool name and
normalized argument hash; a converted case that reaches a call with no recorded
result fails with `fixture.missing_tool_result`.

**Why:** the real run called a real tool against a real system, and the case must
not. The failure is the correct outcome — it means the model behaved differently
from the recording, and the case has nothing to say about what happens next.

**Reversal cost:** cheap.

### A converted case needs a human to write its assertions

**Decided:** it carries `source: trajectory` and does not enter the blocking
suite until reviewed.

**Why:** auto-generated assertions assert whatever the system did on the day it
was recorded, including its defects. There is no worse outcome for a regression
suite than pinning a bug as expected behaviour.

**Cost:** the conversion is not push-button, which is most of its appeal.

**Reversal cost:** cheap.

### Quarantine expires after fourteen days whether or not the test was fixed

**Decided:** flaky tests are retried once with the retry rate reported even
while green, quarantined on a second failure within thirty days, and released
from quarantine automatically after fourteen days. Gates may never be
quarantined.

**Why:** a quarantine with no expiry is a delete with extra steps, and the tests
that end up there are disproportionately the ones covering concurrency and
recovery — exactly the ones worth fixing.

**Cost:** a build that goes red on a schedule rather than on a change, which is
annoying by design.

**Reversal cost:** cheap.

### The deterministic suite adds no tables

**Decided:** the capability track gets `eval_scenario_runs` and
`eval_criterion_scores`; the deterministic suite persists nothing beyond the
ordinary event log it already writes as `tenant_eval`.

**Why:** the suite's output is a CI result, and a table would be a second record
of it that can disagree with the first.

**Reversal cost:** cheap.

### Gate 7 is asserted by blocking egress, not by not configuring a key

**Decided:** the definition of done's eighteenth item — a deterministic suite
that runs in CI without an API key — is asserted by running the integration job
with network egress blocked.

**Why:** "without requiring an API key" is usually implemented as "we did not
configure one", which stays true until a fixture falls through to a real client
and the failure is a confusing timeout rather than a clear one.

**Cost:** the CI job needs a network policy, which is a small amount of
infrastructure work in Milestone 0.

**Reversal cost:** cheap.

## Runtime loop (ADR-0023)

### The loop was split into a loop and an executor

**Decided:** `run_loop` computes a `RunOutcome` and performs no terminal action;
a single `finalize` in `runtime/executor.py` does every transition, lease
release, and terminal event append, for all five outcome kinds. A structural
gate asserts that `RunRepository.transition` and `RunQueue.release` are
reachable from exactly one module.

**Why:** Section 12.1's suspension path returns bare, which under Section 27.2
leaks the lease of every run that pauses for approval. The minimal fix is a
`finally`, which closes today's paths and leaves the next `return` free to
reopen the hole, and offers nothing to gate against.

**Cost:** a run's ending is spread over two modules that must be read together.

**Reversal cost:** moderate. Inlining `finalize` back into the loop is
mechanical; the gate would have to be retired with it.

### `Step` became a value object with a column, not a table

**Decided:** `model_calls` gains `step_number INTEGER NOT NULL`; there is no
`steps` table.

**Why:** every step makes at least one model call, so a `steps` table would
duplicate `model_calls` row for row and need a consistency rule against it. The
column also makes steps that produce no tool calls visible, which they are not
today.

**Reversal cost:** cheap before Milestone 2; a migration after.

### Nine fields were added to `Run`

**Decided:** `tenant_id`, `agent_id`, `agent_version`, `lease_epoch`,
`attempts`, `priority`, `scheduled_for`, `deadline_at`, `failure`.

**Why:** six are columns the event-log spec already introduced and the domain
model never reflected, so this is bookkeeping. Three are new: `agent_id` and
`agent_version` because Section 12.1 reads them off `Run` and Section 6.3 puts
them on `Session`, and `deadline_at` because `RunLimits` declares it as a value
and the lease sweep needs it as an indexable column.

**Reversal cost:** cheap; they are additive and nothing reads them yet.

### The agent version is pinned at run creation

**Decided:** `agents.get_version` is called once, when the run row is created,
and the result is never re-resolved — including across a suspension that lasts
days.

**Why:** an approval is granted against a specific agent's proposed action. A
deploy landing between the request and the approval would otherwise change what
the approver authorized.

**Cost:** a long-paused run resumes on an agent version that may since have been
withdrawn. The alternative is worse.

**Reversal cost:** cheap.

### A child-run wait reuses `WAITING_FOR_APPROVAL`

**Decided:** suspension has three kinds — approval, user, child run — and the
third maps to `WAITING_FOR_APPROVAL` rather than to a new `WAITING_FOR_CHILD`
status. Recorded as open question 2 in the spec.

**Why:** the wait behaves identically in every respect that the state machine
cares about — lease released, no worker slot, resumed by an external event — and
a fourth non-terminal status means amending four documents that enumerate
`RunStatus` exhaustively.

**Cost:** an operator looking at a stuck run sees a status that does not mean
what it says. This is the decision in this document least likely to survive
Andy's review, which is why the suspension kind is a typed field rather than
inferred.

**Reversal cost:** moderate, and rising. Cheap now; expensive once the status
has API consumers.

### Cancellation was split across three milestones

**Decided:** the token and the loop's observation points land in Milestone 1,
the tool-executor points in Milestone 4, and the API surface and sandbox
propagation in Milestone 5, where Section 21 places cancellation.

**Why:** Milestone 5's acceptance criterion is "cancellation reaches the
worker", which is the API half. The token is three lines and threading it later
means threading it through code that assumed it did not exist.

**Cost:** a partially useful mechanism exists for four milestones.

**Reversal cost:** cheap to collapse back into Milestone 5; expensive to
introduce late, which is the asymmetry the decision rests on. Recorded as open
question 1.

### "After every operation" was read as "record in the same transaction"

**Decided:** budget has three scopes — run, step, attempt. Section 6.5's *"check
limits before and after every model or tool operation"* is implemented as a
check before and a *record* after, where recording usage and evaluating the
limit are one operation in one transaction.

**Why:** a literal second check after the operation and a separate write leaves
a window in which the run is over budget and no query would say so. ADR-0002's
per-attempt rule and Section 6.5's per-operation rule are then both satisfied
without a third granularity.

**Reversal cost:** cheap.

### The heartbeat also owns the deadline and the cancellation poll

**Decided:** one supervisor task at a third of the lease interval renews the
lease, checks `deadline_at`, and polls for a cancellation request.

**Why:** three timers and three queries for three questions that are all "has
the outside world changed its mind", on a run that is otherwise blocked on a
provider stream.

**Cost:** cancellation latency is bounded by the heartbeat interval rather than
being immediate.

**Reversal cost:** cheap.

### A fenced worker aborts its in-flight stream

**Decided:** on `heartbeat` returning `False`, the worker cancels the model
stream, appends exactly one `run.fenced` event, and writes nothing else.

**Why:** nothing the stream produced could be committed under a stale epoch, so
finishing it spends tokens on an uncommittable result. The single append is
legal because the event log is sequence-guarded rather than epoch-guarded.

**Cost:** the output that would have explained the stall is lost. Recorded as
open question 6.

**Reversal cost:** cheap.

### Compaction was capped at two attempts per step

**Decided:** `build_with_pressure` measures, compacts, and measures again, up to
`MAX_COMPACTIONS_PER_STEP = 2`, then raises a permanent `ContextOverflow`.

**Why:** an uncapped loop against a compactor that cannot shrink the body — a
single tool result larger than the budget — spins on a model call per attempt.
Two is enough for the case where one pass narrowly misses.

**Reversal cost:** cheap; it is a constant.

### The full-snapshot interval is every eighth checkpoint

**Decided:** a checkpoint is full at version 1, every eighth version, and on
compaction, suspension, and termination.

**Why:** it bounds delta reconstruction at seven reads. The number is otherwise
arbitrary and is recorded as open question 4.

**Reversal cost:** cheap.

### The evaluation harness's tool event names were corrected

**Decided:** the approval case's `event_order` now reads `tool.call.proposed`,
`tool.call.authorized`, `tool.call.completed`. It previously read
`tool.proposed`, `tool.authorized`, `tool.succeeded`.

**Why:** those three names are not events. Section 6.8 declares the `tool.call.*`
family and ADR-0021 adds no tool event, so the case as written would have failed
against a correct implementation. This is a correction to a document written
during this run, not a change to a requirement of Andy's.

**Reversal cost:** none; it was a defect.

### 27.5's "reject or queue" resolves to reject

**Decided:** a message to a session with an active run returns
`ConflictError` and HTTP 409, except where the active run is
`WAITING_FOR_USER`, in which case the deterministic routing rule sends the text
to that run's input endpoint.

**Why:** Section 27.5 permits either, but ADR-0004's partial unique index on
non-terminal runs per session makes queueing impossible at the database level as
currently specified. Choosing "queue" would silently require a different
constraint.

**Cost:** a client that submits during a long run gets an error rather than a
promise.

**Reversal cost:** moderate; queueing later costs a migration rather than a
redesign.

### The maintenance sweeps run everywhere under advisory locks

**Decided:** lease expiry, approval expiry, and deadline enforcement run in
every worker process, each guarded by a PostgreSQL advisory lock, rather than in
a dedicated reaper deployment.

**Why:** a singleton is an operational burden and a single point of failure for
three time-based obligations, one of which is a safety property.

**Cost:** every node does a little wasted work every interval. Recorded as open
question 5.

**Reversal cost:** cheap.

### An empty terminal turn is retried rather than returned

**Decided:** a model turn with no content and no tool calls is treated as a
failed step, retried under Section 13's rules, and fails the run with
`EmptyModelTurn` on exhaustion.

**Why:** returning it completes the run with an empty final message, which is
indistinguishable to a user from the agent having nothing to say.

**Cost:** a second context assembly on a model that just produced nothing.
Recorded as open question 3.

**Reversal cost:** cheap.

### This document adds no event types

**Decided:** the spec consolidates the fourteen run-lifecycle events introduced
elsewhere and assigns owners to the three that had none — `run.claimed` and the
two `run.waiting_*` events — without introducing any.

**Why:** the event vocabulary is a compatibility surface with the projections,
the SSE transport, and the trajectory export. Adding to it from the last spec
written would be the least reviewed change in the corpus.

**Reversal cost:** not applicable.

## Deferred questions from earlier specs, now decided

These three were left explicitly open by specs written earlier in this run. The
mandate was to decide rather than to leave the plan with holes in it, so each is
decided, recorded here, and reflected in the spec that raised it.

### The compaction summarizer resolves through `ModelRouter` under a `compaction` policy

**Decided:** the summarizer's model is not fixed in the context-engine spec and
not chosen at the call site. It resolves through `ModelRouter` under a named
`compaction` model policy, defaulting to the run's own provider at the cheapest
tier whose context window admits the region being summarized. The prompt is a
versioned asset and its version is recorded on the checkpoint compaction writes.

**Why:** defaulting to the run's provider keeps ADR-0002's provider pinning
intact — a compaction mid-run would otherwise send conversation content to a
second vendor without anything having decided that. Recording the prompt version
on the checkpoint is what makes a compaction-fidelity regression attributable to
a prompt change rather than to a model change; without it the eval can detect the
regression and not locate it.

**Cost:** one more model policy to configure, and a prompt-asset versioning
convention that did not otherwise exist.

**Reversal cost:** cheap. The tuning question the spec deferred is still
deferred to the fidelity eval; only the location of the choice is fixed.

### Skill bodies go in Region B and are sticky for the session

**Decided:** Section 30.4's skill instructions are assigned to Region B, and a
selected skill's body stays in the prefix for the remainder of the session unless
a control tool deselects it.

**Why:** Region B was already the right region on volatility grounds; the
objection the spec recorded was the caching consequence of a large body in a
mid-session layer. Stickiness answers it directly. A body that entered and left
on alternating steps would invalidate the cached prefix on each of them, which
costs more than carrying an unused body for the rest of the session.

**Cost:** a session that touches many skills accumulates their bodies until the
budget forces compaction. The class caps bound this; it is not unbounded.

**Reversal cost:** cheap until skills ship in Milestone 8.

### The temporal entity graph is post-0.1, not an open question

**Decided:** it is recorded as post-0.1 work with nothing in Milestones 0 through
10 depending on it, rather than left as an unscheduled open question.

**Why:** Milestone 9 delivers beliefs with scopes, provenance, decay, and
contradiction handling, and that is what the retrieval path consumes. An entity
graph with valid-time and transaction-time edges is a second storage model over
the same evidence, and a second model needs a consistency rule against the first.
Leaving it "not yet specified" invites an implementer to build a partial version
of it inside the belief store, which is the outcome worth preventing.

**Cost:** temporal queries — "what did I believe about X in March" — are not
answerable in 0.1 beyond what belief history gives.

**Reversal cost:** cheap; it is additive whenever it is taken up.

## Cross-document defects found at readiness review (2026-07-25)

### `RunStatus` is seven members and there is no `WAITING_FOR_CHILD`

**Decided:** `RunStatus` is a `StrEnum` with `QUEUED`, `RUNNING`,
`WAITING_FOR_APPROVAL`, `WAITING_FOR_USER`, `COMPLETED`, `FAILED`, and
`CANCELLED`, values equal to their names in uppercase. A run blocked on a child
run waits in `WAITING_FOR_USER` with a suspension record naming the child.

**Why:** the type was referenced by five documents and declared by none, which
is the defect a coding agent hits within the first hour of Milestone 1. Seven
is the smallest set that covers every transition `finalize` can perform. An
eighth state for child runs was rejected because nothing outside the suspension
record distinguishes it: the queue treats all three waiting states identically,
and the resume path already dispatches on suspension kind rather than status.

**Cost:** a query for "runs blocked on a child" reads the suspension record
rather than filtering on status alone.

**Reversal cost:** moderate. Adding a state after Milestone 2 means a migration
plus every `status IN (...)` predicate, of which the partial index below is one.

### `runs` gains four columns the domain fields always implied

**Decided:** `tenant_id UUID NOT NULL`, `agent_id UUID NOT NULL`,
`agent_version TEXT NOT NULL`, `deadline_at TIMESTAMPTZ NULL`, and a partial
index on `deadline_at` restricted to the three live statuses.

**Why:** every one of these is read by code the plan already requires — tenant
scoping on every query, the agent version pinned for reproducibility, the
deadline sweeper — and none of them had a column. `deadline_at` is nullable
because a run without a deadline is normal; the other three are not, because a
run without a tenant is not a run.

**Reversal cost:** moderate; it is a migration once Milestone 2 has shipped.

### The step's identity is `model_calls.step_number`, not a new column

**Decided:** the canonical step identity is the existing
`model_calls.step_number`. `tool_invocations.step_number` remains a foreign
reference to it, not a second source of truth.

**Why:** an earlier passage in `runtime-loop.md` claimed `step_number` lived
only on `tool_invocations`, which the model gateway spec had already falsified.
Only `model_calls` is written for every step: a step that proposes no tool calls
writes no invocation row at all, so numbering from `tool_invocations` would skip.
Choosing the column that always exists means the identity is chosen rather than
invented.

**Reversal cost:** cheap. Both columns already exist; this fixes which one the
prose points at.

### `tool_invocations.origin_trust` is `NOT NULL`

**Decided:** the column is `NOT NULL`. A call the runtime issues itself — a
control tool, a maintenance sweep — carries `PLATFORM`.

**Why:** `tool-system.md` declared it nullable and `policy-and-approvals.md`
declared it `NOT NULL`, and the authorization record is the wrong place to leave
that ambiguous. A nullable column would mean "policy did not compute the origin
trust", which is the one thing an authorization row must never be able to say.
Every path that writes the row runs after a policy decision, so a value always
exists.

**Reversal cost:** cheap now, moderate after Milestone 4.

### The column is `idempotency_class`, not `idempotency`

**Decided:** `tool_invocations` carries `idempotency_class TEXT NOT NULL`.
`ToolSpec.idempotency` and `mcp_servers.idempotency` keep their existing names.

**Why:** the same table already carries `idempotency_key`, and two columns
differing by one suffix is exactly the confusion crash recovery cannot afford —
the recovery path reads one to decide whether replaying is safe and the other to
decide whether it is the same call. The field on `ToolSpec` sits in a different
namespace where no key is present, so renaming it would cost clarity rather than
buy any.

**Reversal cost:** cheap; the table is not yet created.

### The event catalog is fifty-one types and is now closed

**Decided:** `runtime-loop.md` carries the full catalog: the twenty-four named
in Section 6.8 plus twenty-seven introduced by later specs, including the seven
MCP and bridge events from `tool-system.md` and the six memory events from the
formation and retrieval specs. Nothing new is introduced by the runtime loop
itself.

**Why:** the consolidated list had gone stale at twenty-four while five specs
added to it, so a reader had no single place to learn the vocabulary and no way
to notice a name being coined twice. Stating the total makes the next addition
visible: a spec that adds an event now has to move the count.

**Cost:** the catalog is a second place to edit when an event is added.

**Reversal cost:** cheap.

### The harness asserts `run.queued` and `approval.resolved`

**Decided:** the `approval_granted_resumes_run` case's `event_order` asserts
`run.queued` and `approval.resolved`, replacing `run.created` and
`approval.granted`.

**Why:** neither replaced name is in the catalog. A golden case that asserts a
non-existent event name fails on the day it is first run, and the failure looks
like a runtime bug rather than a typo in the fixture. `approval.resolved`
carries the outcome in its payload, which is why one name covers grant and deny.

**Reversal cost:** cheap.

### The gate id is `gate.policy.prompt_is_not_authorization`

**Decided:** the long registry spelling is canonical; the short
`gate.policy.prompt_not_auth` used in one illustrative CLI listing was wrong and
the listing was reflowed to fit the real id.

**Why:** gate ids are matched exactly by the harness and quoted in CI output, so
a second spelling is a gate that silently never runs. The registry spelling wins
because it is the one the harness loads.

**Reversal cost:** cheap.

### The plan's superseded port signatures are annotated, not deleted

**Decided:** `RunRepository.claim_next` and `RunRepository.heartbeat` stay in
Section 7 of `engineering-plan.md`, with a paragraph above them recording that
ADR-0023 supersedes both with `RunQueue.claim` and `RunQueue.heartbeat`, and
that `RunQueue` is what gets implemented.

**Why:** the standing instruction is that conversion is additive and existing
requirements are not rewritten. Deleting the two signatures would be the
cleanest-looking fix and would also destroy the record of what was replaced,
which is the part a reviewer needs. Annotating leaves the plan readable as
history and unambiguous as instruction.

**Reversal cost:** cheap.

### Two citations pointed at the wrong subsection

**Decided:** two references in `model-gateway.md` that cited "Section 10.3" for
the normalized request now cite Section 10.1. The two other 10.3 references in
the same file are correct — they point at the fake provider — and were left
alone.

**Why:** §10.1 is Normalized request and §10.3 is Fake provider. A wrong
cross-reference in a spec that is meant to be copy-ready sends the implementer
to the wrong requirement.

**Reversal cost:** cheap.

## Bootstrap and composition (ADR-0024)

### `DATABASE_URL` is invented

**Decided:** `Settings.database_url` reads `DATABASE_URL`, a
`postgresql+asyncpg://` DSN, required in every deployment and validated
before anything is constructed.

**Why:** the plan makes PostgreSQL the source of truth, ships a Docker
Compose file, and requires `make migrate` — and names no environment
variable for reaching the database. It names exactly three environment
variables in total, none of them this one. Something has to carry the
connection string, it fails the "can be committed" test, and every
alternative I could see was worse: a committed YAML file with a password
in it, or a hostname/port/user/password quartet that has to be
reassembled. `DATABASE_URL` is what SQLAlchemy, Alembic, and the compose
file already expect.

**Reversal cost:** cheap. It is one field and one name.

### `.env.example` is not a list of all 106 configuration knobs

**Decided:** the Milestone 0 definition-of-done item "New configuration
appears in `.env.example`" is read as applying to the environment layer
only. A value is an environment variable if and only if it differs
between two deployments of the same revision and cannot be committed.
That test leaves eight fields in `Settings`; the other knobs live in
committed YAML.

**Why:** the literal reading makes all 106 knobs environment variables,
which contradicts `policy_version` — defined as a hash of the profile
file and the hardline file and recorded on every policy decision. If an
environment variable could change an effective rule, that hash would not
change with it, and the audit trail would be false rather than merely
stale. The literal reading also contradicts Section 15's own sentence
that policy rules are version-controlled files, not rows. This is the
single largest interpretive decision in the document and the one most
worth confirming.

**Cost:** changing a knob for one deployment now means committing a file
or adding a named interpolation point.

**Reversal cost:** moderate. Widening the environment layer later is
additive; narrowing it after operators depend on an override is not.

### The environment never overrides a file — it is interpolated into one

**Decided:** configuration is three layers: shipped defaults, an optional
operator overlay directory, and `${VAR}` interpolation at points the YAML
names explicitly. Only the first two are a precedence chain. There is no
`AGENT__POLICY__DEFAULT__X=1` style override path.

**Why:** Section 10.5 already writes `model: ${OPENAI_MODEL}`, so the
interpolation form is the plan's own precedent; this generalizes it and
forbids the other. The conventional twelve-factor arrangement, where the
environment wins over files, is the specific thing that would make
`policy_version` unfalsifiable. Interpolation keeps the secret out of the
file while leaving the file the thing that gets hashed.

**Reversal cost:** high. This is the decision the audit story rests on.

### The overlay merges by top-level key, not deeply

**Decided:** `AGENT_CONFIG_DIR` is merged file by file over the shipped
defaults, and within a file, by top-level key. An overlay that defines
`model_policies` replaces the whole mapping rather than merging into each
policy.

**Why:** deep merge makes the effective document hard to predict from the
two inputs, and the effective document is what gets hashed. Replacing a
whole top-level key is legible in a diff. The cost is verbosity when an
operator wants to change one nested value.

**Reversal cost:** moderate — deep merge is a superset, but overlays
written against shallow merge would then change meaning.

### Configuration YAML lives beside the package that owns it

**Decided:** six files under `src/agent_core/`, each next to the module
that reads it, rather than a top-level `config/` directory.

**Why:** the plan set exactly one precedent, `policy/hardline.yaml`, and
it is beside its package. A top-level `config/` would also collide
confusingly with the `config.py` module Section 4 names, and it separates
a default from the code that reads it, which is the arrangement where the
two drift.

**Reversal cost:** cheap.

### `hardline.yaml` is the one file the overlay may not touch

**Decided:** an overlay that contains `policy/hardline.yaml` is a startup
error, not a silent no-op.

**Why:** the hardline set is defined as what configuration cannot
disable, and an overlay is configuration. A silent no-op would let an
operator believe they had changed something. Failing loudly at startup is
the only behaviour consistent with the definition.

**Reversal cost:** cheap.

### The composition root never runs migrations

**Decided:** startup asserts the schema revision matches and refuses to
start on a mismatch. `make migrate` stays a separate step.

**Why:** migrating on boot means N processes racing during a rolling
deploy, and a process that failed to start having already changed the
schema. Section 25 already treats migration as its own step. The cost is
one more command in local setup, which the Makefile already has.

**Reversal cost:** cheap.

### The deployment role is an entry-point argument, not a variable

**Decided:** `runtime/worker.py` passes `role="worker"` or
`role="maintenance"`; `api/main.py` and `cli/main.py` pass `role="api"`.
The role is not in `Settings`.

**Why:** `runtime-loop.md` says all three deployment roles are the same
binary with a role flag, and a flag on the entry point is what makes a
process's role visible in the process table rather than in its
environment. It also keeps `Settings` to values that are genuinely
per-deployment; the role is per-process.

**Reversal cost:** cheap.

### Four CLI options are added where the plan names none

**Decided:** `--json`, `--session <id>`, `--role <role>`, and `--follow`.

**Why:** Section 17 lists twelve commands and no options at all, which
reads as a command inventory rather than a complete grammar. Each of the
four is added because a command is otherwise unusable rather than merely
less convenient: without `--session` there is no way to continue a
conversation, without `--role` `agent worker` cannot start the
maintenance role the runtime spec requires, without `--follow` the events
command can only poll, and without `--json` nothing that calls the CLI
can parse it. If the plan's silence was deliberate minimalism rather than
omission, this is the entry to reverse.

**Reversal cost:** cheap, but the four are load-bearing in the milestone
walkthroughs.

### `get`, `events`, and `cancel` are reserved after `agent run`

**Decided:** `agent run get <id>` and `agent run "get the weather"` are
disambiguated by treating the three subcommand names as reserved words,
with `--` as the escape: `agent run -- get the weather`.

**Why:** the plan gives both forms — `agent run "Calculate 12 times 9"`
and `agent run get <run-id>` — and never says how they are told apart. A
prompt is free text and can begin with any word. Reserving three words is
the smallest rule that makes the grammar decidable, and `--` is the
convention every POSIX tool already uses for it.

**Reversal cost:** cheap. The alternative is a separate `agent prompt`
verb.

### The secret scanner is specified rather than left to a tool choice

**Decided:** five rule families — provider key prefixes, PEM blocks,
literal bearer values, DSNs with inline passwords, and secret-named
assignments over twelve characters — plus a report that never prints what
it matched and an allowlist whose entries require prose.

**Why:** dependency rule 12 and the Milestone 0 deliverable both name a
scanner and neither says what it looks for, which makes "the check
passes" unfalsifiable. Naming the families also fixes the thing a scanner
most often gets wrong, which is printing the secret it found into CI
logs. `.env.example` is scanned rather than exempted, because an example
file is exactly where a real key gets pasted by accident.

**Reversal cost:** cheap.

### Transaction hygiene: the check is Milestone 0, the gate is Milestone 2

**Decided:** the two placements are not in conflict once *check* and
*gate* are separated. The check ships in Milestone 0 with the other
structural checks; the gate that asserts it passes over real repository
code is a Milestone 2 acceptance criterion.

**Why:** the plan puts transaction hygiene in Milestone 0 and the runtime
spec tags its gate Milestone 2. Milestone 0 has no database code to walk,
so a Milestone 0 gate would pass vacuously; a Milestone 2 check would be
added against code that already violates it. Shipping the check early and
gating on it later is what both documents are each half-saying.

**Reversal cost:** cheap.

### An event repository is Milestone 1; event storage is Milestone 2

**Decided:** Milestone 1's criterion that every state transition is
represented by an event is satisfied by an in-memory `EventRepository`
behind the same port. Milestone 2 adds the PostgreSQL adapter with
append-only guarantees, sequence allocation, and projections.

**Why:** the two milestones appear to contradict each other only if
"event storage" and "event repository" are the same thing. One port with
two adapters is the arrangement the whole plan is built on, and it is
what makes Milestone 1's vertical slice real rather than mocked.

**Reversal cost:** cheap.

### In-memory repositories are adapters, and there is no in-memory queue

**Decided:** five in-memory adapters in
`adapters/persistence/memory.py`, shipped as production code and run
against the same contract suites as their PostgreSQL counterparts, each
declaring which capability groups it satisfies against a checked-in
table. `RunQueue` gets no in-memory adapter.

**Why:** ADR-0001 defines replaceability as a port with a contract suite
attached; a double under `tests/` would be a second, unverified
definition of what the port means. The `RunQueue` exception is the
inverse of the same argument — that port's entire content is `FOR UPDATE
SKIP LOCKED` and lease fencing, so an in-memory version would pass its
own tests while teaching the wrong lesson about what the port guarantees.

**Reversal cost:** cheap for the five; the `RunQueue` omission is what
keeps Milestone 1 from appearing to exercise the worker path.

### A `Composition` exposes application services and nothing else

**Decided:** no adapter, repository, session factory, or engine is
reachable from the object `build` returns.

**Why:** ADR-0023 reserves `RunRepository.transition` to
`runtime/executor.py`, and the way that reservation survives a second
entry point is to make the repository unreachable from the entry points
rather than to rely on everyone remembering. Narrowing the return type is
the mechanism; the static check that no module outside `bootstrap.py`
instantiates an adapter is its enforcement.

**Reversal cost:** cheap now, expensive later.

### `runtime/engine.py` is retired in favour of three files

**Decided:** the Section 4 tree's `runtime/engine.py` becomes `loop.py`,
`executor.py`, and `supervisor.py`, and the tree is annotated rather than
redrawn.

**Why:** ADR-0023 already split the loop from the executor and made the
split structurally enforced — only `runtime/executor.py` may call
`RunRepository.transition` — which a single `engine.py` cannot express.
`supervisor.py` is the heartbeat and deadline task the same document
requires. Keeping the old name would leave a gate pointing at a file that
does not exist.

**Reversal cost:** cheap.

### `ModelProvider` was declared twice with incompatible shapes

**Decided:** `model-gateway.md` holds the canonical port. Section 7's
declaration is annotated as superseded and left in place, matching the
treatment already given to `RunRepository.claim_next`.

**Why:** the two declarations differ in the attribute name
(`provider_name` versus `name`), in the `stream` signature (the gateway
passes `ResolvedModel` and `ModelAttempt`), in the presence of `close`,
and in whether `capabilities` sits on the adapter at all — and neither
document said which one an implementer should write. The gateway version
is the one the rest of that spec, the router, and the retry ownership
split all assume.

**Reversal cost:** cheap.

## Development toolchain (ADR-0025)

### `make check` excludes the integration suite

**Decided:** `make check` is `lint typecheck test-fast`, where
`test-fast` is everything that needs neither a database nor a provider
credential. The integration suite has its own target and its own CI job.

**Why:** Section 21 requires both `make test` and `make check`, and
Section 24 makes "`make check` succeeds" a definition-of-done item. If
`check` contained the whole suite it would need Docker, and a fresh
checkout with no daemon running could not satisfy a definition of done.
The alternative — dropping the integration job from CI — would make the
harness's four-job shape decorative.

**Reversal cost:** cheap. One line in the Makefile.

### Six Makefile targets were added to Section 21's eight

**Decided:** `test-static`, `test-contract`, `test-fast`,
`test-integration`, `test-live`, and `docs`.

**Why:** the governing rule is that CI runs no command the Makefile does
not define, and there are four CI jobs plus a documentation build. Each
addition exists because a job invokes it. Without them the workflow file
inlines pytest selectors, which is the exact drift that lets `make check`
and CI stop agreeing about whether the build is green.

**Reversal cost:** cheap, but reversing it reintroduces the drift.

### Test selection is by pytest marker, not by directory

**Decided:** each of the six test directories is assigned a marker, and
`static`, `integration`, and `live` are the three that select. The
contract selector is a negation, so an unmarked test runs in job 2.

**Why:** the question a CI job asks is "can this run without Docker",
not "which folder is this in", and three of the six directories need a
database. Making the negation the default means a new test with no
marker is visible rather than silently unrun.

**Reversal cost:** cheap.

### PostgreSQL is pinned to 16-alpine

**Decided:** `postgres:16-alpine`, one service, named volume, healthcheck
that `make db-up` polls.

**Why:** the persistence layer depends on `FOR UPDATE SKIP LOCKED`
semantics, and a major-version change should be a reviewed change rather
than whatever `latest` resolved to that morning. Alpine is the smaller
image; the open question below records that its locale and collation
behaviour differs from the Debian-based tag, which is the surface a
sort-order-dependent query would hit.

**Cost:** none today.

**Reversal cost:** cheap, but a version change is a migration test.

### The compose credentials are `agent/agent` and the scanner scans them

**Decided:** they live in `.env.example`, and they pass the secret
scanner by an allowlist entry carrying the prose reason "local compose
default, not reachable from outside the host network".

**Why:** ADR-0024 already decided the scanner scans `.env.example`
rather than exempting it. A scanner that skips the one file every
contributor copies is not a scanner, and an allowlist entry that has to
state a reason is a reviewed fact.

**Reversal cost:** cheap.

### CI runs one Python version, not a matrix

**Decided:** 3.12 only.

**Why:** the project pins `requires-python >=3.12` and runs one
deployment. A matrix tests a configuration nothing runs, at roughly
double the CI minutes. This is recorded as an open question in the spec
because it sets an expectation about what "supported" means.

**Reversal cost:** cheap. A matrix is three lines of YAML.

### The live job never runs on a pull request

**Decided:** schedule and manual dispatch only. The job is defined at
Milestone 0 even though no live adapter exists until Milestone 3.

**Why:** live tests cost money per run and need a credential a fork's
pull request cannot hold. Defining the job early keeps the workflow file
complete and costs one skipped job per night.

**Cost:** external contributors never see live results on their branch.

**Reversal cost:** cheap.

### Egress is blocked at Milestone 0 by a pytest fixture

**Decided:** an autouse session fixture patches `socket.socket` to raise
on connect for the `static` and `contract` markers, exempting Unix
sockets and loopback for `integration`, and lifted by `live`.

**Why:** the harness's gate 7 requires the deterministic suite to run
without an API key, and observes that this is usually implemented as "we
did not configure one" — true until a fixture falls through to a real
client. The requirement was previously attributed to Milestone 0 only by
this document, which is not authoritative. A conftest fixture is the
smallest thing that makes the claim testable: no firewall, no container
network policy, no runner configuration.

**Reversal cost:** cheap.

### "Initial ADRs" means carrying the accepted set forward

**Decided:** Milestone 0 copies `docs/adr/` into the agent repository in
full, keeps the numbering and the index, and authors nothing new.
Numbering continues from the highest number carried over.

**Why:** the phrase had three defensible readings — the six filenames in
the Section 4 tree, the eleven a note defers to their milestones, or the
twenty-five that now exist. Carrying the set forward satisfies all three
at once. Authoring a fresh ADR for a decision this corpus already
recorded would create a second record that is edited independently of
the first, leaving a reader unable to tell which one the code follows.

**Reversal cost:** cheap.

### `docs/security.md` is placed at Milestone 0

**Decided:** the file Section 22 requires exists from Milestone 0.

**Why:** no milestone claims it. Section 24's definition of done requires
"Security implications are documented" for every milestone, and this is
the file that requirement writes to, so it must exist from the first
milestone with security implications. Milestone 0 ships two security
controls — the secret scanner and the egress block — so it is that
milestone. This adds no deliverable to the Milestone 0 implement list;
it identifies where an existing definition-of-done item lands.

**Reversal cost:** cheap.

### `docs-manifest.yaml` was left at four sources

**Decided:** the single-file HTML publication continues to carry the
index, the current milestone, the engineering plan, and the changelog.
The MkDocs site remains the complete corpus.

**Why:** widening it correctly requires per-document anchor prefixing in
`scripts/build_docs.py` first. Thirty-seven documents share heading names
— "Decisions", "Context", "Open questions for review" — and the anchor
generator resolves duplicates to the first occurrence, so a naive
widening produces a publication whose cross-references silently point at
the wrong document. Doing it badly is worse than not doing it, and the
plan now carries roughly thirty cross-reference paragraphs that a reader
of the combined file cannot follow either way.

**Cost:** a reader handed the single file must go to the site for any
specification or ADR.

**Reversal cost:** cheap once the anchor work is done; the manifest is
generated-output tooling and no canonical source changes.

### The structured-logging design is split across two specifications

**Decided:** the configuration shape, renderers, context variables, and
redaction processor are specified in `development-toolchain.md`; the call
site that runs the bootstrap is phase 1 of the composition root in
`bootstrap-and-composition.md`.

**Why:** logging is a Milestone 0 toolchain deliverable and the
composition root is where every startup-ordered thing is sequenced. Both
statements are true, and duplicating either would create a second place
to edit. It is recorded as an open question because it is the one file in
the corpus described by two specifications.

**Reversal cost:** cheap.

## Builtin tools (ADR-0026)

### Section 8.1's seven names are read as a convention, not a roster

**Decided:** the builtin roster is the union of Section 8.1's namespaced
names and Section 8.2's specified tools — eight tools, including
`demo.external_write`, which 8.1 omits, and `artifact.export`, which 8.2
omits.

**Why:** read as two rosters they contradict, and nothing in the plan
says which wins. `tool-system.md`'s domain partition table already
reserves `demo` as a builtin domain registered at build time, which a
document treating 8.1 as complete would not have done. The 8.1 fence
follows the words "Use namespaced names:", which is a convention
illustrated by example.

**Reversal cost:** cheap. Removing a tool from the roster is deleting a
row from the classification table.

### `artifact.export` is placed at Milestone 6

**Decided:** Milestone 6, with the control tools and the programmatic
bridge.

**Why:** the plan assigns it no milestone at all. Milestone 4 was
rejected because artifact export is not a workspace operation and would
put the model in front of the artifact store before that store's
retention and cross-tenant rules have been exercised. Milestone 5 was
rejected because pairing it with `sandbox.run_command` invites the two
designs to merge, and exporting a file is not a property of having run a
command. Milestone 6 is the first point at which the model rather than
the executor decides what leaves the run.

**Reversal cost:** cheap while it has no design; the classification is
milestone-independent.

### `math.calculate` gets a hand-written parser, not an `ast` allowlist

**Decided:** a hand-written tokenizer and precedence-climbing parser,
roughly two hundred lines, no dependency.

**Why:** Section 8.2 forbids unrestricted `eval` and says nothing about
what to use instead. The conventional answer — `ast.parse` in eval mode
plus a node-type allowlist — was rejected on three grounds: an allowlist
is a subtraction from a grammar that grows with every Python release,
Python's parse semantics are not the ones the tool wants (`/` is float
division, `-7 // 2` floors while `Decimal` truncates), and `ast.parse`
can exhaust the C stack on deeply nested input before any allowlist
runs. A closed grammar that a test can enumerate is not available any
other way.

**Cost:** about two hundred lines to write and maintain, against thirty
for the allowlist.

**Reversal cost:** moderate. The grammar is checked in and the property
test asserts against it, so replacing the front end means replacing the
test's premise.

### The numeric type is `decimal.Decimal` at fifty significant digits

**Decided:** `Decimal`, `prec = 50`, `ROUND_HALF_EVEN`, `Emax` and
`Emin` at ten thousand, with `InvalidOperation`, `DivisionByZero`,
`Overflow`, and `Underflow` trapped.

**Why:** the most commonly reported class of "the model got the
arithmetic wrong" is a tool returning `0.30000000000000004` for
`0.1 + 0.2`. A calculator that reproduces binary floating-point surprise
has given up its only advantage over the model's own token-level
arithmetic. Fifty digits is past anything a calculator tool is asked and
bounds the cost of `exp` and `ln`. Putting the magnitude bound in the
context rather than the evaluator means no call site can forget it.

**Reversal cost:** raising the precision is a one-line change nobody
would notice; lowering it is not, once results have been rendered.

### `//` and `%` floor rather than truncate

**Decided:** Python's `int` semantics — `floor(a / b)` and a remainder
whose sign follows the divisor — implemented explicitly rather than by
calling `Decimal`'s operators.

**Why:** `Decimal` truncates toward zero, so `Decimal(-7) // Decimal(2)`
is `-3` where `-7 // 2` is `-4`. The caller is a language model whose
prior for both operators is Python's, and whose reasoning about a
modulus is almost always a positive-residue argument. Being wrong in the
other direction produces a correct argument built on an unexpected `-1`,
which is confidently wrong with no failure anywhere.

**Cost:** someone reading the implementation has to notice the operators
are not `Decimal`'s.

**Reversal cost:** cheap now — one function, one differential test, one
sentence of the `description` — and expensive once a model has learned
the tool's behaviour.

### There is no trigonometry at Milestone 1

**Decided:** the function set is `abs`, `ceil`, `floor`, `round`,
`sqrt`, `ln`, `log10`, `exp`, `min`, and `max`, plus the constants `pi`
and `e`.

**Why:** not difficulty — a Taylor series over `Decimal` is short — but
that `sin(90)` has two defensible answers and a result has no field in
which to say which convention it used. A tool that silently reads
degrees as radians returns a plausible wrong number. Adding trigonometry
means adding a units argument or a second family of names, which is a
decision to make when something needs it.

**Reversal cost:** cheap, but it is a `ToolSpec` version bump, which
invalidates every cached prefix.

### Builtin failure messages carry the supported set and never the input

**Decided:** the model-facing `message` for every reason code is static;
`unknown_name` carries the whole supported function and constant list;
the character offset of a syntax error goes to `detail`, which the
operator sees and the model does not.

**Why:** hard gate 4 requires a static message per `reason_code`, and
that table is one table for every tool including MCP tools whose failure
text is written by a third party. A table with one interpolating entry
has no invariant left. The mitigation is to make the reason codes
specific enough — `syntax` against `arity` against `domain` against
`unknown_name` — that the model can tell a typo from a misunderstanding
without being told where.

**Cost:** a model that mis-parenthesizes has to re-derive where, from an
expression it wrote.

**Reversal cost:** cheap for this tool, expensive as a precedent.

### `Clock.now()` is declared to return an aware UTC `datetime`

**Decided:** aware, `tzinfo` UTC, asserted in the `Clock` contract
suite.

**Why:** `runtime-loop.md` types it as `datetime` and says no more.
Every consumer so far compares two values from the same clock, so the
ambiguity was harmless; `system.current_time` converts between zones and
is the first consumer for which it is not. A naive datetime cannot be
converted without assuming the process's local zone, which would smuggle
ambient state through a port built to keep it out.

**Reversal cost:** none in practice — it is a clarification every
existing consumer already satisfies.

### `system.current_time` accepts IANA names only and defaults to UTC

**Decided:** resolved through `zoneinfo.ZoneInfo`; abbreviations like
`EST`, fixed offsets like `+05:30`, and the literal `local` are all
rejected; the default is `UTC` rather than the process's local zone;
`tzdata` becomes a declared runtime dependency.

**Why:** abbreviations are ambiguous — `IST` names three zones and `CST`
names four — so a tool that picks one has answered a different question.
Defaulting to the server's zone makes the answer depend on which host
ran it, which is the nondeterminism the `Clock` port exists to remove.
`tzdata` is declared because the failure without it is that every name
except `UTC` stops resolving in a slim image while every developer
machine passes.

**Cost:** one more runtime dependency in the image.

**Reversal cost:** accepting fixed offsets later is cheap; removing them
once accepted is not. Recorded as an open question in the spec.

### Hard gate 2 is restated over `target_kind` as well as `source`

**Decided:** no spec whose `source` is `MCP` or `DEVICE`, **or** whose
`target_kind` is `sandbox` or `device`, may declare `output_trust` above
`EXTERNAL_UNTRUSTED`.

**Why:** `tool-system.md` states the gate over `source`, which catches
MCP and device tools. `sandbox.run_command` is a builtin — `source =
BUILTIN`, `target_kind = sandbox` — so the gate as written does not
reach the one builtin that returns bytes produced by code we did not
write, which is exactly what the gate exists to stop being read as
trusted narration.

**Reversal cost:** none. It is a widening that fails nothing currently
declared.

### `allow_parallel` is false for every builtin that writes

**Decided:** true for the four read-only tools, false for the other
four.

**Why:** it costs nothing at Milestone 1, where both tools are pure, and
it removes an ordering-bug class that would first appear at Milestone 4
when three workspace tools can appear in one batch. Loosening it later
is a measured decision; tightening it after a bug is a regression
investigation.

**Reversal cost:** cheap, but it is a `ToolSpec` version bump.

### Classification is settled for all eight tools four milestones early

**Decided:** every `ToolSpec` field that registration validation reads
or the 9.2 matrix is keyed by has a value now, including for
`sandbox.run_command` and `artifact.export`, whose behaviour is not
designed.

**Why:** hard gate 1 refuses to start without `output_trust` on every
registered spec, and no builtin in the corpus declared one. Settling
classification is also what lets Milestone 4's policy work run against
real registry rows. The alternative — specifying all eight completely
now — is false precision, because `sandbox.run_command`'s design is the
sandbox design and would be rewritten after Section 28's mechanism
decision is exercised.

**Reversal cost:** cheap. A classification field is one table cell and
one line of a spec.

## Milestone map (ADR-0027)

### One heading, one form, and one `**M<n>.**` suffix across ten specs

**Decided:** every declaring spec spells the section `## Hard gates`,
declares each gate as a numbered item with a bolded lead, and ends each
item with a milestone token the docs check reads. Five headings were
renamed, four bullet lists became numbered lists, and seventy-five
tokens were added.

**Why:** registry rule 1's docs check is a Milestone 0 deliverable and
it could not be written against the corpus. The section was spelled
three ways, declared two ways, and nine of the ten specs stated no
milestone at all. Teaching the check all three headings and both forms
was rejected: it makes the check the place where the inconsistency is
preserved rather than removed, and a parser with three branches is one
nobody trusts to fail correctly.

**Cost:** ten files edited mechanically. No sentence stating a
requirement changed, which was checked sentence by sentence rather than
asserted.

**Reversal cost:** cheap but pointless. Reverting the form means
rewriting the check to be worse.

### Nine bullets carrying eleven metrics moved out of two gate lists

**Decided:** tracked metrics live in a sibling `## Tracked metrics`
section. Five specs gained one. The two specs that interleaved gates
with metrics in a single list keep fourteen gates and lose nine
bullets.

**Why:** a count over an interleaved list is undefined until somebody
reads every bullet and decides, which is exactly what rule 1's check
cannot do. The separation was made on the specs' own words — *"A hard
gate"*, *"The primary metric"*, *"not a metric to improve"* — rather
than on my judgment about which items sounded gate-like.

**Reversal cost:** cheap. The bullets moved intact.

### The harness's memory-formation count of seven becomes five

**Decided:** five gates, not seven. Four of that spec's eight bullets
are the metrics the spec itself calls metrics.

**Why:** the harness's table was written when six specs existed and
counted a bullet list nobody had yet had to parse. Correcting the count
was unavoidable once the list was split, because the table is now
asserted against the registry rather than maintained by hand.

**Reversal cost:** none. It is a derived number.

### `optional` is added to the registry for exactly one gate

**Decided:** a bounded `optional` field, used by the model gateway's
live vendor smoke test, which may report skipped when its named
precondition — a credential — is absent.

**Why:** the registry forbids a skip, and this gate cannot run in a
fork without vendor keys. The alternatives were deleting a gate that
catches real provider drift or letting a skip pass silently, which is
the thing the registry exists to prevent. Use is bounded to external
credentials in writing.

**Question for you:** a second use is a design smell I would rather
argue in review than legislate now. If you want the field forbidden
outright, the gate becomes a scheduled job outside the gate set.

**Reversal cost:** moderate. Removing the field means finding another
home for the gate.

### Milestone 1 cancellation is a deadline plus a `SIGINT` handler

**Decided:** `CancellationToken` is buildable at Milestone 1 as a
lazily evaluated deadline plus a process signal handler, and
`CancelReason` splits by dependency — `DEADLINE` at Milestone 1,
`FENCED` at Milestone 2, `REQUESTED` by poll at Milestone 2 and by
endpoint at Milestone 5.

**Why:** the runtime loop assigns the token and three observation
points to Milestone 1 while every writer it names lives in a supervisor
task Milestone 2 builds. Deferring all cancellation to Milestone 5 —
Section 21's reading — leaves three observation points unreachable for
four milestones, and the code that must observe them is written at
Milestone 1 either way. A deadline and a signal need only `Clock` and
the process.

**Question for you:** this is the one placement where I overrode a
milestone the plan states rather than one it leaves silent. It is
recorded as an open question in the map for that reason.

**Reversal cost:** cheap. Deleting the two writers leaves the
observation points dead but correct.

### The import-boundary walk is owned by this plan, not by a spec

**Decided:** `gate.structure.import_boundary` has one owner, the
engineering plan's Milestone 0, and `tool-system.md` keeps its sentence
as a declared alias. Two other gates were declared twice and are owned
by `event-log-and-persistence.md`, with `runtime-loop.md` restating
them.

**Why:** a count check that does not know about a double declaration
double-counts, and an implementer who does not know writes the same
assertion twice under two names. The non-owning spec keeps its sentence
because a reader of the tool system should still learn that the import
boundary is checked; what changed is that the sentence now says it is
the same gate. Ownership follows declaration, which put one registry
entry outside the detailed-design specs.

**Reversal cost:** cheap. Ownership is one column.

### The map declares seven gates of its own, raising the registry to 94

**Decided:** the milestone map's seven checks over the corpus are
registry entries in the `harness` area — token presence, map bijection,
alias arithmetic, anchor resolution, identifier grammar, a derived
census, and milestone ordering. Six are Milestone 0 and one is
Milestone 1.

**Why:** I found this by running the map's own gate 2 against the map:
seven gates were stated under its hard-gates heading and absent from
its census, its gate table, and the harness's kind table, so the
document failed its own bijection check. Registering them is the only
resolution that does not weaken a gate. They take the `harness` area
because the harness owns the registry and the docs check that reads it;
the map declares them because each is a check over the scheduling
record, which is what the map is.

**Cost:** the registry goes from eighty-seven entries to ninety-four
and Milestone 0 goes from five gates to eleven. Every derived count in
two documents was recomputed.

**Reversal cost:** moderate. Unregistering them reopens the bijection
failure.

### Tool-system build step 3 splits at the unique index

**Decided:** the idempotency port, its semantics, and its contract
suite are Milestone 1; the unique index that makes deduplication
correct under concurrency is Milestone 2. The in-memory adapter
declares the gap against the capability table rather than simulating
it.

**Why:** the tool system places persistence at step 3 and calls steps 1
through 5 Milestone 1, while the plan forbids PostgreSQL persistence
until the in-memory slice is complete. This is ADR-0024's
repository-versus-storage separation applied a second time, and its
reason for rejecting an in-memory `RunQueue` applies unchanged: a
simulation of a unique index passes its own test and teaches the wrong
lesson about what the port guarantees.

**Reversal cost:** cheap. The port does not change; one gate moves.

### Context-engine gate 1 moves from Milestone 7 to Milestone 1

**Decided:** the deterministic-assembly gate and build-sequence step 1
are Milestone 1. The other four gates stay at Milestone 7.

**Why:** the spec states Milestone 7 once for the whole section, and
ADR-0024 decision 10 had already scheduled that gate's subject — the
minimal context builder and `prefix_sha256` — into the vertical slice.
This is the evidence that a section-level milestone is not good enough,
and it is why the form is now per-gate.

**Reversal cost:** none. The gate follows the code that was already
scheduled.

### Milestones 6 and 8 add no gates, reported rather than fixed

**Decided:** report the shape. No gates were invented to fill the two
empty columns.

**Why:** every invariant those two milestones strengthen is already
registered against an earlier milestone, so the columns are empty for a
defensible reason rather than an oversight. Inventing a gate to fill a
column produces a check written to exist rather than to fail.

**Question for you:** this is worth a look when you are back. If
isolated execution and skills genuinely warrant new invariants, they
should be stated in those specs and land in the registry from there,
not be back-filled from the map.

**Reversal cost:** none. Adding a gate later is the normal path.

### Thirty-eight of ninety-four gates are green before Milestone 2

**Decided:** reported as a finding, not acted on.

**Why:** it was not knowable before the gates were counted, and it is
the strongest argument in the corpus for building the in-memory tier as
real adapters rather than as test doubles. Eleven of the thirty-eight
hold against a repository with no agent in it.

**Reversal cost:** not applicable; it is an observation.

## Readiness review

### Coverage is judged against the whole corpus, not the specs alone

**Decided:** a mechanism designed in the engineering plan and nowhere
else counts as covered, and is labelled plan-only so the distinction
stays visible.

**Why:** Section 16 specifies nine endpoints with methods, paths,
headers, request and response bodies, the SSE frame format, the
reconnect rule, and the error envelope. Scoring that as absent because
no file under `docs/plan/` expands it would produce a Milestone 5
verdict that is false in the way that matters, since an implementer
reading Section 16 can build most of that milestone. The distinction is
still reported, because every other plan section was expanded exactly
once and every expansion found a real conflict.

**Reversal cost:** none. It changes a label, not a finding.

### An explicit deferral scores as partial rather than absent

**Decided:** where a document names what it does not design, the
undesigned thing is partial.

**Why:** `builtin-tools.md` has a section titled *"The six tools this
document does not design"* that assigns each a milestone and lists what
it owes, and `tool-system.md` states that device tools are a reserved
seam rather than a design. A hole somebody has measured is a different
object from one nobody has looked at, and collapsing the two would make
the review least useful exactly where the corpus is most self-aware.

**Cost:** it makes the review's counts look better than a naive scan
would. The compensation is that each deferral is named individually
with what it still owes, so nothing hides behind the label.

**Reversal cost:** none.

### The readiness review resolves no conflict

**Decided:** it reports four milestone conflicts and defers each to the
document that owns the subject.

**Why:** the milestone map owns scheduling and the specs own their
requirements. A readiness review that also decided things becomes a
document the other documents must be reconciled against, which is the
problem the map was written to solve.

**Reversal cost:** none.

### The API specification is named as the next document to write

**Decided:** the review recommends writing an API specification before
coding reaches Milestone 5, rather than building that milestone from
Section 16 directly.

**Why:** Milestone 5 registers one gate, the fewest of any milestone
that adds work, and no detailed-design spec covers the API layer. Six
items inside Section 16 are visibly unsettled: the error envelope has
one example and no code list, request IDs are a bullet, whether the
HTTP `Idempotency-Key` and the Milestone 1 idempotency port are one
mechanism is undecided, the SSE consumer side is one sentence
forwarding to Section 16, authentication is designed only at its
refusal, and nothing turns an HTTP cancel into an observation by a
worker in another process.

**Question for you:** this is the largest remaining piece of design
work and I have recommended rather than started it, because it is a
document rather than a reconciliation and you may want the API surface
to be yours. The counter-argument is real: Section 16 is more detailed
than the sections that turned out to hide contradictions, HTTP has
stronger conventions than composition roots do, and the six items are
individually small.

**Note:** since written. It is
[http-api-and-streaming.md](../plan/http-api-and-streaming.md) and
ADR-0028, and the expansion found nine contradictions, which settles
the argument above on evidence rather than on the prediction. The
decisions taken while writing it are recorded under
[HTTP API and streaming](#http-api-and-streaming-adr-0028) below.

**Reversal cost:** low now, high after clients exist.

### The sandbox specification is named as the second document

**Decided:** reported as the highest-consequence gap, and flagged as
possibly belonging before Milestone 5 rather than after.

**Why:** Milestone 6 registers zero gates, eight of its twelve implement
bullets have no design outside the plan, and `tool-system.md:977`
already constrains MCP server URLs by *"the egress allowlist the sandbox
spec establishes"* — a specification that does not exist. It is the only
undesigned area whose failure mode is a trust boundary rather than a
missing feature.

**Question for you:** milestone order says the sandbox spec follows the
API spec. I have not reordered them, but writing it first costs nothing
except the order in which two documents get written, and one document
already depends on it.

**Reversal cost:** none; it is an ordering question between two
unwritten documents.

### Knowledge retrieval is reported as possibly its own milestone

**Decided:** reported, not acted on.

**Why:** knowledge is half of Milestone 9's title and has no design
anywhere — no ingestion, chunking, indexing, or scoping — while the
memory half has two complete specifications and fourteen gates.

**Question for you:** splitting knowledge out would make Milestone 9
shippable against what is actually designed. I did not split it,
because renumbering milestones touches every document in the corpus and
that is your call rather than mine.

**Reversal cost:** high once made, which is why it is a question.

## HTTP API and streaming (ADR-0028)

### The wire error vocabulary is the existing taxonomy, snake-cased

**Decided:** the code list is Section 13's twenty-three error classes
and the runtime loop's eight, mechanically snake-cased, plus four
API-specific codes for conditions with no domain class —
`malformed_request`, `unsupported_media_type`, `payload_too_large`,
`rate_limited`.

**Why:** Section 16's only worked example is `tool_validation_error`,
which is `ToolValidationError` snake-cased. The convention was already
chosen; the document applies it rather than picking a second one. A
hand-maintained mapping between an internal taxonomy and a separate
client vocabulary is a thing that drifts, and the drift is silent.

**Alternative:** a small vocabulary designed for clients, which is
what most APIs have and which reads better. It costs a mapping table
somebody owns.

**Reversal cost:** moderate. Renaming a code after a client depends on
it is a breaking change, and the codes are the part of an API that is
hardest to change.

### Four error classes deliberately never reach a client

**Decided:** `WorkerFenced` and `EmptyModelTurn` are internal, two
further classes have no client-actionable meaning, and any class not
in the map resolves to `internal_error` and 500.

**Why:** a fenced worker is not a run failure and an empty model turn
is retried internally, so surfacing either would describe an event the
client cannot act on and did not cause. The catch-all exists so that
adding a class to the taxonomy cannot leak a class name onto the wire.

**Cost:** a genuinely new client-facing condition will arrive as
`internal_error` until somebody maps it. The gate that every returned
code is in the vocabulary catches the leak, not the omission.

**Reversal cost:** cheap.

### Scopes are exact-match strings over a closed dotted vocabulary

**Decided:** eight scopes, matched by string equality. No wildcards,
no prefixes, no hierarchy in which `run.write` implies `run.read`.
Roles are bundles resolved at authentication and the API never checks
a role.

**Why:** a hierarchy needs a grammar, a grammar has an evaluation
order, and an evaluation order can be subtly wrong in the direction of
granting access. Exact match cannot be subtly wrong.
`engineering-plan.md:459` names scopes and no document had stated
their grammar.

**Cost:** callers list every scope they need, and adding a route may
mean adding a scope to existing principals.

**Reversal cost:** cheap while the vocabulary is closed; adding a
hierarchy later is compatible with exact-match tokens already issued.

### A resource in another tenant is 404, never 403

**Decided:** generalized from the rule the policy spec already fixes
for approvals. Scope is checked before tenancy.

**Why:** 403 is the more informative answer and that is the problem —
it confirms the resource exists, which turns identifier enumeration
into a working attack. Checking scope first means a principal with no
scope cannot watch 403 become 404 and learn the same thing.

**Cost:** a caller with a genuine permission problem sees the same
response as a caller with a typo. The structured log distinguishes
them; the wire does not.

**Reversal cost:** cheap.

### `SessionStatus` is declared uppercase, against Section 16's sample

**Decided:** `ACTIVE` and `CLOSED`, matching `RunStatus` and the
guarded updates in the DDL. Section 16's sample shows `"active"` and
Section 16 is not edited.

**Why:** the type is referenced in Section 5 and declared nowhere, so
something had to declare it. Every other status in the corpus is
uppercase. Lowercase session status beside uppercase run status on one
wire is what a client library encodes as two enums and a comment.

**Question for you:** editing Section 16's one-line sample would
settle this in a character, and the conversion rules forbid editing
the plan's requirements during conversion. This is the one place the
new document and the plan disagree on a literal value.

**Reversal cost:** cheap now, expensive after a client parses it.

### The two things called "idempotency key" are two mechanisms

**Decided:** the HTTP `Idempotency-Key` header on message submission
and `tool_invocations.idempotency_key` are unrelated. Different
scopes, different tables, different milestones.

**Why:** the milestone map schedules the tool key as a Milestone 1
port on `ToolInvocationRepository`, which is a tool-call concern,
while the `idempotency_keys` table is Milestone 2 and carries a
`request_hash` for an HTTP body. Unifying them would put a table at a
milestone whose DDL does not exist. The readiness review left this
undecided and deferred it here.

**Cost:** two mechanisms with one name is a naming problem that will
be rediscovered. The document names both explicitly for that reason.

**Reversal cost:** cheap.

### A repeated submission with a different body is a conflict

**Decided:** a repeat of an `Idempotency-Key` whose `request_hash`
matches returns the original run with 200; a repeat whose hash differs
returns 409.

**Why:** returning the original run for a different body would answer
a question the client did not ask, silently. A client that reuses a
key by accident has a bug, and 409 makes the bug loud at the point it
happens rather than at the point its consequences are noticed.

**Reversal cost:** cheap.

### A second message to a `WAITING_FOR_USER` run is routed, not rejected

**Decided:** routed to input delivery, returning 202. Every other
non-terminal run status returns 409.

**Why:** Section 27.3 permits either and requires one to be
configured. Routing is what a user answering a question expects, and
rejecting makes every client re-implement a rule the server already
has the state to apply.

**Question for you:** the counter-argument is that rejecting is more
explicit and a client that meant to start a new run gets told so. The
plan calls this a configured policy, so both remain reachable; this
sets the default.

**Reversal cost:** cheap. It is a configuration default.

### Transient stream frames carry no `id` field

**Decided:** token deltas and other non-persisted frames are sent
without `id`. Only frames backed by a persisted event carry one.

**Why:** the EventSource specification advances a client's
last-event-ID only on a frame that carries `id`. A synthetic id on a
transient frame therefore sets the client's resume point to a value
the server cannot resolve, and every subsequent reconnect is wrong.
This is the most tempting wrong decision in the document, because
uniform framing looks tidier.

**Reversal cost:** cheap to state, expensive to discover late — the
failure is silent and appears only after a reconnect.

### Replay subscribes before it reads

**Decided:** `LISTEN` first, buffer arrivals, read the persisted
prefix, note the high-water mark, drain the buffer discarding at or
below it, then go live.

**Why:** the obvious order is to read the log and then subscribe, and
it drops every event committed in the window between the two. The
window is small and it is not empty. Subscribe-before-read is what
makes the handoff gapless; discarding by sequence is what makes it
duplicate-free. Eval case 22 tests exactly this.

**Reversal cost:** cheap now. It is an ordering inside one function.

### Overflow closes the stream with a resumable marker

**Decided:** a client too slow to drain gets its stream closed with a
marker it can resume from, rather than an unbounded buffer or a silent
drop.

**Why:** unbounded buffering makes one slow client a server-wide
memory problem, and silent dropping breaks the only guarantee the
stream makes. Closing is recoverable because the durable log is the
source of truth and the client reconnects against it.

**Reversal cost:** cheap.

### Artifact content is always served as an attachment

**Decided:** `Content-Disposition: attachment` for every media type,
with no inline exceptions.

**Why:** an artifact is model-influenced content served from the API
origin. Serving it inline is stored cross-site scripting. A list of
media types deemed safe is a thing that grows by argument, and the
argument happens in a pull request rather than in a threat model.

**Cost:** a browser client that wants to display an image fetches it
and creates an object URL. That is a few lines in the client.

**Reversal cost:** cheap to relax, and relaxing it is the direction
that carries the risk.

### Health is the only unauthenticated surface, and it says little

**Decided:** `/health/live` and `/health/ready` need no credential,
and their bodies carry no version, host, dependency name, or count.

**Why:** an unauthenticated endpoint that reports the database is
unreachable, or which version is deployed, is reconnaissance. Being
unauthenticated is safe only if there is nothing there.

**Cost:** an operator debugging readiness reads logs rather than the
probe body.

**Reversal cost:** cheap.

### The four application service signatures are fixed here

**Decided:** `SessionService`, `RunService`, `ApprovalService`, and
`ArtifactService` get their method signatures in this document, each
method taking `Principal` first and returning view types rather than
rows.

**Why:** `bootstrap-and-composition.md` names all four as what `build`
returns and gives none of them a signature, and the readiness review
found `ApprovalService` in that state. Section 17 makes the CLI a
second caller, and two callers discovering a signature independently
is how the CLI ends up importing a web framework.

**Reversal cost:** cheap. Nothing implements them yet.

### One route is added: `GET /v1/sessions/{session_id}`

**Decided:** added. Thirteen routes rather than twelve.

**Why:** a client reconnecting with only a session identifier
otherwise cannot learn the session's status or find its active run.
It exposes no capability a client holding its own records lacked.

**Question for you:** `GET /v1/runs`, a list of runs in a session, is
the obvious next route and was not added, because nothing in 0.1 needs
it and `active_run_id` covers reconnect. Say if you want it.

**Reversal cost:** cheap.

### 413 and 429 get response shapes now and mechanisms later

**Decided:** both codes are in the vocabulary with defined bodies;
neither has a mechanism in 0.1. Section 22 owns rate limiting.

**Why:** rate-limit numbers should come from operational data that
does not exist. Fixing the shape means a client written against 0.1
already handles the day the mechanism arrives, which costs nothing and
avoids inventing a mechanism blind.

**Reversal cost:** cheap.

### One authentication token, with no rotation without a restart

**Decided:** `auth_token` stays the single `SecretStr` that
`bootstrap-and-composition.md` declares. Comparison is
constant-time. Dev mode is bound to loopback.

**Why:** changing a declared `Settings` field is a change to another
document's design, which this assignment does not do unilaterally.

**Question for you:** should `auth_token` become a list, so a token
can be rotated by adding the new one, deploying, and removing the old?
As it stands the only rotation procedure drops in-flight requests.
This is the API's most operationally awkward decision.

**Reversal cost:** cheap now — one field, one comparison loop.

### The stream is per run, and heartbeats are every fifteen seconds

**Decided:** the event stream endpoint is per run. Fifteen seconds
between heartbeats when no event is flowing.

**Why:** ADR-0010 fixed stream ids as the session sequence, so a run's
stream is deliberately non-contiguous and clients must not infer a gap
from it — which the document states as a rule because it looks like a
bug. Fifteen seconds sits under common proxy idle timeouts and is
otherwise arbitrary.

**Question for you:** a session-scoped stream route is nearly free,
since the ids are already per session, and no client in 0.1 wants one.
Worth adding now or later?

**Reversal cost:** cheap.

### The policy rule behind an approval is not exposed to clients

**Decided:** not exposed. The approval view carries what the policy
spec already permits and no rule identifier.

**Why:** the policy spec withholds it deliberately, and this document
does not overturn another document's security decision as a
side-effect of writing a response body.

**Question for you:** an operator debugging a denial currently has
only the structured log. An operator-scoped field would fix that
without widening the client surface. That is a policy decision rather
than an API one, which is why it is asked rather than taken.

**Reversal cost:** cheap to add, expensive to remove.

### Event retention is left unbounded and is flagged

**Decided:** no retention rule is introduced.

**Why:** the corpus bounds checkpoints, memory, and artifacts, and
never bounds `events`. Introducing a retention rule here would be
inventing a durability policy inside an API document.

**Question for you:** replay depends on the log being complete for a
session's lifetime, so any future retention rule interacts directly
with the reconnect guarantee this document makes. It should be decided
where durability is owned, and it is currently decided nowhere.

**Reversal cost:** the decision is cheap; the migration that
implements it will not be.

### The readiness review is narrowed rather than left standing

**Decided:** the review's claim that the cross-process cancel path
"is not specified anywhere" is corrected in place. The worker half was
specified all along in `runtime-loop.md`; only the API half was
missing, and it was one column write.

**Why:** the review is a live statement of what the corpus covers, and
an overstatement in it would send an implementer looking for a design
that already exists.

**Reversal cost:** none.

### Historical records are not rewritten when the gate count changes

**Decided:** ADR files, this file's existing entries, and the
changelog keep the numbers that were true when they were written. The
milestone map, the harness gate table, the engineering plan's routing
paragraphs, and the readiness review are updated, because they are
statements of current fact.

**Why:** an ADR is a record of a decision at a point in time and
rewriting its arithmetic destroys that. ADR-0028 states the new total
instead, which is where a reader looking for the change will be.

**Reversal cost:** none.

## The gate registry (no ADR)

### The secret scanner is a gate, and it is registered

**Decided:** `gate.structure.no_committed_secrets`, structural,
Milestone 0, owned by the engineering plan. It was specified in full in
`bootstrap-and-composition.md`, carried a gate identifier there, ran in
`make check`, and failed the build — and it was in no registry table.

**Why:** the corpus's own definition of a hard gate is that it fails
the build, and every other check named in the engineering plan's
Milestone 0 acceptance criteria is a registry entry. Leaving this one
out is exactly the drift `gate.harness.registry_complete` exists to
catch, arriving before there is any code to catch it in.

**Cost:** the registry goes from one hundred and four entries to one
hundred and five and every cumulative figure from Milestone 0 onward
rises by one, one day after ADR-0028 fixed the totals at one hundred
and four.

**Alternative:** demote it — change *Gate id* to *check id* in
`bootstrap-and-composition.md` and correct the map's *three gates* to
*two*. Cheaper by about a dozen edits and defensible, since the four
AST rules in the same section are explicitly not gates. Rejected
because those four are assertions about module structure that hold
vacuously in an empty repository, and this one is a scan of committed
text that can fail on the first commit.

**Reversal cost:** low. One registry row and the arithmetic that
counts it.

### The area is `structure`, not `security`

**Decided:** the identifier's area changes from `security` to
`structure`. No twelfth area is added.

**Why:** `security` appears in exactly one identifier in the whole
corpus and in no grammar. `structure` is defined as the structural
statements about the repository that no single subject spec owns,
which describes a repository-wide scan precisely. The map already
claimed three `structure` gates while only two existed, so this makes a
sentence true rather than making a vocabulary bigger.

**Note:** if security-specific gates accumulate later — egress
enforcement and sandbox escape are the obvious candidates, and the
sandbox specification is the next document — a `security` area may
earn its place. One gate does not.

**Question for you:** the sandbox specification will register gates for
egress and isolation. Should those take a new `security` area, or stay
in `structure` and `tool`?

**Reversal cost:** low, while nothing consumes the identifier.

### The engineering plan owns it, not the spec that specifies it

**Decided:** the `spec` field points at the engineering plan's
Milestone 0 acceptance criteria, the same place
`gate.structure.import_boundary` points. `bootstrap-and-composition.md`
continues to declare no gates.

**Why:** the plan already names the secret scanner in that list, and
the map already explains that a plan-owned entry is what happens when a
requirement belongs to no subject spec. Giving
`bootstrap-and-composition.md` a `## Hard gates` section for one gate
would make the map's *two specs declare no gates at all* false and
change the declaration-form arithmetic, to record the same fact.

**Reversal cost:** low.

## Sandbox isolation and artifacts (ADR-0029)

### The gates take a new `sandbox` area, not `security` and not both

**Decided:** the thirteen gates the sandbox specification declares take
a new twelfth area, `sandbox`. The registry's area vocabulary becomes
`structure, runtime, tool, builtin, model, policy, event, context,
memory, harness, api, sandbox`.

**Why:** this answers the question left open under the gate registry
above — *"the sandbox specification will register gates for egress and
isolation. Should those take a new `security` area, or stay in
`structure` and `tool`?"* — and the answer is a third option that was
not on the list when the question was asked.

`security` was rejected on the same ground the single secret-scanner
gate was: an area names a subject some document owns, not a
cross-cutting property. Egress, isolation, limits, and artifacts are
not the only security-relevant gates in the corpus — cross-tenant
denial, the injection corpus, and credential scrubbing all live
elsewhere — so a `security` area would either pull those out of the
areas that own them or be a `security` area that does not contain the
security gates.

Splitting them across `structure` and `tool` was rejected because it is
false to the registry's own rule. One specification declares all
thirteen, and `memory` is the precedent: the two memory specifications
took one `memory` area between them, fourteen gates, rather than
distributing them into `event` and `context`.

**Cost:** the area list grows by one, and every document that states
the area count changes. The map, the harness gate table, and this file
are the three.

**Reversal cost:** low, while nothing consumes the identifiers.
Renaming an area is one column plus three sentences of prose.

### The workspace is a cache held for a lease, not state held for a run

**Decided:** a run's workspace exists for a worker's lease on that run,
not for the run's logical lifetime. It is created lazily at the first
sandbox-targeted call, held across steps and across an approval hold
shorter than `approval_hold_seconds`, and destroyed with the sandbox
when the lease ends. A run that resumes gets a fresh, empty workspace,
and anything that must survive is an artifact.

**Why:** the plan requires a workspace and never says how long one
lives, and the two candidate answers are not close. A durable per-run
workspace needs shared storage between execution hosts, which puts back
the cross-tenant blast radius that one sandbox per run removes; it
makes the workspace a second piece of state whose consistency with the
event log nobody owns; and it turns crash-resume into recovery. With
the workspace as a cache, resume needs no recovery at all — the sandbox
is gone, so there is nothing to reconcile.

**Cost:** a tool that writes a file and expects to find it after a long
approval hold will not find it. The specification makes that explicit
rather than incidental, and `artifact.export` is the way to keep
something.

**Question for you:** the hold threshold is `approval_hold_seconds`,
which is 300. A workspace survives a five-minute approval and is
dropped past it. If real approvals routinely take longer than that, the
number is what to revisit, not the rule.

**Reversal cost:** high once tools are written against it. The rule
shapes what `sandbox.run_command` is allowed to promise.

### `sandbox.run_command` is Milestone 6; `builtin-tools.md` was wrong

**Decided:** the tool ships at Milestone 6. The five places in
`builtin-tools.md` that put it at Milestone 5 are corrected — the
roster, the classification argument, and the two prose passages that
called Milestone 5 the sandbox milestone.

**Why:** Section 8.2 says the tool arrives *"only after the sandbox
milestone"*, and Section 21 names Milestone 5 *"HTTP API and SSE"* and
Milestone 6 *"Isolated execution and artifacts"*. `builtin-tools.md`
read "the sandbox milestone" as 5 and said so twice. That is a
transcription error against the plan's own milestone list rather than a
scheduling disagreement, so correcting the spec reverses nobody's
decision.

The readiness review reported this conflict and deferred it here,
because the real question was whether the tool could ship early against
the development sandbox mechanism and gain container backing later. It
cannot: that mechanism refuses to start when the environment is
production, and a tool that only works in development is not a
milestone deliverable.

**Note:** the argument `builtin-tools.md` was making survives the
correction and is kept. `artifact.export` and `sandbox.run_command` both
touch a workspace and must not be merged into one tool — one is
`IDEMPOTENT` and runs in process, the other is neither.

**Reversal cost:** low. Nothing is built.

### The red-team escape test becomes case 26, and nothing is renumbered

**Decided:** the container escape Section 28.7 demands becomes harness
case 26, a security case at Milestone 6 backed by
`gate.sandbox.escape_denied`. The twenty-five existing cases keep their
numbers and their text, and the heading that names them stays.

**Why:** the harness promises that Section 20's twenty-five cases stay
twenty-five, and `gate.harness.anchor_resolves` asserts that a case
number resolves to a stable anchor. Renumbering to slot a security case
in beside case 19 would break both to gain tidiness. Appending costs
nothing, and the rule it sets down — a case added later takes the next
integer and no case is ever renumbered — is worth having in writing
before there are a hundred cases.

**Note:** the case is skipped rather than passed when the configured
mechanism is `fake`, and the skip is a failure at Milestone 6. A fake
that reports no escape proves nothing, and a security case that passes
because it did not run is worse than no case.

**Reversal cost:** low.

### Artifacts expire after thirty days by default

**Decided:** `expires_at` is written at creation as thirty days out,
and a sweeper deletes what has passed. Deletion is by `expires_at`
alone, never by reference counting.

**Why:** the plan requires a retention rule and gives no number. Thirty
days is long enough that an artifact outlives the run that produced it
and any review of that run, and short enough that a tenant's store does
not grow without bound from the first day. Reference counting was
rejected because an artifact referenced by an event that is itself kept
forever is never collectable, which is a retention rule that does not
retain.

**Question for you:** this is the one default in the document that
silently deletes something a user might expect to keep, and thirty days
is a guess at what is really a product decision. If artifacts are meant
to be durable — the output of a long task somebody comes back to a
month later — the number is wrong, and the right answer is probably
per-tenant configuration with a floor.

**Reversal cost:** low before Milestone 6 and low after. It is a column
default and a sweep interval.

### `fake` is a fourth sandbox mechanism and a real adapter

**Decided:** `SandboxMechanism` gains a fourth value, `fake`, which
runs commands in a temporary directory with no isolation at all. It is
a production adapter in the same sense the in-memory repositories are:
it runs the `ExecutionEnvironment` contract suite unchanged, and the
composition root refuses it when the environment is production, by the
same startup check that refuses `docker`.

**Why:** the contract suite has to run in CI with no container runtime
available, and the alternative is a test double that lives in the test
tree and drifts from the port it doubles. The in-memory tier at
Milestone 1 is the precedent the corpus already set.

**Note:** two of the thirteen gates exist because this value exists —
the startup refusal at Milestone 1, and the rule that the security
cases run against a real runtime and never against `fake`.

**Reversal cost:** low.

### Section 28 is not edited, and the specification is subordinate to it

**Decided:** `sandbox-isolation.md` expands Sections 18 and 28 without
changing either. Where it is more specific than the plan — the
allowlist grammar, the three-tier environment, the workspace lifetime —
it is filling in what those sections left open, not deciding against
them.

**Why:** the conversion rules forbid materially rewriting the
engineering requirements, and every specification before this one has
been subordinate to the section it expands the way `runtime-loop.md` is
subordinate to Section 12. Nothing in Section 28 turned out to be
wrong, which made this easy.

**Note:** two things were left undone on purpose, both following a
convention the corpus already had rather than a rule anyone wrote down.
ADR-0029 is not added to the engineering plan's *"Version 2.0 adds
these ADRs"* list, because ADRs 0021 through 0028 are not in it either
— that list is a record of the 2.0 revision rather than an index. And
`sandbox-isolation.md` is not added to the milestone map's *"What
changes in each spec"* table, because `http-api-and-streaming.md` is
not in it either.

**Reversal cost:** low.

### `skill_manage` is a capability tool, not a control tool

**Decided:** Section 30.2 calls `skill_manage` a control tool. It is
reclassified as a capability tool, `risk: HIGH`,
`CONDITIONALLY_IDEMPOTENT`, requiring the `skill.write` scope, and
denied when `origin_trust` is below `USER`. `skill.load` stays a
control tool.

**Why:** `tool-system.md` draws the control-tool line at state that
does not outlive the run — the four control tools it lists change the
runtime's own state and take no approval, no scope, and no policy
row. A skill revision is a durable row in a tenant's schema. Keeping
the label would have meant relaxing all three constraints for one
tool, which empties the category for the four that belong in it.

**Alternative:** keep it a control tool and write "except
`skill_manage`" into each of the three constraints.

**Note:** this is the only place where a word in the engineering plan
is contradicted rather than expanded, and it is contradicted by a
specification that eleven documents already build on. Section 30.2's
two authoring paths, its four operations, and its "nothing is
auto-registered as a tool" are unchanged.

**Reversal cost:** low now — three lines in `tool-system.md` and a
heading in `skills.md`. Moderate after Milestone 8, when the registry
entry and its policy rows exist.

### The scope is `skill.write`, and there is no `skill.read`

**Decided:** one new scope, `skill.write`, added to the closed dotted
vocabulary in `http-api-and-streaming.md`. It gates `skill_manage`
only. No `skill.read` scope is created, and `skill.write` appears in
no route row.

**Why:** the API specification closes the scope list and matches
scopes exactly with no hierarchy, so a scope that is never checked at
the boundary would still have to be declared to be checkable at the
tool call. Reading the catalog is not an action a principal takes: it
is what a session already does at open, for every session, and a
scope that is granted to everyone is a column rather than a control.
ADR-0013 spells it `skills:write`, which is not the dotted form the
vocabulary uses.

**Alternative:** a `skill.read` scope gating catalog visibility, which
would make an agent with no skills indistinguishable from an agent
whose skills were withheld.

**Reversal cost:** low. Adding a scope to a closed list is a one-line
change and a row in the route table.

### `skill` is a thirteenth gate area

**Decided:** the sixteen gates go in a new `gate.skill.*` area rather
than being split across `context`, `tool`, and `policy`.

**Why:** the grammar's rule is that an area names a subject one
specification owns, which is the precedent `memory` and `sandbox`
both set, and a `security` area was rejected earlier for naming a
cross-cutting property instead. One document owns all sixteen. The
split would also have put `gate.skill.metadata_only` and
`gate.skill.authoring_trust` in different areas when they are two
halves of one governance argument.

**Alternative:** three gates in `context`, eight in `tool`, five in
`policy`, and no new area.

**Note:** this answers the milestone map's own open question 3 —
whether Milestone 8 should acquire gates — with yes, and closes the
last two milestone rows that showed zero.

**Reversal cost:** low before Milestone 0's docs check is written,
because a gate identifier is a string in four places. Moderate after,
because the check parses the area.

### The prefix ceiling moves from 13,500 to 15,000 tokens

**Decided:** two context classes are added — a pinned skill catalog
at twenty entries and 1,500 tokens, and loaded skill bodies at two
skills and 6,000 tokens — and the prefix ceiling that four documents
state as 13,500 becomes 15,000.

**Why:** the catalog is a prefix-region class by construction, since
it is pinned at session open and never changes within a session, and
1,500 tokens of it will not fit under a ceiling that was set before
it existed. 15,000 is 13,500 plus the catalog, rounded to the nearest
five hundred; the 6,000-token body class sits in Region B and is not
under the ceiling.

**Cost:** four edits in `bootstrap-and-composition.md` and one in
`context-engine.md`. No gate anchors to the number.

**Alternative:** hold 13,500 and take the catalog out of the prefix,
which would invalidate the cached prefix on every session whose
catalog differs — which is every session.

**Question for you:** 15,000 is a rounding, not a measurement. The
first real model fixture will say whether it is generous or tight.

**Reversal cost:** low. It is a constant in five places and a startup
constraint row.

### A third skill load fails rather than evicting the first

**Decided:** at most two skill bodies are loaded per session. A third
`skill.load` returns a structured failure; it does not evict.

**Why:** a loaded body is sticky for the session precisely so the
prefix stays byte-stable, and eviction is the one operation that
would undo that. A failure the model can see and route around is
cheaper than a cache invalidation it cannot.

**Alternative:** least-recently-used eviction, which is what a cache
would do and what a prefix must not.

**Note:** the 3,000-token body limit is half the 6,000-token class, so
the two-body cap holds by arithmetic rather than by policy. That
limit is derived from the cap rather than from any procedure anyone
has written, which is the wrong direction for a number to come from
and is `skills.md` open question 4.

**Reversal cost:** low.

### The authoring loop is Milestone 10 and takes its first gates

**Decided:** the static substrate — package, catalog, load, storage —
is Milestone 8. `skill_manage`, the background review, and rollback
are Milestone 10. Six of the sixteen gates land there, the first any
document has declared at Milestone 10.

**Why:** Section 30.5 says to ship the substrate first and enable
authoring when the evidence supports it, and Milestone 8 is where the
substrate is named. Nothing in Milestones 8 or 9 needs the write
path.

**Note:** Milestone 10 still has no acceptance criteria of its own,
which `readiness.md` reports as an open question and this does not
answer. Its gate column is no longer zero, but the gates come from a
plan section rather than from the milestone's own criteria.
`AGENTS.md` says Milestones 0 through 9 are implementable and
Milestone 10 is not to be begun.

**Reversal cost:** low. The milestone is a field on each gate.

### Harness case 27 is a Milestone 8 case with no threshold

**Decided:** one new evaluation case, *"A skill changes the
outcome"*, at Milestone 8. Three assertions: the second run succeeds
where the first fails, the first run's prefix contains no part of the
body, and the two runs' policy dispositions do not differ. No
numeric improvement threshold is set.

**Why:** Section 30.5 gates the authoring rollout on evidence that
self-authored skills improve eval cases without increasing policy
failures, and the corpus had no case behind that sentence. The case
belongs at Milestone 8 because it tests the substrate — a static
skill is enough to prove a skill can change an outcome — and putting
it at Milestone 10 would leave the substrate untested for two
milestones.

**Question for you:** what improvement counts? Two percent? Five? The
case proves the mechanism; it does not answer whether a given delta
should turn authoring on for a tenant. This is `skills.md` open
question 8 and it is the one number in that document nobody has the
data to choose yet.

**Reversal cost:** low. Cases are never renumbered, so 27 stays 27
even if its content changes.

### The readiness verdict on skills is corrected, not carried forward

**Decided:** `readiness.md` says skills have no specification at all
and that no document outside the plan and ADR-0013 mentions
`SKILL.md`. The second half is true; the first is not, and the
verdict is rewritten where it is stated rather than footnoted.

**Why:** `tool-system.md:1102-1149` is forty-eight lines of real
design — the metadata boundary, the trust label on skill content, the
`required_tools` check as a note rather than a refusal, and the rule
that a skill's script is not a tool. `skills.md` had to be written to
fit inside that section rather than on top of it, and a verdict that
says the section does not exist would have made the fit look
optional.

**Note:** the review's own miss is stated in the document rather than
quietly removed, because `readiness.md` is a live statement of the
corpus's condition and its value depends on it being corrected in
place.

**Reversal cost:** low.

### Two citation errors are fixed in live documents and left in records

**Decided:** the version-pinning acceptance criterion is at
`engineering-plan.md` line 2690, not 2684, and the policy-and-approval
gating requirement is Section 30.3, not Section 30.4.
`readiness.md` and `policy-and-approvals.md` are corrected;
`docs/adr/0005-two-stage-policy-and-approval-model.md` and the
2026-07-25 entry in this file are not.

**Why:** the corpus already separates live statements of current fact
from records of what was true at a point in time, and an ADR whose
citations are silently updated stops being a record.

**Note:** the reverse correction was made and undone during this
work. Section 30.4 is loading and lifecycle and it is the right cite
for "only skill metadata enters ordinary context"; Section 30.3 is
governance and it is the right cite for gating, provenance,
versioning, restricted review, injection resistance, and sandboxed
scripts. Two `context-engine.md` citations were changed to 30.3 in
error and are back at 30.4.

**Reversal cost:** low.

## Harness case gaps and citation integrity (no ADR)

### Line-number citations are checked mechanically rather than remembered

**Decided:** `scripts/check_citations.py` and a generated ledger,
`docs/status/citation-ledger.yaml`, were added. `make docs-check` now
fails when a `file.md:LINE` citation no longer holds the text it was
recorded against, and `make citations-fix` repoints a citation whose
text has merely moved. Nine wrong citations across five documents were
corrected first, so the ledger records a correct state rather than
blessing the current one.

**Why:** `skills.md` had just been written to say that a line-number
citation is correct only until the cited file is next edited and that
nothing checks it. A sweep proved the point harder than intended:
thirty-eight citations exist in the live specifications and nine of
them were already wrong, most predating this session's edits. Two of
the nine were induced by insertions made during it. Writing the hazard
down and leaving it unchecked would have been the worst of the three
available outcomes.

**Cost:** one script, one generated YAML file, one Makefile target, and
about six lines in `AGENTS.md`. The check is pure Python over the
repository and adds no dependency.

**Alternative:** delete every line number and cite by section heading
instead. That is more robust and was rejected because it would have
meant rewriting thirty-eight sentences across five specifications
during a conversion that is explicitly forbidden from rewriting
requirements, and because several citations point into code blocks and
tables that have no heading to name.

**Note:** the repair is deliberately partial. A citation whose text
moved is repointed automatically; a citation whose text is gone, or now
appears twice, is reported and left alone, because deciding what such a
citation now means is a judgment and not a substitution.

**Question for you:** do you want the line numbers at all? They are
precise and they rot. Heading-relative citations would survive edits
and would need no tooling, at the cost of a wide rewrite. My
recommendation is to keep them now, since the check makes the rot
visible, and to drop them if the check ever starts failing on churn
rather than on error.

**Reversal cost:** low. Deleting the script, the ledger, the Makefile
target, and the paragraph in `AGENTS.md` restores the previous state,
and the nine corrected citations are correct either way.

### The citation check is not written into the Milestone 0 toolchain spec

**Decided:** `docs/plan/development-toolchain.md` is left unedited. It
specifies the implementation-era `Makefile` — `lint`, `typecheck`,
`test-fast`, and the rest — and reconciles a target count against
Sections 21, 24, and 25. The citation check exists in the repository's
current documentation `Makefile` and is recorded in `AGENTS.md`.

**Why:** adding a seventh target to a table the specification
introduces as "six targets exist that Section 21 does not list" would
have meant re-deriving that reconciliation for a docs-only check, and
the constraint against rewriting settled requirements applies.

**Note:** this is a real risk and it is being flagged rather than
solved. When Milestone 0 replaces the documentation `Makefile` with the
implementation one, the citation check will be dropped unless someone
carries it across. `AGENTS.md` is the only thing that will remember.

**Question for you:** should `development-toolchain.md` name it, at the
cost of moving the count from six to seven?

**Reversal cost:** low now, higher after Milestone 0 ships a Makefile
without it.

### `delta` is promoted to a fifth assertion type, and ADR-0022 says four

**Decided:** the harness assertion vocabulary gains a fifth entry,
cross-arm metric relation, with `arms`, `carry`, and `delta` fields on
the case schema. ADR-0022 says the vocabulary gains four and is left
as written.

**Why:** two memory gates and Section 30.5's rollout criterion are
stated as comparisons between two runs — better on the metric, no
worse on policy. A vocabulary of predicates over a single run cannot
express that, so the harness could not assert three of its own hard
gates. Case 27 already described "runs one task twice and compares" in
prose with no schema behind it, and case 31 needed the same shape.

**Alternative:** express the comparison outside the harness, in a
one-off script per gate. Rejected because a gate asserted by a script
nobody runs is not asserted.

**Note:** ADR-0022 is a record of a decision at a point in time and is
not rewritten, per the rule already established for the gate count. The
divergence between "gain four" there and "gain five" in
`evaluation-harness.md` is deliberate and is logged here so it is not
later read as an error.

**Reversal cost:** moderate. The three fields are schema, and the
schema has not been implemented; after Milestone 8 it is a migration.

### "More than half the gates are not case gates" was arithmetic, and wrong

**Decided:** the claim is replaced with the exact count. Sixty-four of
one hundred and thirty-eight declared gates are not case gates.

**Why:** it was false when written — at one hundred and thirty-four
declared gates, sixty-three were non-case — and it stayed false as the
count grew. The sentence's purpose is to justify building the
structural and property tracks in Milestone 0, and a count does that
better than a fraction that has to be re-checked every time a gate is
added.

**Note:** two independent derivations now agree on the kind split:
seventy-four case, seventeen property, seven corpus, forty structural.
One comes from the milestone map's census of registry entries, the
other from the harness's own kind table. They were reconciled rather
than assumed.

**Note:** an earlier entry in this file is headed *"Roughly a third of
the declared gates are not case gates"*. It was right when written and
is left as the record it is. The ratio has moved twice as gates were
added, which is the argument for a count rather than a fraction: a
count is wrong loudly and a fraction is wrong quietly.

**Reversal cost:** low.

### `gate.tool.mcp_disconnect` is named for the column, not for clarity

**Decided:** the gate asserting that a mid-call server disconnect
yields `unavailable` with `tool.server_unreachable` is named
`gate.tool.mcp_disconnect` rather than
`gate.tool.mcp_disconnect_structured`.

**Why:** gate identifiers sit in a thirty-character column in the
milestone map's tables, and the corpus already truncates two of them
with a trailing `..`. A third truncation to buy one adjective is a bad
trade, and the shorter name is not ambiguous — there is one disconnect
gate.

**Note:** the thirty-character ceiling is a table-formatting artifact
that has started shaping identifiers. It is worth deciding whether the
column should widen before it truncates a name that matters.

**Question for you:** widen the column and un-truncate
`gate.sandbox.workspace_isola..` and
`gate.event.checkpoint_dispens.`, or keep the ceiling?

**Reversal cost:** low.

### `gate.harness.mcp_no_socket` is a case gate, not a structural one

**Decided:** the gate asserting that the MCP fixture layer opens no
socket is kind `case`. It was first written as `structural`, and the
harness kind table and totals were corrected before the count
propagated.

**Why:** the gate statement asserts the property by running the MCP
cases with egress blocked, not by inspecting the fixture code. That
makes it the same shape as `gate.harness.no_egress`, which is already
a case gate for the same reason. Kind follows how the gate is
asserted, not what it is about.

**Note:** this was caught by re-reading the gate statement against the
kind definitions rather than by any check. Nothing in the corpus
validates that a gate's declared kind matches how its statement says
it is asserted, and the two are written in different files.

**Reversal cost:** low now — one table row and two totals — and higher
once the harness is built against the wrong kind.

### The readiness verdict table held a gate count four short, and no check reads it

**Decided:** Milestone 8's row in the verdict table of
[readiness.md](../plan/readiness.md) is corrected from ten registry
entries to fourteen, and its named gap from *"MCP auth scheme; the
mock server"* to *"MCP auth scheme"*. The prose under the table now
says where the other four came from. The Milestone 8 section further
down was already right; only the summary row was stale.

**Why:** the four MCP gates added on this pass landed at Milestone 8,
and the table's own stated rule is that the column counts registry
entries whose `milestone` field names that milestone. The number was
therefore derived, written by hand, and wrong the moment the gates
were registered. It was found by re-deriving all eleven rows from the
registry tables rather than by reading the document — every other row
agrees, and the totals agree three ways: one hundred and thirty-eight
registry entries, the census, and the kind split of seventy-four case,
seventeen property, seven corpus, and forty structural.

**Note:** this is the same failure as the drifted citations recorded
above, in a different disguise: a number derived by a stated rule but
maintained by memory. The difference is that this one already has a
gate. `gate.harness.census_derived` requires a test that computes the
census from the registry and compares it to the written table, and it
is a Milestone 0 gate.

**Alternative:** implement that check now, inside `check_docs.py`,
the way the citation check was implemented. Rejected, and the
distinction is worth stating because the two cases look alike.
The citation hazard had no gate, no milestone, and no owner — it was
pure documentation maintenance, which is inside this assignment.
The census check *is* a declared Milestone 0 gate with a specified
home in the evaluation harness, reading a gate registry that does not
exist as data yet. Writing it here would implement a milestone gate
ahead of its milestone and in the wrong place, against the standing
constraint not to begin Milestone 0 work.

**Question for you:** the verdict table's column is derived from the
registry and drifted within one working pass. Should
`gate.harness.census_derived` be widened to cover it — the same test,
one more table — or should the column be removed from
[readiness.md](../plan/readiness.md) and the reader sent to the
census, which is the one place the number is stated by derivation?

**Reversal cost:** low. One table cell and one sentence.

## Persistence authoring (ADR-0031)

### Both Milestone 2 gaps are closed inside the persistence spec, not in a twentieth document

**Decided:** the ORM surface and the Alembic conventions are written
into
[event-log-and-persistence.md](../plan/event-log-and-persistence.md)
as two new sections rather than into a new specification, and recorded
as [ADR-0031](../adr/0031-persistence-authoring.md).

**Why:** that document already owns the schema, the `gate.event.*`
area, and `gate.structure.txn_hygiene`. A twentieth spec would need
either a fourteenth gate area or a gate area owned by two documents,
and the readiness review calls both gaps tooling rather than
architecture — they are two sections of material, not a subject.

**Alternative:** a `persistence-adapter.md` alongside the others. It
would read more discoverably in the nav and would split ownership of
the schema from ownership of the migrations that build it.

**Reversal cost:** cheap. Two sections move to a new file, the ADR is
re-pointed, and the gate ids do not change.

### The ORM shape was forced by the dependency rules, not chosen

**Decided:** row classes are separate declarative types confined to
`adapters/persistence/`, translation is two hand-written functions per
table in a `mappers.py`, and a repository is constructed with a live
session and never commits.

**Why:** declarative mapping of a domain type puts SQLAlchemy inside
`domain` and fails Section 5's first rule on the import walk.
Imperative mapping avoids the import and fails the seventh rule
silently instead — a mapped class carries instrumentation, so the
domain object *becomes* the ORM object and every repository return
value violates the rule at the moment the signature check has nothing
left to reject. Pydantic and SQLAlchemy also carry conflicting
metaclasses, so the runtime agrees with the rules.

**Cost:** roughly twenty tables acquire two boring functions each, all
of them needing a test. That is the honest price, and it buys a schema
and a wire contract that move independently, and an upcaster with a
function to live in.

**Note:** `mappers.py` is one module more than Section 4's tree lists.
It is introduced as an addition rather than presented as though it
were always there.

**Question for you:** the generic field-name mapper is the version
nobody has to write. It was rejected because it turns a column rename
into a `KeyError` at runtime rather than a type error at check time,
and because it is exactly the mechanism by which a schema change leaks
into an API payload with no one writing a line. Is that trade the one
you want at twenty tables?

**Reversal cost:** moderate before Milestone 2 ships and expensive
after. The mapping shape is set by the first repository written.

### Five gates were added, and four of them observe criteria that already existed

**Decided:** `gate.structure.migration_graph` at Milestone 0, and
`gate.event.migration_clean`, `gate.event.migration_stepwise`,
`gate.event.revision_pinned`, and `gate.structure.orm_confined` at
Milestone 2. The registry goes from one hundred and thirty-eight
entries to one hundred and forty-three.

**Why:** Section 24 makes *"Database migrations upgrade from a clean
database"* and *"Database migrations upgrade from the previous
revision"* conditions of **every** milestone, and nothing evaluated
either of them. That is the same defect the milestone map found by
counting gates, reached from the other direction: an acceptance
criterion no check evaluates is a sentence, not a criterion.

**Note:** every id is at or under thirty characters, which is the
width the milestone-map table column allows before an id truncates.

**Reversal cost:** cheap while they are prose. Each id appears in the
declaring spec, the registry block, the census, and the kind table.

### The migration-graph walk registers at Milestone 0, four milestones before the migrations

**Decided:** `gate.structure.migration_graph` is a Milestone 0
registry entry, tagged `**M0.**` inside an otherwise all-Milestone-2
hard-gates list in the persistence spec, and the milestone-map section
prose now explains two qualifications where it used to explain one.

**Why:** Milestone 0's own acceptance criteria already require that an
empty Alembic migration runs, which is a graph with one node. A walk
that only begins once a dozen revisions exist has already missed the
branch it exists to prevent, and a check added against existing
violations gets relaxed rather than obeyed. This follows ADR-0024's
precedent, where the transaction-hygiene *check* is a Milestone 0
deliverable and its *gate* is a Milestone 2 criterion.

**Alternative:** register it at Milestone 2 with the four others and
keep the list uniform. Cheaper to describe, and it would leave the
first six migrations unwatched, which is exactly the window in which
the conventions get set.

**Reversal cost:** cheap. One field in the registry and two
paragraphs.

### Two mis-numbered cross-references were corrected in live specifications

**Decided:** `bootstrap-and-composition.md` cited *"Section 3"* for
the `AsyncSession` unit-of-work rule, which is Section 2.2, and
`model-gateway.md` cited *"Section 3"* for the import-boundary tests,
which are required by Section 5. Both are corrected. The engineering
plan's Milestone 0 pointer paragraph, which named eleven registry
entries and one plan-owned gate, is corrected to thirteen and two —
it predates the secret-scanner gate becoming plan-declared.

**Why:** they are live statements of current fact, not records at a
point in time, and both were found by grounding rather than by
reading. Section 3 is *"Version 0.1 definition of done"* and says
nothing about either subject, so both citations pointed a reader at a
section that would not answer them.

**Note:** the ADR files and this file are not edited to match. They
are records of what was believed when they were written.

**Reversal cost:** cheap.

### A skills.md sentence carried bare line numbers that drift silently

**Decided:** the finding paragraph in [skills.md](../plan/skills.md)
that reads *"which is at 2692; line 2686 is an MCP trust-labelling
bullet"* now names `engineering-plan.md:2696` as a backticked citation
and refers to the other line by description rather than by number.
One ledger excerpt was repointed by hand.

**Why:** the citation checker only sees `file.md:NNN`. A bare number
in prose is invisible to it, so this pass moved the checked citation
and left the unchecked one behind — the two ended up naming different
lines in the same sentence. The paragraph is *about* citation drift,
which makes it the worst place in the corpus to carry an unchecked
number.

**Note:** the hand repoint was needed because a citation inside a
cited excerpt takes two `--update` passes to settle, and the second
pass cannot resolve by content once the excerpt itself has changed.
That is a real limitation of the checker and it is worth knowing
before someone hits it again.

**Reversal cost:** cheap.

## Bare line references (no ADR)

### Line numbers written in prose are converted, and the form is rejected

**Decided:** thirty-five references that named a line without naming
it in the checked form — *"line 1408"*, *"lines 659 to 661"*,
*"`tool-system.md` 1102-1149"* — are converted to `file.md:LINE`
citations across [model-gateway.md](../plan/model-gateway.md),
[readiness.md](../plan/readiness.md),
[runtime-loop.md](../plan/runtime-loop.md), and
[skills.md](../plan/skills.md). Every target was re-resolved by
content against the current file rather than trusted.
`scripts/check_citations.py` gains a `check_bare_references()` pass
that fails `make docs-check` on either form in a live specification.

**Why:** the checker only ever saw `file.md:NNN`, which means the
forms it could not see were the only ones that could rot — the ones
it could see are repaired on every run. They had rotted. Two were off
by more than eighty lines. The ones still correct were correct by
luck, since nothing had ever evaluated them.

**Cost:** twenty-eight patches across four specifications and
forty-three lines in the checker. The ledger grows from thirty-three
citations to sixty-five.

**Alternative:** delete the numbers and cite by heading instead.
Rejected on the same argument as the earlier sweep: several targets
are rows in tables and lines inside code blocks, and there is no
heading to name.

**Note:** two references were rephrased rather than repointed. A
second occurrence of *"line 2202"* in the paragraph that had already
cited it became *"that fixture asymmetry"*, and the self-referential
paragraph in `readiness.md` that named its own wrong line now names
the ledger instead. Repointing a number that a sentence is arguing
about would have kept the sentence and lost the argument.

**Reversal cost:** cheap for the checker, moderate for the prose.

### ADR files become citable targets, and one ADR filename was wrong

**Decided:** `docs/adr/*.md` is added to the checker's target globs,
last in the list so that a citation naming `index.md` still resolves
to `docs/index.md`. Three ADR-targeted citations exist as a result,
all of them new. One of them corrects a filename: `skills.md` cited
`docs/adr/0005-two-stage-policy-and-approval-model.md`, which is not
a file in this repository. The ADR it means is
`0005-deterministic-policy-engine.md`, whose lines 10 and 141 both
still carry the attribution that sentence relies on.

**Why:** an ADR is a record, but a citation *into* one is a live
statement and rots the same way. The wrong filename had survived
because nothing resolved it — a checker that knows three directories
cannot report a name in a fourth.

**Note:** the same wrong filename appears in an earlier entry in this
file and is left alone. It is a record of what was believed on
2026-07-25, and the corpus separates records from live statements.

**Reversal cost:** cheap.

### The standing toolchain-spec question is now worth more

**Decided:** `docs/plan/development-toolchain.md` is still left
unedited, on the reasoning recorded above under the harness section.

**Why:** none of that reasoning changed. The specification reconciles
a Makefile target count against Sections 21, 24, and 25, and a
docs-only check is not a Milestone 0 deliverable.

**Note:** what would be lost at Milestone 0 grew. It is no longer one
script that repoints moved citations; it is also the only thing
stopping thirty-five prose references from coming back one sentence
at a time. `AGENTS.md` now carries two paragraphs about it and is
still the only thing that will remember.

**Question for you:** the earlier question stands — should
`development-toolchain.md` name the check, at the cost of moving its
target count from six to seven? My recommendation has moved from
neutral to yes.

**Reversal cost:** low now, higher after Milestone 0 ships a Makefile
without it.

## The Milestone 3 gaps (ADR-0032)

The readiness review named three gaps at Milestone 3. Closing all
three was one pass, and it made four judgment calls that are cheaper
to reverse now than after Milestone 3 ships.

### The trajectory export goes in the persistence spec, not a fourteenth one

**Decided:** the export's format, redaction pipeline, consent model,
retention, CLI, and endpoint are all specified inside
`docs/plan/event-log-and-persistence.md`, in a new section between
Projections and Checkpoints.

**Why:** the export is a projection over the event log, and that
document already owns the log, the projections, the schema, and the
`gate.event.*` area. A fourteenth specification would have owned one
feature and borrowed all four.

**Cost:** the document is now the longest in the corpus.

**Alternative:** a `trajectory-export.md`. Rejected for a second
reason as well — "thirteen specs" is a number nine other documents
state, and a fourteenth would have rippled through all of them for a
feature that fits in an existing one.

**Reversal cost:** moderate. Splitting later is a move plus the
ripple that was avoided.

### `agent run export` is a subcommand, not a thirteenth command

**Decided:** `export` joins `get`, `events`, and `cancel` as a
reserved word after `agent run`. The CLI still has twelve commands.

**Why:** the precedent already existed and was already load-bearing.
`docs/plan/evaluation-harness.md` added four subcommands under
`agent eval` without changing the twelve, so a subcommand under an
existing command is not a new command. Section 17 states twelve and
`bootstrap-and-composition.md`'s own heading repeated it, which made
the alternative a corpus-wide arithmetic change for one feature.

**Note:** the composition spec's heading moved from "Three reserved
words" to "Reserved words after `agent run`", so the next spec that
needs one changes a list rather than a number.

**Reversal cost:** cheap.

### Consent is stamped forward and withdrawn backward

**Decided:** a grant is evaluated at run start and stamped on the
run; a withdrawal is evaluated at export time and by the sweeper, and
it reaches every run and every artifact already produced.

**Why:** the two directions are not symmetric and treating them as
symmetric gets one of them wrong. A grant is a statement about data
the principal has not produced yet, so evaluating it at export time
would retroactively authorize every conversation they had before
anyone asked. A withdrawal is a statement about data they have, so
leaving existing artifacts in place would make it a preference
rather than a withdrawal.

**Cost:** a boolean column on `runs` at Milestone 2 for a Milestone 3
feature, and a daily sweep that will almost always find nothing.

**Note:** the column has to precede the first exportable run, because
a run that started before it existed has no honest value to backfill.
Deletion routes through `expires_at` and the artifact sweeper that
already runs, so the rarest governance operation in the system runs
on the most exercised code in it.

**Question for you:** one grant covering all export, or separate
grants for evaluation use and for support use. One is specified; two
is defensible and costs a column.

**Reversal cost:** low before Milestone 3, high after a tenant has
granted anything.

### Redaction fails closed rather than redacting twice

**Decided:** the verification scan over the finished document raises
`ExportRedactionError`, writes no artifact, and names the rule
without printing the match.

**Why:** the alternative is to redact whatever the scan found and
ship the document. That converts a detectable defect in the
replacement stage into an undetectable one, and ships the artifact
either way.

**Cost:** an export can fail for a reason the caller cannot fix. That
is the correct trade and it needs to be said out loud in whatever the
operator reads.

**Reversal cost:** cheap.

### Two defects fixed in passing

**Decided:** `sandbox-isolation.md` used an undeclared `TrustLabel`
at two sites where the declared type is `TrustLevel`, and
`ArtifactOrigin` had no member for an export. Both are corrected, and
`TRAJECTORY_EXPORT` is the fifth origin.

**Why:** the second is not bookkeeping. `origin` is the field that
says what an artifact is a function of, and an export is the only
origin whose contents are a function of a whole run rather than of a
single act inside it.

**Reversal cost:** none.

## The Milestone 4 builtin tools (no ADR)

### No `workspace.` tool resolves a path

**Decided:** all three take a `path` argument and none of them joins
it, normalizes it, compares it against a root, or opens anything.
Each hands the string to `WorkspaceHandle` and lets the execution
service resolve it. A structural gate asserts the prohibition by
inspection: the three modules import no `os`, `os.path`, `pathlib`,
`shutil`, or `glob`, call no `open`, and reach the filesystem only
through `ToolExecutionContext.workspace`.

**Why:** the alternative is three implementations of one containment
rule, and a traversal test that passes because two of the three got
it right is exactly the failure being designed out. Three tools
written by three people over one sprint is the ordinary case, not a
hypothetical.

**Cost:** the tools cannot do anything clever with paths, which is
the point.

**Reversal cost:** none; loosening it later is a deletion.

### Provenance lives on `WorkspaceHandle`, not in a repository

**Decided:** `WorkspaceHandle` gains one method and one enum.
`write` records the resolved path as `TOOL_WRITTEN` in the same
operation that writes the bytes; `provenance` reports it;
`workspace.read_text` returns `INTERNAL_TOOL` for `TOOL_WRITTEN` and
`EXTERNAL_UNTRUSTED` for everything else.

**Why:** the obvious design is a query against the run's tool
invocations, and it is not available. `ToolExecutionContext`
deliberately carries no database session, no `EventRepository`, and
no `ToolRegistry`, and adding one to answer this question would undo
a boundary that exists for good reasons. The port that owns the
volume is the only thing that can answer honestly anyway.

**Alternative:** carry provenance on `listdir` entries rather than
answering per path. Rejected because it widens `FileChange`, which
several other things read, to serve one caller.

**Note:** `SANDBOX_WRITTEN` is defined at Milestone 4, two milestones
before anything can produce it, so that the Milestone 6 implementer
inherits the answer instead of choosing it. Bytes produced by code we
did not write are the case `EXTERNAL_UNTRUSTED` exists for, and
passing through this port does not launder them.

**Reversal cost:** low now, high after the first sandbox adapter.

### A listing is `INTERNAL_TOOL` only when every entry is

**Decided:** `workspace.list_files` returns `INTERNAL_TOOL` only when
every entry it returns is `TOOL_WRITTEN`, and `EXTERNAL_UNTRUSTED`
otherwise.

**Why:** a filename is attacker-controlled whenever the file is. An
archive extracted into a workspace can create
`URGENT_instructions_for_the_assistant.md`, and a listing that
renders that as trusted platform text is a prompt injection with a
very small payload.

**Cost:** in practice almost every listing at Milestone 4 will be
untrusted, because almost nothing fills a workspace yet. That is the
correct default and it means the rule is exercised from the start.

**Reversal cost:** none.

### The reader has no size-limit failure of its own

**Decided:** `workspace.read_text` declares no `too_large` reason
code. A large file becomes a large result and the execution
pipeline's existing excerpt-and-artifactize step handles it.

**Why:** a second ceiling is a second truncation policy to keep in
agreement with the first, and the two would drift the first time
either moved.

**Cost:** the model learns a file was truncated from the pipeline's
notice rather than from the tool's failure, which is one indirection.

**Reversal cost:** cheap.

### `demo.external_write` has no destination and no failures

**Decided:** it writes nowhere, has no side table, and cannot fail.
Its record is the `structured` result the pipeline already persists
on `tool_invocations` at step 13, and it carries no timestamp.

**Why:** its entire purpose is to exercise the approval path. Every
byte of machinery beyond that is a second thing to operate for a tool
whose value is that it does nothing.

**Question for you:** whether it should be registered outside
development at all. The approval path is exactly what is worth
exercising in production, and a production catalog containing a tool
that does nothing is either honest or confusing depending on who is
reading it.

**Reversal cost:** cheap either way.

### The Milestone 4 gap was narrower than the review said

**Decided:** the readiness review's sentence that *"Path traversal is
rejected"* stood on *"an algorithm that no document contains"* is
now in the past tense, and the finding around it re-tensed with it.

**Why:** `sandbox-isolation.md` was written after that review and
specifies `WorkspaceHandle.resolve` as a five-step containment rule
with a property gate over it. The claim was true when written and
stopped being true with that document. Only the tool-level half was
still missing, and that is what this pass wrote.

**Note:** two citations into `builtin-tools.md:909` broke, both from
the readiness review and both pointing at the `sandbox.run_command`
Milestone 5 transcription error that `sandbox-isolation.md` corrected
long ago. They are repointed to `builtin-tools.md:1399`, where the
corrected statement lives, and the surrounding prose re-tensed to
match.

**Reversal cost:** none.

## The Milestone 4 scope vocabulary (no ADR)

### One scope vocabulary, not an API one and a tool one

**Decided:** there is one closed set of fourteen strings. Nine were
already enumerated by the API specification and five more appear as
`ToolSpec.required_scopes` on the builtin roster. They share a
namespace rather than occupying two that happen to use the same
spelling.

**Why:** `artifact.read` and `artifact.write` are two actions on one
resource. If the API owned one vocabulary and the tool system
another, those two would be the same string in both, and the first
time they meant different things the bug would be unfindable.
`skill.write` settles it on its own: the API document already
describes it as checked by the policy engine rather than by a route,
which is a tool-system check written into an API scope.

**Alternative:** two prefixed namespaces, `api.` and `tool.`. It
makes the boundary visible at the cost of doubling every scope that
crosses it, and every interesting one crosses it.

**Reversal cost:** expensive after the first profile ships, because
the strings are in operator configuration.

### An MCP tool may require only its own server's scopes

**Decided:** a declared scope is legal if it is one of the fourteen
or if its first segment is `mcp` and its second is the server id.
`mcp` is reserved and cannot be a resource name.

**Why:** MCP `required_scopes` are operator-declared, so a list
closed against them is a list the operator routes around. The
escalation worth blocking by construction is an operator declaring
that a remote filesystem-write tool requires `session.write` — a
line that reads as a restriction and is a grant, because every
principal who can write to a session now reaches a tool nobody
audited.

**Cost:** an operator who genuinely wants an MCP tool gated on a
first-class scope cannot say so. They gate it in the policy profile
instead, which is where risk classification belongs anyway.

**Reversal cost:** cheap; the rule is one registration-time check.

### Scopes are opaque strings compared by exact match

**Decided:** the check is a set difference, all-of. No hierarchy, no
wildcard, no prefix rule. `run.write` does not satisfy `run.read`,
and a tool that means both declares both.

**Why:** the alternatives each buy convenience with a question an
implementer has to answer the same way twice. A hierarchy needs a
rule for whether `run` implies `run.cancel`; a wildcard needs a rule
for whether `run.*` reaches a scope added next year. Both rules are
the kind that get written slightly differently in the authorization
check and in the admin UI that displays what a principal has.

**Cost:** verbose principal configurations. Fourteen strings is a
small enough set that the verbosity is bounded.

**Reversal cost:** cheap in one direction only. Adding a hierarchy
later widens every existing principal silently, so it would need a
migration that enumerates what each one currently reaches.

### The scope denial names the missing scope

**Decided:** `policy.scope.missing` names the scopes the action
required and the principal lacks. It never names the scopes the
principal holds. This is the one denial in the document that names
anything.

**Why:** the document's rule is that a denial states the outcome and
not the reason, because a reason is a gradient a model can climb.
A scope is not a gradient. A model cannot grant itself a scope, and
the sentence is being written for the human who reads the transcript
and has to work out whether the principal is misconfigured or the
tool is over-declared. The held set is withheld on the other half of
the same reasoning: it is a map of the surface still worth probing.

**Reversal cost:** none.

### The scope set is stamped on the run, not re-derived

**Decided:** a new `runs.principal_scopes JSONB NOT NULL` column is
written at submission. `PrincipalResolver.for_run` reads the stamp
and never a principal table.

**Why:** a worker holds no credential, so it has nothing to resolve
a principal from. Re-deriving at execution would also make the
runtime loop's *"takes effect on the next run"* depend on queue
latency rather than on submission order, which is the difference
between a guarantee and a usual outcome. ADR-0032 chose the same
shape for the consent stamp, and for the same reason: a grant is a
statement about work not yet submitted.

**Note:** approval resumption deliberately ignores the stamp for the
revalidation step. A run that resumes after its principal's scopes
were narrowed revalidates against the stamp for consistency of the
decision it already made, and the next run is denied. Voiding
mid-run would make a narrowing operation abort work in flight, which
is a different and louder decision than the one an operator thinks
they are making.

**Reversal cost:** expensive; it is a column with a writer at
Milestone 2 and a reader at Milestone 4.

### `JSONB` rather than `TEXT[]` for the stamp

**Decided:** `JSONB`, matching `tool_definitions.required_scopes`.

**Why:** the same data already has a representation in this schema.
One concept with two representations is a conversion function that
somebody writes twice and gets subtly different both times.

**Cost:** no array containment operators, which is fine because
nothing queries a run by its scopes.

**Reversal cost:** cheap; one migration.

### `Principal.roles` is declared and not resolved in 0.1

**Decided:** the field is populated and written to the audit record.
Nothing reads it as an authorization input. Role-to-scope expansion
is a later version.

**Why:** the field exists on the model already and deleting it would
be a churn with no benefit. Reading it in 0.1 would mean a second
authorization path that resolves to the same scope set by a
different route, and two paths to one answer is where the two
answers diverge.

**Reversal cost:** none; adding the expansion later changes what
fills `scopes`, not what checks them.

### Development mode binds the fourteen and no `mcp.` scope

**Decided:** `AUTH_MODE=dev` gives its single principal all fourteen
first-class scopes and no scope in the `mcp.` space.

**Why:** a developer should not be blocked by authorization on
anything the platform itself ships, and should be blocked by it on
anything a server they just connected declares. That asymmetry is
the whole point of the reserved prefix, and dev mode is where it is
cheapest to notice.

**Reversal cost:** none.

### The approvals resolve route was documented as `GET`

**Decided:** the route table's
`GET /v1/approvals/{id}/resolve` row is corrected to `POST`. It is a
transcription defect, not a design position — the same document's
request body, the policy specification, and `ApprovalService.resolve`
all describe a state change.

**Why:** a resolve over `GET` is a state change a crawler can
perform.

**Reversal cost:** none.

### Where the configured principal's scope set lives

**Decided:** provisionally a `Settings` field, which keeps the
configuration-file count at six.

**Why:** the alternative is a seventh configuration file, and the
composition specification's own heading counts six.

**Question for you:** a file would make adding a second principal a
data change rather than a deploy, which is the shape this wants as
soon as there is more than one. The `Settings` field is right for
0.1 and probably wrong for 0.2, and I would rather you knew that
than discovered it.

**Reversal cost:** cheap while there is one principal.

## The Milestone 7 history predicate (no ADR)

### Selection is specified inside the context engine, not in an ADR

**Decided:** the history predicate is written into
`docs/plan/context-engine.md` and no ADR records it.

**Why:** an ADR earns its place when a decision binds documents that
would otherwise diverge. This one binds a single spec's own build
sequence and adds one column that the event-log spec already had a
place for. The Milestone 4 scope vocabulary was settled the same way
and for the same reason.

**Reversal cost:** cheap; an ADR can be written over a decision
already recorded in prose.

### Selection is two functions, not one

**Decided:** seeding a run and assembling a request are separate
selections with separate inputs, and the readiness finding is treated
as covering both.

**Why:** the finding says "stable across two runs with the same
input", and there are two places that phrase can fail. Assembly reads
the checkpoint, which is closed and ordered. Seeding reads a
projection, which is neither. Writing one predicate over both would
have hidden the harder half.

**Reversal cost:** none.

### The retained set is a contiguous suffix, not a ranking

**Decided:** history is a tail of the ordered item list, chosen by a
single cut index. No relevance ranking over past turns.

**Why:** a ranked subset produces a transcript with holes, and a
model reading a hole reads a conversation in which the missing thing
never happened. It would also be a second retrieval system beside
in-turn recall, which already exists to pull back the older thing
that matters — and with two of them, a turn that should have been
present and was not is ambiguous between a selection defect and a
ranking miss.

**Alternative:** rank by relevance and keep the highest-scoring
turns. It sounds better than it is: every objection above is a
consequence of it, and none of them surfaces in a demo.

**Reversal cost:** high once anything depends on the ordering.

### Seeding reads the log at a pinned sequence

**Decided:** `seed_checkpoint` reads session history strictly below
`runs.seed_event_sequence`, the sequence of the
`user.message.created` event the run answers, rather than reading the
projection as it stands.

**Why:** `seed_checkpoint` has two call sites — run creation and the
rebuild forced when a run's checkpoints are gone — and they can be
hours and a deploy apart. A live read makes the second seed a
different conversation from the first, which is the failure the
Milestone 2 dispensability gate exists to detect and would instead
have been causing.

**Cost:** one nullable column on `runs`, written in a transaction
that already allocates the value.

**Reversal cost:** none while it is additive; expensive after runs
exist without it, which is why it is a Milestone 2 column.

### Child runs select no session history

**Decided:** `runs.seed_event_sequence` is null for the child runs of
Section 27.6, and a null sequence selects nothing.

**Why:** Section 27.6 already says a child returns a concise result
and never writes to the parent's conversation. Seeding it from the
parent's history would make the relationship asymmetric in the
direction that costs the most: unbounded context, a wider redaction
surface, and a subagent that can be steered by conversation it was
not given on purpose.

**Question for you:** this is closer to a product decision than an
engineering one. A subagent that cannot see the conversation that
spawned it needs the parent to restate anything it depends on, and a
parent that forgets to will produce a child that confidently answers
the wrong question. The alternative is to pass a bounded excerpt the
parent names explicitly, which is more work and more honest. I chose
the strict version for 0.1 because it is the one that can be relaxed
later without breaking anything.

**Reversal cost:** cheap; the column already carries the value that
would be needed.

### One gate, not two

**Decided:** `gate.context.history_cut` at Milestone 7 covers the
assembly half. The seeding half gets no gate of its own.

**Why:** the Milestone 2 dispensability gate already asserts that a
run whose checkpoints were deleted resumes to the same terminal
state, which is untestable unless reseeding is deterministic. A
second gate over the same property would pass or fail with the first.

**Reversal cost:** none.

### `select_history` returns an index

**Decided:** the signature returns the cut index, not the retained
list.

**Why:** an index can only describe a suffix. Returning a list would
make contiguity an assertion somebody has to remember to write, and
the gate would be checking a property the type could have carried.

**Reversal cost:** none.

### A split tool pair moves the cut later, never earlier

**Decided:** when the cut falls between a tool call and its result,
the pair is excluded as a unit.

**Why:** admitting the call instead would add tokens to a set already
at its limit, which turns one boundary adjustment into a loop. The
pair is an atomic budget unit in both directions, and the cheaper
direction is out.

**Reversal cost:** none.

### The cut never falls earlier than the compaction boundary

**Decided:** items below `replaced_through_sequence` are never
re-admitted to the body.

**Why:** they are already represented by the summary at position 7.
Admitting them again states the same turns twice in two voices, and
the paraphrase and the original will not agree about emphasis.

**Reversal cost:** none.

### The token estimator becomes a pure function

**Decided:** `TokenEstimator.estimate` stays approximate and gains a
requirement that it be a pure function of its arguments.

**Why:** approximation moves the cut to a slightly wrong place, which
is a tuning question. Non-determinism moves the cut to a different
place on two identical calls, which is the failure the predicate
exists to prevent. Nothing in the earlier wording ruled out a
sampled or clock-sensitive implementation.

**Note:** a cache is still permitted. It may change how long
`estimate` takes and never what it returns.

**Reversal cost:** none.

### The compaction summary's stated position was wrong

**Decided:** `context-engine.md` said the summary "sits at position 6
in the assembly order" and now says position 7.

**Why:** rows 5 and 9 were inserted by the skills specification and
this sentence was not renumbered with them. Position 6 is the
session-open memory snapshot.

**Reversal cost:** none.

## The Milestone 8 MCP authentication gap (no ADR)

### Authentication is specified inside the tool system, not in an ADR

**Decided:** the authentication scheme is written into
`docs/plan/tool-system.md` and no ADR records it.

**Why:** an ADR earns its place when a decision binds documents that
would otherwise diverge. This one adds four columns to a table that
document already owns and three gates to a list it already keeps. It
touches the sandbox spec only by reading its tier-0 variable list
rather than copying it, which is the opposite of divergence. The
Milestone 4 scope vocabulary and the Milestone 7 history predicate
were settled the same way and for the same reason.

**Reversal cost:** cheap; an ADR can be written over a decision
already recorded in prose.

### The scheme is configuration, not something the broker infers

**Decided:** `mcp_servers` gains an `auth_scheme` column. The broker
resolves a reference to bytes and is never asked what those bytes
are for.

**Why:** a bearer token and an OAuth client secret are both opaque
strings. A resolver that infers between them is a resolver that
eventually presents a client secret as a bearer token to a server
that logs its `Authorization` headers, and the failure is silent on
our side and permanent on theirs.

**Alternative:** store the scheme in the secret alongside the value,
which some brokers support. Rejected for three reasons that each
stand alone: validating a row would then require dereferencing a
secret, secrets rotate and protocols do not, and the scheme appears
in operator-facing errors and in `mcp.server.disconnected`, which is
a place the emitter is forbidden to look for secrets.

**Reversal cost:** none.

### Five schemes, and no mutual TLS

**Decided:** the closed set is `none`, `bearer`, `header`,
`oauth2_client`, and `env`. A client certificate is not among them.

**Why:** the five cover every deployment shape the corpus already
describes, and a closed set is what makes write-time validation
possible at all. Mutual TLS is left out because a client certificate
is a property of the connection rather than of the request, so it
belongs in the egress proxy's configuration rather than in a row
that describes what to put in a header.

**Question for you:** if an operator asks for mutual TLS, the answer
is to configure it at the proxy, not to add a sixth scheme. Say so
if you disagree, because the sixth scheme is the cheaper-looking
option and someone will propose it.

**Reversal cost:** low for a sixth scheme, higher for mTLS, which
would want a place to put a certificate and a key rather than one
reference.

### Configuration is validated when written, not when dialled

**Decided:** every rule about schemes, names, transports, and token
endpoints is checked at the point the row is written.

**Why:** a bad row should be a configuration error a human sees
immediately, not a connect failure a tenant discovers when a tool
they were advertised does not work. It also means the checks run
with no server and no broker, which is why the gate over them needs
neither.

**Cost:** the validator has to know things the dialler also knows,
including the egress allowlist and the sandbox's tier-0 variable
list. The list is read from `sandbox-isolation.md`'s definition
rather than copied, so the two cannot drift.

**Reversal cost:** none.

### Three gates rather than one

**Decided:** `gate.tool.mcp_auth_config`,
`gate.tool.mcp_reauth_bounded`, and `gate.tool.mcp_stdio_env_built`.

**Why:** they divide by what each needs in order to run. The first
needs a validator and nothing else. The second needs a server that
will return 401 on demand. The third needs a child process whose
environment can be read back. One combined gate would be unrunnable
until the last of those three fixtures existed, which would put the
cheapest check behind the most expensive one.

**Reversal cost:** none.

### A 401 buys one re-authentication and at most one retry

**Decided:** the ladder runs at most once per server per session, and
the retry it permits is the retry the recovery table already permits.

**Why:** routing through the recovery rules rather than around them
keeps `mark_effect_sent` the thing that decides retryability. A 401
arriving after an effect was sent says nothing about whether the
effect landed, so a non-idempotent call in that state is `UNCERTAIN`
and is not retried. A ladder that made its own retry decision would
be a second, quieter answer to a question the spec already answers.

**Note:** if the re-resolved credential is byte-identical to the one
that failed, the retry is skipped. Presenting the same rejected bytes
again is a round trip that cannot succeed.

**Reversal cost:** none.

### Expiry is checked at use, and there is no background refresh

**Decided:** the transport compares recorded expiry against the clock
when it builds a header, with a fixed sixty-second skew. Nothing
refreshes on a timer.

**Why:** a background refresh is a second clock in a system that
already has a lease epoch and a worker heartbeat, it keeps tokens
alive for connections nobody is using, and it does not remove the 401
path, because a token can be revoked before it expires. Check-at-use
costs one comparison on a path that is already doing I/O.

**Reversal cost:** none.

### Refresh tokens are not used

**Decided:** the `oauth2_client` scheme re-runs the client-credentials
grant and stores no refresh token.

**Why:** the grant is not supposed to issue one. A server that does
is either misconfigured or running a different flow, and holding a
long-lived credential we did not ask for is a liability with no
corresponding capability.

**Reversal cost:** none.

### The user-delegated OAuth flows are deferred, and said so

**Decided:** authorization code and dynamic client registration are
out of scope for 0.1. A server that requires one fails to connect
with `tool.auth_unsupported`.

**Why:** they need a browser redirect, a callback URL, and a
per-principal token store. `conversation.ask_user` suspends a run to
collect text, not to collect a redirect, so there is no existing
surface to hang this on. Failing to connect with a named reason is
better than a scheme that half works.

**Question for you:** the unlock is an interactive authorization
surface on the HTTP API, which is a real piece of product and not
just a protocol chore. If you want to connect to servers that only
speak user-delegated OAuth before 0.1 ships, this needs to move up
and it needs a design of its own.

**Reversal cost:** moderate; the scheme vocabulary has room, but the
token store and the redirect handling are new.

### A stdio server's child environment is built, not inherited

**Decided:** the child receives the synthesized sandbox tier plus the
one declared credential variable, and nothing else. The credential
never reaches `argv`.

**Why:** inheritance is the default behaviour of every
process-spawning API in the standard library, and what would be
inherited here is the worker's database URL and every provider key,
handed to an operator-configured third-party process. That is why it
is a gate with a planted sentinel rather than a sentence in a
paragraph.

**Cost:** a server that expected some ambient variable will fail, and
the fix is to declare it, which the schema does not currently allow
for anything but the credential. That is deliberate for now.

**Reversal cost:** low; a declared extra-environment field is an
additive change.

### Four pre-existing defects fixed on the same pass

**Decided:** four sentences that had fallen behind what they describe
were corrected rather than left for the next reader.
`tool-system.md`'s availability table said "Three different things
can make an advertised tool uncallable" over a table that already had
four rows, and now says "Several things ... and every one of them".
`evaluation-harness.md` said "seventy-one of the hundred and fifty-six
declared gates are not case gates", stale from before the Milestone 7
gate, and now says seventy-three of one hundred and sixty.
`milestone-map.md`'s opening said "one hundred and fifty declared
across thirteen specs, one hundred and fifty-six registry entries",
stale by one from the same pass, and now says one hundred and
fifty-four and one hundred and sixty. Its third open question said the
MCP half "still registers no invariant of its own", which stopped
being true when gates 11 through 13 were added, and now records both
later passes.

**Why:** three of the four are counts, and a count that has fallen
behind is the failure the derived census exists to prevent. The prose
numbers are not generated, so they are worth re-deriving on every
pass rather than trusting — which is how three of these were found,
by grepping the corpus for every spelled-out census figure after the
new ones were written.

**Note:** the harness one also carried a ninety-seven character line
in a file whose convention is eighty-three.

**Reversal cost:** none.

## The Milestone 9 knowledge-document gap (ADR-0033)

### A fourteenth specification rather than a section in either memory document

**Decided:** knowledge documents get their own specification,
`docs/plan/knowledge-documents.md`, and their own ADR.

**Why:** a belief and a document answer different questions. A belief
answers *what is true* and the unit of retrieval is the claim; a
document answers *what does the source say* and the unit of retrieval
is the passage, quoted verbatim and cited. Both memory specifications
open with a scope line that says beliefs and episodes, so hosting
knowledge inside either one means contradicting the sentence the
document starts with.

**Alternative:** a long section in
`memory-retrieval-and-ranking.md`. Rejected on the same test that
rejected a fourteenth spec once before, for trajectory export — does
the new subject own what it needs, or borrow it. Trajectory export
borrowed almost everything. Knowledge owns ten things: the document
model, ingestion, chunking, the index, the scope model, retrieval
over chunks, rendering and citation, retention, the tool surface, and
its gates. It borrows four: the artifact store, the event log, the
budget, and the trace.

**Cost:** three live sentences stated "thirteen specs" and had to be
corrected, and the census had to be re-derived. That was the whole
ripple, and it was measured before the decision rather than after.

**Reversal cost:** low while nothing is built. The document could be
folded into the memory spec by concatenation.

### `knowledge` is a fourteenth gate area

**Decided:** twelve gates in a new area, `gate.knowledge.*`.

**Why:** the same precedent `skill` set at the thirteenth area. An
area names a subject, one specification owns the subject, and the
gates are statements about that subject rather than about the
repository.

**Alternative:** folding them into `memory`. `memory` already holds
two specifications sharing an area, so the precedent exists — but it
holds two specifications about *one subject*, which is the reason it
was allowed to. Knowledge is a second subject.

**Note:** the identifiers are shorter than they read naturally
because the milestone map's id column is thirty characters and
`gate.knowledge.` consumes fifteen of them. `no_secrets`, not
`no_secrets_ingested`.

### Ingestion is a tool, not a route and not a CLI noun

**Decided:** one builtin, `knowledge.ingest`, admitted through the
tool pipeline.

**Why:** two closed lists said no to the alternatives.
`http-api-and-streaming.md` closes the API at thirteen routes and
states that an artifact is not uploaded through this API in 0.1, so a
knowledge upload route would reopen a list that was deliberately
shut. The CLI is closed at twelve nouns. A tool needs neither list
reopened, and it puts admission through the policy engine, the
approval path, and the event log without any new machinery.

**Cost:** ingestion is only reachable from inside a run. An operator
bulk-loading a corpus has to drive it through the agent, which is
slower than a route would be.

**Question for you:** if bulk loading matters before 0.1, the answer
is a management surface rather than a fourteenth route — say so and
it moves up.

### The secret scan blocks an ingest; the injection scan does not

**Decided:** a detected credential refuses the whole ingest and
nothing is written. Instruction-like text is recorded on the chunk as
`instruction_like` and ingested anyway.

**Why:** the two failures are not symmetric. A credential in a
permanent, retrievable corpus is unrecoverable — it is quoted back
verbatim into a context window by design, which is the one place a
secret must never be. Instruction-like text is survivable by
labelling: the passage arrives inside a `<knowledge>` block at
`TrustLevel.KNOWLEDGE`, the policy engine already refuses to treat
untrusted context as instruction, and a blocking injection scan would
refuse most real technical documentation, because a deployment guide
is mostly imperative sentences.

**Reversal cost:** low. The flag is on the chunk, so a later policy
could filter on it.

### No chunk overlap, and heading paths instead

**Decided:** chunks are structure-first with a target of six hundred
tokens, a ceiling of a thousand, a floor of a hundred, and no
overlap.

**Why:** the citation *is* the chunk id. Overlapping chunks mean the
same sentence has two ids, which makes a citation ambiguous and makes
deduplication a ranking problem rather than a fact. Overlap exists to
restore context a hard cut removed; a heading path restores it
cheaper and exactly, and a document that needs the neighbouring chunk
can be asked for it.

**Cost:** a claim split across a chunk boundary is retrievable only
as two passages. The floor of a hundred tokens and the structure-first
split make that rarer than a fixed-window splitter would.

**Note:** the chunker is deterministic under a `chunker_version`
because a citation that stops resolving after a library upgrade is a
broken citation, and there is a property gate on exactly that.

### Visibility replaces the principal id as the isolation predicate

**Decided:** a document carries `visibility` in `{principal, project,
tenant}`, and retrieval filters on it rather than on `principal_id`.

**Why:** this is the exact inverse of the rule the memory layer took,
and the inversion is the point. A belief travels unless it is pinned,
because you asked the agent to learn across projects. A document is
shared unless it is scoped, because a document is a thing an
organization has rather than a thing an agent inferred, and the
common case for admitting one is that more than one person should be
able to quote it.

**Cost:** the default is the widest of the three, so a mis-set
visibility over-shares rather than under-shares. The gate on it is a
case gate rather than a property gate for that reason — it is worth
an explicit scenario.

### Knowledge passages get their own budget class and yield first

**Decided:** a Region B class of three passages or three thousand
tokens, first in the yield order, ahead of in-turn recall.

**Why:** a passage is roughly three times the weight of a belief and
cannot be trimmed by sentence without becoming a misquotation, so it
cannot share recall's elastic allowance. It yields first because the
corpus is still there — an explicit `knowledge.search` re-reaches it
in one tool call, which is not true of a frozen snapshot.

**Alternative:** taking a share of the two-thousand-token recall
class. Rejected because the two classes then compete on a single
threshold, and a long document would silently evict beliefs.

### Passages drop whole, never truncated

**Decided:** when the class overflows, the lowest-ranked passage is
removed entirely.

**Why:** the model is going to quote and cite what it is given. A
passage shortened to fit is a misquotation attributed to a real
document, which is worse than the passage being absent. There is a
property gate asserting the rendered text matches the stored chunk
exactly.

### No supersession collapse, no per-subject cap, no conflict surfacing

**Decided:** none of the three memory-retrieval mechanisms carry
over. A per-document cap of two passages replaces them.

**Why:** all three exist because beliefs make claims that can
contradict each other, and the retrieval layer is where the
contradiction has to be resolved before the model sees it. Passages
do not make claims — they report what a source says, and two sources
disagreeing is information rather than a defect. The per-document cap
exists for a different reason: to stop one long document from filling
the whole class.

### One new scope, `knowledge.write`

**Decided:** the closed vocabulary goes from fourteen strings to
fifteen.

**Why:** ingestion is the one privileged act here, and the vocabulary
is closed precisely so a new privileged act has to be enumerated
rather than invented at the call site. Reading is governed by
visibility rather than by a scope, because a scope that everyone
holds is not a control.

### The management surface is deferred; the deletion semantics are not

**Decided:** no list, browse, or re-ingest surface in 0.1. Deletion
semantics are fully specified anyway.

**Why:** the surface is a product decision that wants a UI, and
nothing in Milestone 9 needs it. Deletion is different — a corpus
with no defined deletion path is a retention problem the moment the
first document is admitted, and the mechanism costs nothing because
it reuses ADR-0032's consent-withdrawal move: a source artifact is
stored with no `expires_at`, and deleting the document sets one so
the existing sweeper collects it. There are gates on the cascade and
on citations resolving rather than dangling.

**Question for you:** if you want a management surface before 0.1,
it is a small API document rather than an addition to this one.

## Section 29's Device model (ADR-0034)

### An audit of the seam rather than a design

**Decided:** the seventeenth specification does not design the
`Device`. It walks the corpus for every place a deferred device
design will have to touch, and records what it finds.

**Why:** Section 29's own last subsection says *"Defer the Device
concept, presence, device-scoped tool routing, and notifications to
a milestone with concrete use cases"*, and the plan's sequencing
table puts inbound surfaces and pairing at Milestone 10. Writing
contracts for the four ports Section 29.6 names would be building
the thing the plan defers, inside a document whose job is to expand
the plan rather than overrule it.

**Alternative:** write the design anyway and mark it deferred.
Rejected because a design nobody is allowed to build is a design
nobody reviews, and it would have been the only specification in the
corpus that no milestone reaches.

**Reversal cost:** low. The audit is a list of places, and a later
design consumes the list rather than replacing it.

### The document declares no gates

**Decided:** no `## Hard gates` section. The census stays at 166
declared, 175 declarations, 172 registry entries.

**Why:** a gate is a test that runs against built code, and this
document authorizes no code. The one obligation Section 29 actually
places on 0.1 — that a second client can attach to a session and
replay it — is already a hard gate somewhere else,
`gate.api.replay_exact` at Milestone 5.

**Cost:** gate-less specifications go from two to three. The other
two, `bootstrap-and-composition.md` and `development-toolchain.md`,
are gate-less for the same reason: they describe arrangement rather
than behaviour.

### The four Section 29.6 ports get a placement and nothing else

**Decided:** each of the four is named with the module it will live
in, and no signature is written.

**Why:** the readiness review recorded that the model had no home,
which is a question about placement rather than about contracts.
`bootstrap-and-composition.md` already answers placement in general:
a port lives in the module named for the capability it abstracts,
not for the component that calls it. Applying a rule that already
exists is not new design; writing four method signatures would be.

**Note:** this is why the document is useful at all. A placement can
be checked against the rule today. A contract could only be checked
against a use case nobody has yet, which is the same use case
Section 29.8 says it is waiting for.

### A device that attaches mid-session is invisible until the next one

**Decided:** presence does not change what a session advertises. A
device that attaches after a session opens contributes no tools to
that session.

**Why:** `tool-system.md` pins the advertised tool prefix at session
open and resolves availability at call time, and it already handles
the disconnect direction — a device that goes away yields
`unavailable` with `tool.device_offline`. The attach direction has
the same shape as an MCP server sending
`notifications/tools/list_changed`, which the same document records
and does not apply, with the new tools available at the next session
open. Resolving attach any other way means two rules for one
behaviour.

**Question for you:** the user-visible cost is that plugging in a
phone mid-conversation does not make the phone's tools usable until
the next session. That may be exactly wrong for a product whose
premise is multi-device. If it is, the fix is to unpin the prefix
for every source at once, not to special-case devices.

### Per-device scopes are an intersection, and never a new prefix

**Decided:** a per-device scope set is the intersection of the
principal's scopes and the device's, computed once when the run is
submitted, and every string in it comes from the closed fifteen.

**Why:** `runs.principal_scopes` is stamped at submission and
`PrincipalResolver.for_run` reads the stamp and never a table, which
is hard gate 13, *"The scope set is the run's."* An intersection
computed before the stamp needs no change to the policy engine at
all — the engine is never told a device exists. The constraint that
travels with it is that a device may not introduce a `device.` scope
prefix, because the vocabulary is closed at fifteen strings with one
exception for `mcp`, and hard gate 11 enforces that grammar. The
`device.` that exists today is a tool-name domain, an unrelated
namespace that happens to share a word.

**Reversal cost:** low now, high later. Making device scopes
anything other than a subset means changing the grammar gate, the
stamp, and the resolver together.

### Device lifecycle events are named as a gap, not placed

**Decided:** the document records that a device attaching,
detaching, or being revoked has nowhere to be written, and stops
there.

**Why:** `events.session_id` is `NOT NULL` and the uniqueness
constraint is `(session_id, sequence)`, so the append-only log has
no shape for an event that belongs to a principal rather than to a
session. Every fix is a schema change: a nullable column, a second
log, or a synthetic session. Choosing among them is design work on
the deferred subject, and choosing wrong is a migration.

**Question for you:** where should device lifecycle events go? My
weak preference is a separate audit table rather than a nullable
`session_id`, because that constraint is doing real work everywhere
else in the log.

### `NotificationService` is a name with nothing behind it

**Decided:** recorded as a gap. Not defined here.

**Why:** the name appears exactly twice in the whole corpus, once in
the plan's port list and once in ADR-0011, and neither says what it
does. It could be one port or two: pushing a message to a device is
a transport concern, deciding whether to notify at all is a policy
concern, and the two have different testability.

**Question for you:** one port or two? Two is my weak preference, on
the same split the corpus already draws between the tool broker and
the policy engine.

### Three plan sentences are corrected in the specifications' favour

**Decided:** where Section 29 and a specification disagree, the
specification wins and the plan sentence is the one recorded as
needing correction.

**Why:** the specifications are newer, they carry gates, and the
plan delegates to them by name. Concretely: the registry accepting
new entries from *"exactly two sources"* is wrong once attach
exists, because attach is a third; Section 29.5's *"queue or
reject"* is reject, because ADR-0004's partial unique index makes a
second active run on a session impossible to enqueue rather than
merely unwise; and presence-based exposure yields to the pinned
prefix, as above.

**Note:** none of the three is edited in `engineering-plan.md` on
this pass. Correcting them in place would settle the deferred
design's conflicts before the design exists. Recording them means
the implementer meets each one with the resolution already attached.

### A paired Surface is a Device with an empty capability set

**Decided:** no second model. A Surface — a phone, a chat client, an
email bridge — is a `Device` that advertises no tools.

**Why:** the two differ in what they can do, not in what they are.
Both attach to a principal, both are clients of the same API, both
need the same revocation path. A second model would duplicate
identity, pairing, and revocation in order to express *"and it has
no tools"*, which an empty set already expresses.

**Question for you:** what trust label does a message that arrives
through a paired Surface carry? A message a user types on their own
phone is `USER`. A message that arrives as an email through a bridge
is not, and the corpus has no rule yet for the middle.

### The order the deferred work lands in is left open

**Decided:** the five gaps are listed with what each will cost, and
not ordered.

**Why:** even the cheapest sequencing question — whether the
`Device` table lands before device-scoped tool routing — has an
answer that depends on which use case forces the work, and Section
29.8 defers precisely until there is a concrete use case. Ordering
them now would be inventing that use case.

**Question for you:** if you want an order anyway, my weak
preference is identity first — the table, pairing, and revocation —
with routing and notifications after. Identity is the part every
other piece needs, and it is the part that is a migration.

## Milestone 10 and readiness open question 4

### Answered by measuring, not by adding a heading

**Decided:** Milestone 10 is correctly an open direction. It gets no
`#### Acceptance criteria` heading and no implement list.

**Why:** the question was whether the missing criteria are an
omission or a choice, and the way to tell is to look at what the
milestone's own conditions are. Two of its four parts gate on
evidence rather than on a date — *"Implement a scheduler only after
durable on-demand runs are reliable"* and *"Add subagents only when
evaluation evidence shows that a single agent fails"*. Acceptance
criteria are a promise about a delivery. A promise cannot be made
about work whose own entry condition says it must not start until
evidence arrives, and inventing one would be worse than the missing
heading, because every other milestone's criteria are promises this
plan can keep.

**Question for you:** if you would rather Milestone 10 read
uniformly with the others, the honest form is a heading that states
the entry gates as the criteria — "subagents are not built until
evaluation evidence shows a single agent fails for one of five named
reasons" is a criterion, just not a delivery one. I did not write it
because it restates the gate rather than adding anything.

### The review's own subagent count was stale

**Decided:** corrected in place, with the finding kept and re-tensed
rather than deleted.

**Why:** the readiness verdict said five of the nine subagent
requirements had no design, naming restricted context and the child
deadline among them. Both are designed now. `context-engine.md:278`
makes `runs.seed_event_sequence` nullable for child runs, which
*"seed from a parent's concise instruction rather than from session
history"*, and `memory-retrieval-and-ranking.md:87` gives child runs
their own recall class at fifteen beliefs against an interactive
run's forty. `runtime-loop.md:1141` says *"the parent's
`deadline_at` is copied onto every child at creation"*. All three
documents were written after that verdict. Five of the nine are
supplied, two are partial, and two still have none.

**Question for you:** none. This is a correction of fact, and the
house rule is that a review's findings are kept as the record with
the closing appended, which is what I did.

### A child run cannot be inserted into its parent's session

**Decided:** recorded as a conflict in the readiness review, not
resolved.

**Why:** `event-log-and-persistence.md:724` enforces Section 27.5's
one active run per session with a unique index on `session_id` where
status is not `COMPLETED`, `FAILED`, or `CANCELLED`. A parent
suspended on a child waits in `WAITING_FOR_APPROVAL` carrying a
typed suspension kind — `runtime-loop.md:290` chose that over an
eighth status — and that is not one of the three. So the parent row
still occupies the session, and a child row in the same session
violates the index. Section 27.6 offers *"the parent's session or a
dedicated child session per policy"*; only the second is
implementable as the schema stands, and no policy is written to
choose between them.

**Question for you:** resolving it is Milestone 10 work and this
documentation pass authorizes none, so it stays recorded. When it
comes up, my weak preference is to delete the branch rather than
write the policy: a dedicated child session always, because the
alternative needs the index predicate widened to exempt child runs,
and that predicate is the thing keeping one active run per session
true.

### `delegate.run` has a carrier and no schema

**Decided:** noted as partial, not designed here.

**Why:** `tool-system.md:916` registers `delegate.run` as a control
tool that spawns a child run and suspends the parent, and
`context-engine.md:278` says the child seeds from a parent's concise
instruction. Nothing types that instruction. The plan's *"explicit
objective"* requirement therefore has a delivery path and no input
schema anywhere in the corpus.

**Question for you:** whether the objective should be a plain string
or a structured brief with a success condition. I have no view worth
recording, because the answer depends on what the first real
delegation is for, and Milestone 10's own gate says that should be
driven by evaluation evidence.

## The closing adversarial pass

### Ten findings, eight confirmed and two cleared

**Decided:** run one pass whose only job was to falsify the claim
that the corpus is complete, then verify every finding it raised
against the sources before treating any of them as fact.

**Why:** the corpus had reached the point where every plan section
had a specification and no specification named a blocker, which is
exactly the point at which a completeness claim is least likely to
be tested. So the pass was prompted to falsify the claim rather
than to confirm it. It raised ten findings. Eight survived checking
and are corrected in this pass. Two did not: one was an explicit
deferral, and `readiness.md:56` already rules that a deferral
recorded as a deferral is not a gap, and the other was already an
open question in the document it was raised against and already an
entry in this file.

**Question for you:** none. The pass is the kind of work the
standing instruction covers, and everything it produced is in the
corrections below.

### The approval routes are Milestone 5, and an earlier verdict was wrong

**Decided:** narrow `policy-and-approvals.md`'s build step 11 to
the approval service read methods and the CLI commands that call
them, and leave all three approval routes at Milestone 5 with every
other route in the API.

**Why:** an earlier pass recorded this contradiction with the
opposite verdict, reading `http-api-and-streaming.md` as the
document in error. Checking the sources reversed it. Milestone 5's
implement list in the plan is the entire HTTP surface, down to the
error envelope and the health endpoints, and Milestone 1 has no
HTTP API at all, so no route can land before Milestone 5. And what
`agent approval list` calls is the application service, not the
route: `policy-and-approvals.md:994` says so in the same paragraph
that adds the routes, and decision 3 of
`http-api-and-streaming.md` is the reason, because a service that
raised an HTTP error would make the CLI import a web framework. So
Milestone 4 owes the routes their service, which is what "Approval
API and CLI" in Section 21's Milestone 4 implement list means here,
and Milestone 5 owes the routes.

**Question for you:** none, but this one is worth knowing about.
Had the earlier verdict been trusted, three routes would have moved
to the wrong milestone on the strength of a summary rather than a
source. Every finding in this pass was checked against the files
before it was written down, and this is the one that changed sign.

### `skill_manage` is not a name the registry can hold

**Decided:** the registered name is `skill.manage`. The correction
is stated once in `tool-system.md`, which owns the grammar, and
carried by the normative spec block in `skills.md`. The plan's
spelling stays in prose everywhere the string does not matter.

**Why:** the name grammar at `tool-system.md:336` requires at least
one dot in every registry name, and a capability tool is a registry
entry. The `skill` domain already holds `skill.load` and the
`skill.write` scope already names the capability, so the dotted
spelling is the one the domain partition table was built for and no
new domain is needed. The alternative was a corpus wide rename of
every occurrence, which would have rewritten the plan's own
requirement text. The corpus has a precedent against that:
`engineering-plan.md:3324` reclassified this same tool from control
to capability with a pointer paragraph rather than a rewrite, and
that paragraph now carries both corrections.

**Question for you:** whether the prose should be renamed too. My
view is that it should not. The plan's requirement lines are the
record of what was asked for, and the one string that has to be
exact is now exact in the two documents that specify it.

### The memory management surface is three port methods and nothing else

**Decided:** recorded and sharpened in the readiness review, not
designed.

**Why:** the review already listed this among five partials, and it
is sharper than the review recorded. The `MemoryStore` port
declares `list`, `edit`, and `delete` at
`memory-formation-and-consolidation.md:357`, and no tool, route, or
command in the corpus calls any of them. The agent facing surface
is two tools, `memory.search` and `memory.recall_episodes`, and
both read. None of the twelve CLI commands is a memory command, and
no memory route is registered. So *"A user can inspect and delete
stored memories"* at `engineering-plan.md:2773` is the one
Milestone 9 acceptance criterion with nothing behind it to test,
and the three port methods are the shape of an answer rather than
the answer.

**Question for you:** what the surface should be. A management tool
on the `skill.manage` precedent, a route set, a CLI command, or all
three. I did not choose, because edit semantics over an append only
belief store that carries provenance and supersession is real
Milestone 9 design and this documentation pass authorizes none.
Reversal cost of deciding later is **cheap**; the cost of deciding
now and deciding wrong is a migration.

### Five smaller corrections, and what the citation check cannot see

**Decided:** fixed in place.

**Why:** two were cross references that pointed at the wrong place.
`readiness.md` cited `engineering-plan.md:3215` twice for a
requirement that sits at 3227, and
`bootstrap-and-composition.md` sent a live provider job to Section
20.6 instead of 20.4. One was a description of a sibling document
that the sibling had outgrown: `skills.md` said `tool-system.md`
classified `skill_manage` as a control tool while also giving it
`NON_IDEMPOTENT`, which was true of a draft and is not true of the
document. One was a sentence in `milestone-map.md` that still read
as though Milestone 10 added no gates of its own, after `skills.md`
gave it six; it now records the six and narrows the standing
question to the routing and subagent half. And Section 31 of the
plan had no outward pointer to the documents that expand it, which
every other expanded section carries.

**Question for you:** none, but the first and third are worth
naming as a class. `scripts/check_citations.py --update` rewrites
the ledger with whatever text sits at the cited line, so it cannot
see a citation that points at the wrong line, and it cannot see a
quotation in the citing document that no longer matches the cited
text. Both were found by reading. If the check is ever extended,
that is the gap to close.

### Thirteen registry rows carried an identifier the grammar forbids

**Decided:** the four affected tables in `milestone-map.md` widen
their identifier column, and every row in the registry is written
in full.

**Why:** the document sets the grammar itself. An identifier is
`gate.<area>.<slug>`, where the slug is lowercase,
underscore-separated, and unique within its area. A slug holds no
dots, so `gate.runtime.one_terminal_wr..` is not an identifier, and
two of this document's own hard gates fail on one. Gate 5 asserts
that every identifier matches the grammar, and a truncated one does
not. Gate 2 asserts that the registry and `evals/gates/*.yaml` hold
the same identifiers, compared as sets and not as counts, and a
truncated form cannot be a set member of anything. The corpus was
carrying thirteen such rows across four tables, which no
implementation of either gate could have accepted.

**Note:** this answers a question already on the record. The entry
headed *"`gate.tool.mcp_disconnect` is named for the column, not
for clarity"* asked whether to widen the column or keep the
ceiling, and named two truncations. The audit found twelve gates
across thirteen rows, the extra row being an alias restated in a
second table. The ceiling is gone. The naming decision that entry
recorded stands on its own reasoning, since there is one disconnect
gate and the short name is not ambiguous, but the constraint that
produced it no longer exists and no future name needs shortening to
fit a column.

**Note:** `make docs-check` passed green with all thirteen in
place, before the audit and after the fix. Nothing in the toolchain
reads a gate identifier.

**Reversal cost:** low.

### Three runtime gate identifiers had never been spelled in full

**Decided:** runtime loop gates 1, 11, and 12 are registered as
`gate.runtime.one_terminal_writer`,
`gate.runtime.waiting_holds_nothing`, and
`gate.runtime.cancel_keeps_effects`.

**Why:** nine of the twelve truncated gates were spelled in full
somewhere else in the corpus and were restored from there, the
eight sandbox ones from `sandbox-isolation.md` and
`gate.event.checkpoint_dispensable` from both
`event-log-and-persistence.md` and `runtime-loop.md`. The other
three existed nowhere in full. Their declarations in
`runtime-loop.md` read *"One terminal writer"*, *"A waiting run
holds nothing"*, and *"Cancellation never abandons an effect"*, and
each completion above is the reading consistent with both the
surviving prefix and the declaration. The third has independent
corroboration: the census in `milestone-map.md` already describes
Milestone 5 as *"the API surface, the stream, cancellation keeps
effects"*.

**Question for you:** confirm the three spellings. They are the
only identifiers in the registry that this pass named rather than
recorded, and every other one in the corpus can be traced to a
document that already stated it.

**Reversal cost:** low now — four table rows and this entry — and
higher once `evals/gates/runtime.yaml` exists and tests reference
the identifiers.

### The harness's worked examples named gates that do not exist

**Decided:** the second `evals/gates` example and the `agent eval
gates` output example in `evaluation-harness.md` are corrected to
use registry identifiers, and the two milestone counts in the
second are corrected against the census.

**Why:** they used `gate.policy.prompt_is_not_authorization` and
`gate.tool.watermark_contract`. The registry holds
`gate.policy.prompt_not_authz` and `gate.tool.watermark_first`.
Gate 2 of `milestone-map.md` compares the registry against
`evals/gates/*.yaml` as sets, so a worked example of that very file
naming an identifier the registry does not hold is a worked example
that fails the gate it illustrates. The document's first example
uses a real entry with its real kind and milestone, so this is a
deviation from the document's own convention rather than a licence
to invent.

**Note:** `gate.tool.watermark_first` is a case gate at Milestone
1, and the example is a Milestone 4 listing, so it could not be
renamed in place. It is replaced by `gate.builtin.listing_stable`,
a property gate at Milestone 4, which keeps the example's point
that a milestone listing mixes areas.

**Note:** the counts read 28 gates for Milestone 4 and 12 for
Milestone 5. The census says 22 and 11, and 28 is Milestone 1's
count. The pass and pending figures move with them so the example
still adds up, and the sentence below it that read *"the twelve
gates arriving next"* now reads eleven.

**Reversal cost:** low.

### Nothing in the toolchain reads a gate identifier

**Decided:** the audit stays a record and is not turned into a
check in `scripts/`.

**Why:** the checks that would have caught all of this are already
specified. They are gates 2, 5, and 6 of `milestone-map.md`, all
Milestone 0, with a named home in `tests/gates/` and a registry in
`evals/gates/*.yaml` that does not exist yet. Writing a partial one
into `scripts/check_docs.py` now would begin Milestone 0, pre-empt
where the check lives and what it reads, and create a second
authority on a question the corpus has already answered once.

**Note:** what the audit did, for whoever writes those gates. It
parsed every registry row out of the fenced tables and asserted
four things. Every identifier matches the grammar and its area is
one of the fourteen. No identifier repeats. Every well-formed
identifier appearing anywhere in `docs/plan/*.md` is a registry
entry. And the per-kind and per-spec counts in the harness's own
table, together with the per-milestone counts in the census, are
what the registry actually holds. All four pass now. The third is
the one that found the harness examples, and it is the one no gate
currently states.

**Note:** a malformed identifier written in prose is not an
identifier. `milestone-map.md` now quotes one deliberately, to show
what the grammar excludes, and says so where it quotes it, so gate
5 is not implemented as a scan of free text.

**Reversal cost:** none. This defers work rather than doing it.

### The engineering plan owns two gates and has no hard-gates section

**Decided:** the `spec` field of `gate.structure.import_boundary` and
`gate.structure.no_committed_secrets` names the anchor of the
engineering plan's *"Milestone 0: Repository and engineering
foundation"* heading, and hard gate 4 is read as its own title says —
the anchor an entry names resolves — rather than as a literal test
for the string `#hard-gates`.

**Why:** hard gate 4 of `milestone-map.md` says each entry's
`docs/plan/<file>.md#hard-gates` anchor exists in the built site.
Fifteen documents carry that anchor and the built site confirms all
fifteen. `engineering-plan.md` is not one of them, and it owns two of
the hundred and seventy-two entries — the harness's own per-spec kind
table records them as a row reading *"Engineering plan"* with two
structural gates. Under the literal reading the gate fails on those
two the day it is written, and the failure has no fix inside the
registry.

**Note:** the anchor-agnostic reading is not an invention. Hard gate
4's title is *"Every `spec` field resolves"*, and
`evaluation-harness.md` states the rule as *"a gate whose `spec`
anchor does not resolve fails the docs check"*. Only the illustrative
form in the gate's body names `#hard-gates`, and rule 5 of the
milestone map introduced that form to correct two example entries
that pointed at `#evaluation`, which is a narrower claim than a
universal one.

**Question for you:** the alternative is to give `engineering-plan.md`
a `## Hard gates` section holding those two, which makes the anchor
rule uniform at the cost of moving two requirements out of the
Milestone 0 acceptance criteria that state them. I recorded the
exception instead, because relocating a stated requirement is a
larger change than naming an exception to an anchor convention.

**Reversal cost:** low. One section and two `spec` fields.

### Hard gate 7 has two readings and the strict one fails

**Decided:** hard gate 7 is asserted against the build-sequence table
in `milestone-map.md`, which is what its own sentence says, and not
against the per-gate `step` column.

**Why:** only one registry table of fifteen carries a `step` column,
the tool system's. Read per gate, the check fails on exactly two
rows: `gate.tool.crash_recovery` is Milestone 2 against step 4, and
`gate.tool.dedup_concurrent` is Milestone 2 against step 3, and both
of those steps are Milestone 1. Both placements are deliberate and
argued at length in the same document, under *"Tool-system build
steps 3 and 4 without a database"* and under decision 3, which says
a gate whose earlier form would be vacuous is registered at the later
milestone. Nothing recovers before there is persistence to recover
from, and a single-process dictionary cannot lose a race. A check
that failed on both would be relaxed in its first week.

**Note:** the reading the sentence gives does pass. I ran it over the
six specs of the build-sequence table and the three that tag their
own sequences: no spec has a gate at a milestone later than the last
step that builds it. `gate.structure.migration_graph` sits at
Milestone 0 against a Milestone 2 sequence, which is earlier and
therefore allowed, and the engineering plan says why.

**Question for you:** if the intent was the strict per-gate reading,
the two tool-system rows need something in the registry that records
the exception rather than leaving it in prose — an `observes_step`
field carrying a stated reason, on the `optional` field's precedent.
That is a registry schema change and I did not make one.

**Reversal cost:** low now, higher once the check is written against
one reading.

### Three of the map's own hard gates were run by hand and pass

**Decided:** recorded rather than implemented, on the same grounds as
the identifier audit before it. These are Milestone 0 gates with a
named home in `tests/gates/`.

**Why:** what was run, so that whoever writes them starts from a
known state. Gate 1, every top-level numbered item in a `## Hard
gates` section carries exactly one trailing milestone token: fifteen
sections, one hundred and seventy-three items, one hundred and
seventy-three tokens, one per item, none with two and none with
none. Gate 3, a spec's gate count minus its declared alias count
equals the number of entries citing it: all fifteen agree, and the
three aliases are declared in the declaring spec with the owner named
and the owning gate's number given, in the same sentence form each
time. Gate 4, the anchors: fifteen documents carry `#hard-gates` and
the built site confirms every one, with the exception recorded above.

**Note:** the alias arithmetic is the one that moves the headline
number if it is wrong, and it holds per spec rather than only in
aggregate. One hundred and seventy-three items in hard-gates
sections, minus three aliases, is one hundred and seventy rows across
the fifteen tables, and the two the engineering plan declares outside
any such section make one hundred and seventy-two.

**Reversal cost:** none. This is a record of a measurement.

### The case table promised gates it does not carry

**Decided:** the heading now names what the table holds, and the
gate-to-case direction is stated in prose rather than filled in with a
column I would have had to invent.

**Why:** *"The twenty-five cases, with milestones and gates"* headed a
table whose columns are number, case, milestone, kind, and what only
this case proves. There is no gate column and there is no sign there
ever was one. The heading now reads *"with milestones and kinds"*,
`Kind` being the column's own label rather than a word I chose.

**Note:** the arithmetic behind the heading is the larger finding.
Ninety-five of the hundred and seventy-two registered gates declare
kind `case`, which this document defines as a gate that runs as an
eval case, and the document enumerates thirty-one cases. Nothing in
the corpus reconciles the two numbers. Six cases are tied to a named
gate anywhere in prose — 19 and 26 to the two sandbox gates, 20 to the
policy specification's tenth by position rather than by identifier, 28
to the context engine's prefix gate, and 29 and 30 to the MCP set they
arrived with — and the other twenty-five are tied to nothing. So the
enumeration is the floor Section 20.3 asks for and not the size of the
finished suite, which the section now says.

**Question for you:** whether the binding belongs in the registry's
`check` field, which can carry it with no schema change, or in a gate
column on the table, which puts it where a reader looks for it and
duplicates a fact the registry owns. Recorded as the harness's seventh
open question rather than decided, because a column would mean
asserting which of the ninety-five case gates each row satisfies and
the corpus states that for six.

**Reversal cost:** the heading is free to change again. The binding is
cheap to decide before the registry is written and awkward after.

### Eleven cases are writable in Milestone 1, not ten

**Decided:** corrected the four live statements that said ten, and
tied each to the range so the count is checkable.

**Why:** the case table places cases 1 through 11 at Milestone 1, and
[development-toolchain.md](../plan/development-toolchain.md), the
engineering plan, and the harness's own build order all say "cases 1
through 11". Three live statements said "ten of the twenty-five" and
one said a Milestone 1 checkout without the `milestone` field "fails
twenty of the twenty-five", where the figure is fourteen. The harness
contradicted itself: its build order and its Decision 15 disagreed
about the same set, four lines apart in the same document.

**Note:** each corrected statement now names the range as well as the
count, so the number is checkable against the table rather than
merely asserted. That is why the error survived: "ten" was a number
with nothing to check it against.

**Question for you:** `docs/adr/0022-evaluation-harness.md` carries
the same stale figure in its eighteenth decision. It is left alone
under the rule that a decision record is a record at a point in time,
which I think is right, but an erratum note is the alternative and it
is your call rather than mine.

**Reversal cost:** none. The corrected numbers follow from the table.

### The event catalog is fifty-three types, not fifty-one

**Decided:** added `mcp.server.reauthenticated` and
`knowledge.document.ingested` to the consolidated list in
[runtime-loop.md](../plan/runtime-loop.md) and moved the total.

**Why:** *"The event catalog is fifty-one types and is now closed"*
recorded its own cost — *"the catalog is a second place to edit when
an event is added"* — and its own defence — *"Stating the total makes
the next addition visible: a spec that adds an event now has to move
the count."* The cost was paid twice and the defence did not fire. Git
dates the two additions after the consolidation:
[tool-system.md](../plan/tool-system.md) gained the reauthentication
event and [knowledge-documents.md](../plan/knowledge-documents.md)
gained the ingest event, and neither moved the count.

**Note:** a third miss predates the consolidation rather than
following it. The tool system's events table has eight rows and the
consolidation took seven, so `mcp.server.reauthenticated` was in the
corpus and in range when the list was written. That is a transcription
error, not a maintenance one, and it is the argument for the check
being mechanical rather than editorial.

**Question for you:** none. The two are session-scoped, the ingest
path is a tool and therefore runs inside a run, and the arithmetic is
forced.

**Reversal cost:** none. The list is the union of what the corpus
declares.

### Four harness events have no session to be stored under

**Decided:** listed the four `eval.*` events apart from the
fifty-three rather than in with them, and recorded the storage
question as an open one in both documents that reach it.

**Why:** [evaluation-harness.md](../plan/evaluation-harness.md)
declares `eval.suite.completed`, `eval.gate.failed`,
`eval.scenario.scored` and `eval.ceiling.hit` *"on the harness rather
than on the run"*, under a span root it says *"is not `agent.run`"*. A
harness event has no session, and `events.session_id` is `NOT NULL`.
They are event types by every other measure and there is no row they
can occupy. Folding them into the catalog would have hidden that.

**Note:** the same wall is already named, once.
[multi-device-and-surfaces.md](../plan/multi-device-and-surfaces.md)
enumerates three ways out for device lifecycle events — nullable
`session_id`, a separate table, or a synthesized session — picks none,
and calls the second the smallest. Two documents arriving at the same
constraint independently is the finding here; only one of them noticed
it was a constraint. Each now points at the other.

**Question for you:** which way out, and it is one decision rather
than two. My weak preference is the second, a separate append-only
table for events that are real and belong to no session, because it
leaves the event log's central invariant alone and because a
synthesized session makes the word mean two things.

**Reversal cost:** a migration if the wrong table is built first,
which is why it is worth answering before Milestone 3 rather than
during it.

### Three event names in live prose name nothing

**Decided:** replaced `session.opened` with `session.created`, and
`tool.invoked` and `tool.completed` with `tool.call.started` and
`tool.call.completed`.

**Why:** [skills.md](../plan/skills.md) listed `session.opened` and
`tool.invoked` under the sentence *"Three events carry skill
information, and none of them is new"*, which is the strongest
possible claim to be wrong about, and
[bootstrap-and-composition.md](../plan/bootstrap-and-composition.md)
told the CLI to render tool activity from `tool.invoked` and
`tool.completed`. None of the three is in Section 6.8 or anywhere
else.

**Note:** this is the third instance of one defect. The harness
asserted `tool.proposed`, `tool.authorized` and `tool.succeeded`,
which was corrected under the recorded precedent that Section 6.8's
names are canonical, and [runtime-loop.md](../plan/runtime-loop.md)
carries that as its thirteenth resolved conflict. Three documents
independently invented plausible short forms of the same four names,
which says the real names are longer than a writer reaching for them
expects.

**Question for you:** none. The substitutions are the nearest declared
event to what each sentence meant.

**Reversal cost:** none.

### The error taxonomy is twenty-nine classes, not thirty-one

**Decided:** corrected the count in
[runtime-loop.md](../plan/runtime-loop.md) and
[http-api-and-streaming.md](../plan/http-api-and-streaming.md), and
deleted the two error classes the second invented.

**Why:** the runtime loop says *"Eight more are raised by documents
written since and appear in no taxonomy"* over a block of eight, two
of which — `BudgetExceeded` and `ConflictError` — are in Section 13's
twenty-three already. They are unclassified there, which is a real
finding, but they are not new classes. Six are. The union is
twenty-nine.

**Note:** the interesting part is downstream. The API spec inherited
thirty-one, has a table of twenty-seven, and closed the gap by writing
*"and the two internal counterparts the loop resolves itself"* — two
classes with no names, in no list, that exist only to make a
subtraction work. Set arithmetic over the real union gives exactly two
absent, `WorkerFenced` and `EmptyModelTurn`, and the same sentence
already named both. A wrong total propagated one document and then
manufactured evidence for itself in the next.

**Question for you:** whether `BudgetExceeded` and `ConflictError`
should get retry classifications in Section 13 itself rather than in
the runtime loop's additions block. The classifications are stated and
correct where they are; the objection is only that Section 13 is where
a reader looks.

**Reversal cost:** none. Both counts now follow from lists a reader
can count.

### The control-tool table listed a span as a tool

**Decided:** removed `context.compact` from the control-tool table
in [tool-system.md](../plan/tool-system.md), put
`context.update_working_state` in its place, and corrected the
lead-in that said *"Three of the tool names"* over four rows.

**Why:** nothing in the corpus declares `context.compact` as a tool.
[runtime-loop.md](../plan/runtime-loop.md) uses it as a span nested
under the step span, the event compaction emits is
`context.compacted`, and
[context-engine.md](../plan/context-engine.md) says compaction is a
model call and therefore *"not something `build()` does"* — the loop
measures pressure and invokes the compactor itself. A tool that let
the model force one would hand it a lever over its own context
budget that nothing in that document contemplates. What the context
engine does put behind a control tool is
`context.update_working_state`, which it declares in full: input
schema, effect, and the event `context.working_state.updated`.

**Note:** the set is still four, so [skills.md](../plan/skills.md)
calling a hypothetical `skill.unload` *"a fifth entry"* and saying
*"that table has four entries"* are both still correct and both
untouched. The row was wrong, not the total.

**Question for you:** whether the model should ever be able to force
a compaction. This says no, because the context engine says the loop
decides and the model is the thing being budgeted. The case for yes
is a model that knows it is about to do something long and would
rather compact deliberately than be compacted mid-step. Recorded as
the tool system's seventh open question.

**Reversal cost:** none. It would be a fifth row and a `ToolSpec`.

### `skill_manage` was called a control tool in two more places

**Decided:** corrected both sentences in
[tool-system.md](../plan/tool-system.md) to match that document's
own registration table.

**Why:** the file names `skill_manage` among the tools that motivate
a `kind` field at all, and states flatly that *"`skill_manage` is a
control tool"* in the bullet explaining that control tools still
pass the full pipeline. Its own table classifies `skill.manage` as a
capability tool, and [skills.md](../plan/skills.md) argues at length
that it must be one, because it writes files and a control tool by
definition acts only on the run. The `kind` justification now names
`context.update_working_state` instead, and the pipeline bullet uses
`skill.load`, which is a control tool and carries the bullet's point
better: skills.md labels agent-authored skill content
`EXTERNAL_UNTRUSTED`, so exempting the category would exempt that
labelling from the step that applies it.

**Note:** an earlier pass corrected this document's registration
table and the skills spec. These two sentences survived it, which is
what a fix by search rather than by reading leaves behind.

**Question for you:** none beyond the standing one about spelling it
`skill.manage` in prose corpus-wide.

**Reversal cost:** none.

### The builtin roster is eight tools of eighteen

**Decided:** added a subsection to
[builtin-tools.md](../plan/builtin-tools.md) naming the ten
model-callable build-time tools that other specifications declare,
and scoped the classification table and the registration steps to
the eight the roster owns.

**Why:** the roster reads as the corpus's tool census and is not.
Ten more tools are model-callable at build time:
`conversation.ask_user` and `delegate.run` from
[tool-system.md](../plan/tool-system.md),
`context.update_working_state` from
[context-engine.md](../plan/context-engine.md), `skill.load` and
`skill.manage` from [skills.md](../plan/skills.md), three `memory.`
tools, and two `knowledge.` tools. The rule that makes the count of
eight correct was stated once, in
[knowledge-documents.md](../plan/knowledge-documents.md): subject
specifications declare their own tools, *"so this costs
`builtin-tools.md` nothing, and the roster's count is unchanged"*.
That sentence now lives where a reader of the roster will find it.

**Note:** the classification gap is recorded as a conflict rather
than closed. Registration step 6 reads `ToolSpec.side_effect` and
`output_trust`, and eight of the eighteen declare no `ToolSpec`
fields anywhere in the corpus. Step 3, domain membership, already
passes for all eighteen.

**Question for you:** whether the ten should be classified in
`builtin-tools.md` or in the documents that declare them. Recorded
as that document's ninth open question. One table is easier to check
against the registry; against that, a tool and its fields living in
different files is how the roster got read as the census in the
first place.

**Reversal cost:** low either way. The fields exist or they do not,
and where they are written is a move rather than a redesign.

### The agent-facing memory surface is three tools, not two

**Decided:** corrected [readiness.md](../plan/readiness.md), which
said the surface is two tools and that both of them read.

**Why:** `memory.remember` is a third and it writes.
`memory-retrieval-and-ranking.md` says two, correctly, because
within its own scope there are two: `memory.search` and
`memory.recall_episodes`. Formation declares the third. The
readiness note was reading a retrieval sentence as a corpus-wide
count.

**Note:** the argument the sentence supports is unaffected and is
kept as written. None of the three lists, edits, or deletes, and no
route or CLI command does either, so the `MemoryStore.list`, `edit`,
and `delete` gap stands exactly as stated.

**Question for you:** none.

**Reversal cost:** none.

### The scope table was a row short of the route table

**Decided:** added `| \`GET /v1/sessions/{id}\` | \`session.read\` |`
to the scope table in
[http-api-and-streaming.md](../plan/http-api-and-streaming.md), and a
paragraph naming what the scope gates, paralleling the one already
there for `skill.write`.

**Why:** the document specifies fourteen routes in `http` fences and
its table had thirteen rows. Set comparison gives exactly one
difference in one direction and none in the other: the missing row is
`GET /v1/sessions/{id}`, which is the one route this document adds
rather than one it inherited. The consequence was that `session.read`
sat in the closed scope vocabulary, and in the nine the API document
enumerates, with no route requiring it — a scope nothing could check.

**Note:** the document's own hard gate 5 walks the route table and
fails the build on any route but the two health probes that declares
no scope. The document therefore declared a gate its own table would
fail. `skill.write`'s absence from the same table is *not* this: it
has a paragraph saying it is checked by the policy engine on a tool
call rather than by the API on a route. `session.read` had no such
paragraph, which is what separates an omission from a seam.

**Question for you:** none.

**Reversal cost:** none.

### Five sentences closed the API at thirteen routes

**Decided:** corrected them to fourteen, and made the API document
state the resulting surface where a reader will find it.

**Why:** thirteen is the count of what the document *inherited* —
Section 16's nine, two approvals reads and one input route named
elsewhere, and the two health probes that Section 16 counts as one.
The document then adds `GET /v1/sessions/{id}`, which it argues for
at the route, records as resolution row 8, and decides as decision
20. Nothing stated the sum, so five downstream sentences quoted the
heading instead: [readiness.md](../plan/readiness.md),
[skills.md](../plan/skills.md),
[knowledge-documents.md](../plan/knowledge-documents.md) three times,
and [engineering-plan.md](../plan/engineering-plan.md) twice. The
engineering plan is the sharpest case: one line asks for an error
mapping for each of thirteen routes and the next sentence in the same
paragraph says one route is added.

**Note:** the overview also said the document *"adds nothing to the
API surface that Section 16 did not already put there"* and that
*"every route below is a route the corpus already names"*. Both
clauses are false, and the second is contradicted three times inside
the same document. The heading is now "Thirteen inherited routes",
the opening paragraph carries the sum, and decision 21 states it.

**Question for you:** none.

**Reversal cost:** none.

### The next CLI noun was numbered fourteenth

**Decided:** corrected
[knowledge-documents.md](../plan/knowledge-documents.md) to say a
thirteenth CLI noun.

**Why:** the CLI is twelve commands.
[bootstrap-and-composition.md](../plan/bootstrap-and-composition.md)
owns that census, prints the twelve in a table, and names the cost of
adding one as *"a thirteenth top-level noun"*. The sentence appears to
have taken its ordinal from the route count in the clause before it.

**Note:** the CLI census is otherwise sound. The twelve commands, the
four reserved words after `agent run`, and the rule that a subcommand
under an existing command is not a new command all agree across
`bootstrap-and-composition.md`, `evaluation-harness.md`, and
`event-log-and-persistence.md`.

**Question for you:** none.

**Reversal cost:** none.

### The run body stated two column counts and both were wrong

**Decided:** corrected both in
[http-api-and-streaming.md](../plan/http-api-and-streaming.md), and
stated the split the body actually makes.

**Why:** the paragraph introducing `GET /v1/runs/{run_id}` said *"The
`runs` table has fourteen columns and this returns nine of them"*.
Section 15 declares fifteen columns, four other documents add eleven
more, and the JSON body immediately below has thirteen top-level
keys. Neither number was derivable from anything in the corpus. The
overview of the same document says *"the run record's twenty-three
columns"*, so the document also disagreed with itself; twenty-three
is exactly Section 13's error-class count, which this document
states correctly seventy-six lines later.

**Note:** the arithmetic that does close is thirteen returned and
thirteen withheld, and the split is the seam between Section 15 and
everything after it. Every Section 15 column is in the body except
`lease_owner` and `lease_expires_at`, and every column another
document adds is withheld — though `deadline_at` still reaches the
client inside `limits`, where it was a domain field before
`runtime-loop.md` gave it a column. The sessions section of the same
document is the control case: it states no count, is right, and
explains its one interesting omission.

**Question for you:** none.

**Reversal cost:** none.

### The idempotency resolution was summarized as two tables

**Decided:** corrected [readiness.md](../plan/readiness.md) to say a
table and a column.

**Why:** it said the API document resolved `Idempotency-Key` against
the tool idempotency port as *"two scopes, two tables, two
milestones, one unfortunate name"*. The API document resolves it as
one table and one column: `idempotency_keys` for the HTTP header and
`tool_invocations.idempotency_key` for the tool call. Its own words
are *"They share a name, a column name, and nothing else"*, and its
resolution row says *"two mechanisms, two scopes"* — not two tables.

**Note:** the rest of the sentence is right. Two scopes and two
milestones are both correct, and so is the verdict that the name is
unfortunate.

**Question for you:** none.

**Reversal cost:** none.

### Two documents add the same column with one cross-reference

**Decided:** added the missing cross-reference to
[tool-system.md](../plan/tool-system.md).

**Why:** `policy-and-approvals.md` and `tool-system.md` both add
`origin_trust` and `idempotency_class` to `tool_invocations`, both
`NOT NULL`, and both with a rationale paragraph. `origin_trust` had
a sentence naming the other document; `idempotency_class`, whose
rationale sits in the paragraph immediately below, did not. A reader
of either document alone would not know the column was declared
twice, and a migration author reading both would not know whether
that was intentional.

**Note:** the two declarations agree, so this is a documentation
defect rather than a design conflict. The columns are denormalized
onto the authorization record on purpose, which both documents say
in their own words.

**Question for you:** none.

**Reversal cost:** none.

### Milestone 9 and 10 persistence has no schema

**Decided:** left as is, and recorded.

**Why:** the corpus declares twenty-two tables as DDL across seven
documents, and every document that adds storage before Milestone 9
declares it in a fenced schema block.
`memory-formation-and-consolidation.md` and
[knowledge-documents.md](../plan/knowledge-documents.md) declare
their persistence as Pydantic models instead, and
`multi-device-and-surfaces.md` says the `Device` *"gets a table
rather than a column"* without giving the table any columns. So the
last two milestones are the only ones whose storage an implementer
cannot write a migration from.

**Note:** this may well be deliberate. The retrieval design those
documents store for is the newest work in the corpus, and Section
29.8 defers the device schema explicitly. Inventing the columns
would be exactly the kind of material addition this assignment is
not allowed to make, which is why it is asked rather than decided.

**Question for you:** should the memory, knowledge, and device
tables get DDL before coding starts, or is a Pydantic model enough
to build Milestone 9 from?

**Reversal cost:** low. Adding a schema block to three documents
touches nothing else, and none of the four gates that reference
those documents reads a column name.

### The schema is twenty-two tables and nothing says so

**Decided:** left uncounted, and recorded.

**Why:** the tables are declared across seven documents and no
document enumerates them. The only statement of the size is
[event-log-and-persistence.md](../plan/event-log-and-persistence.md)
calling it *"roughly twenty tables, two functions each"*, which is
defensible for twenty-two and is hedged on purpose. Adding a census
would mean choosing an owner for it, and the natural owner is
Section 15, which this assignment does not edit.

**Note:** four of the twenty-two are effectively write-only in the
corpus. `eval_scenario_runs` and `eval_criterion_scores` are
mentioned twice each in all of `docs/`, and `mcp_tool_catalog` three
times, which is the declaration and little else. That is a
different problem from the count and is not fixed by fixing the
count.

**Question for you:** should one document own a table census, the
way `bootstrap-and-composition.md` owns the CLI census and
`builtin-tools.md` now owns the tool census?

**Reversal cost:** low.

### The runtime port count came from the fences, not the ports

**Decided:** corrected [runtime-loop.md](../plan/runtime-loop.md) to
five, and named the sixth.

**Why:** the section headed *"Ports declared here"* opened *"Four
ports the runtime needs are named in the corpus and declared
nowhere"* and then declared five before the next heading: `Clock`,
`IdFactory`, `AgentRepository`, `PrincipalResolver`, and
`BudgetLedger`. Four is the number of code fences, not the number of
ports, because `Clock` and `IdFactory` share one. The same document
declares `CancellationToken` further down under cancellation, so it
declares six ports in total and five under that heading.

**Note:** the neighbouring count is right and was left alone. Line
29's *"Section 7 declares eight port Protocols with full
signatures"* checks against Section 7 exactly, and so does
`multi-device-and-surfaces.md` putting the device registry *"beside
the seven repositories already there"*.

**Question for you:** none.

**Reversal cost:** none.

### Eight ports had no module and a gate walks the directory

**Decided:** assigned all eight in
[bootstrap-and-composition.md](../plan/bootstrap-and-composition.md),
which meant adding `ports/knowledge.py` and `ports/credentials.py`.

**Why:** that document's *"Where the ports live"* table exists
because *"without an assignment rule, the first implementer invents
one, and the second invents a different one"*, and it left eight
declared Protocols out of the table entirely: `SkillRepository`,
`SkillPackageStore`, `Extractor`, `Chunker`, `KnowledgeStore`,
`WorkspaceHandle`, `ArtifactWriter`, and `CredentialResolver`. That
is not a cosmetic gap.
[evaluation-harness.md](../plan/evaluation-harness.md) declares a
structural gate that walks `agent_core/ports/`, collects the
Protocols it finds, and fails the build for any without a contract
module, so where these eight live decides which contract suites
exist.

**Note:** six of the eight are the document's own rule applied
without judgement. `SkillRepository` goes with the repositories for
the reason `multi-device-and-surfaces.md` puts `DeviceRegistry`
there; `SkillPackageStore` and `ArtifactWriter` take bytes and hand
them back under a key, which is what `artifacts.py` is named for;
`WorkspaceHandle` is what `ExecutionEnvironment` returns. The table
now names thirty-nine ports across fourteen modules, and every
`Protocol` in the corpus that is not one of the four application
services of the API document has a home.

**Question for you:** the two new module names are the part that is
a choice rather than a derivation. `ports/knowledge.py` and
`ports/credentials.py` are what I used. The alternative for the
second was folding `CredentialResolver` into `policies.py`, which I
rejected because policy decides whether a tool may run and the
resolver hands it the secret afterwards, and into `execution.py`,
which I rejected because the sandbox spec's one structural claim
about it is that a sandboxed tool gets a resolver that raises.

**Reversal cost:** cheap now, moderate later. Renaming a module in
prose is a search and replace; renaming it after the contract suites
are written moves files and import lines in the gate that reads
them.

### Three rows in the ports table still name no type

**Decided:** left as prose rows, and recorded here.

**Why:** `ports/telemetry.py` is in the Section 5 tree and no
document in the corpus declares a telemetry Protocol.
`memory-formation-and-consolidation.md` declares `MemoryStore` and
`MemoryConsolidator` as prose bullets with method lists rather than
`Protocol` blocks, while
[memory-retrieval-and-ranking.md](../plan/memory-retrieval-and-ranking.md)
beside it declares five with full signatures. And no MCP port is
declared anywhere, though `adapters.mcp`'s import row says it
depends on `ports` and `domain`, which may mean it implements `Tool`
and `ToolRegistry` and needs no port of its own. Writing the three
would be inventing interfaces, which this assignment does not do.

**Note:** the same harness gate is what makes this load-bearing
rather than tidy. A port that exists as a sentence has neither a
Protocol for the walk to find nor a contract module to demand, so
the gate passes by not looking, which is the failure mode a
structural gate is supposed to remove. The memory half is also the
same asymmetry the schema audit found: the two newest specifications
in the corpus are the two that describe their interfaces in prose.

**Question for you:** should the three be declared before Milestone
0 opens, or is the walk meant to find only what exists?

**Reversal cost:** low.

### The type census found one silent re-declaration in thirteen

**Decided:** added the missing supersession paragraph under
`ProviderReasoningItem` in
[model-gateway.md](../plan/model-gateway.md), and left the other
twelve duplicated declarations alone.

**Why:** the corpus declares one hundred and forty four distinct
types across one hundred and fifty nine declarations, so thirteen
types are declared more than once. Four of the thirteen have
identical member sets and cannot mislead anyone. Of the nine that
differ, eight already say that they differ: `Run`, `ToolSpec` and
`MemoryRecord` carry an "existing fields, unchanged" comment inside
the fence, `ContextBudget`, `RunCheckpoint` and `ModelRequest` are
introduced by a sentence naming what they extend, `ModelProvider`
has a supersession paragraph, and `ModelCapabilities` has a whole
section reconciling it row by row. `ProviderReasoningItem` had
nothing. The plan declares three fields, the gateway declares six,
`opaque_payload` becomes `provider_payload`, and no sentence in the
corpus says so.

**Note:** the rename is not cosmetic, because the field is
persisted. The plan's rules paragraph — store verbatim, never
summarize, drop on a provider switch — governs a field name that no
longer exists, and `RunCheckpoint.conversation` carries the item
into the checkpoint, so an implementer working from the plan and an
implementer working from the gateway write two different keys into
the same stored JSON. The string `opaque_payload` occurs exactly
once in the whole corpus, which is what made it invisible. The same
document solves this problem correctly twice in the same file, and
engages with the plan's block directly at the line above the
rename, which is why I read the third as an oversight and not a
style.

**Question for you:** the paragraph itself invents nothing and needs
no answer. The rename does, and it is now question nine in that
document's own list, beside question six, which asks the same thing
about `tool_calling` and `vision`. One answer covers both.

**Reversal cost:** none.

### The ModelCapabilities reconciliation is not where the fence is

**Decided:** added a pointer sentence under the declaration.

**Why:** [model-gateway.md](../plan/model-gateway.md) reconciles its
`ModelCapabilities` against the plan's field by field, with a table,
a stated reason for every changed row, a precedence rule, and an
open question. That work sits three hundred and fifty six lines
below the fence that needs it. A reader who arrives at the
declaration, counts ten fields where the plan has eight, and does
not read on has no signal that the divergence was considered at
all.

**Note:** this is the general shape of what the type census found.
Every reconciliation in the corpus is sound where it exists. What
varies is whether the reader is standing where it is.

**Question for you:** none.

**Reversal cost:** none.

### StopReason.STOP is not a member of StopReason

**Decided:** changed it to `StopReason.END_TURN`.

**Why:** [runtime-loop.md](../plan/runtime-loop.md) names the empty
terminal turn by a stop reason that does not exist.
[model-gateway.md](../plan/model-gateway.md) declares seven members
and `STOP` is not among them. Of sixty five dotted enum member
references in the corpus this was the only dangling one, and the two
paragraphs immediately after it use `MAX_TOKENS` and `CANCELLED`
correctly, so the wrong one reads as authoritative.

**Note:** `END_TURN` rather than `INCOMPLETE` because the case being
described is a turn the provider says finished normally while carrying
neither text nor tool calls. `INCOMPLETE` is declared for a provider
that ended without finishing, which is a different anomaly, and the
gateway's `ScriptedTurn` already defaults its stop reason to
`END_TURN`. The paragraph is load-bearing: it is the trigger for the
empty-turn retry path and for `EmptyModelTurn` and
`FailureReason.EMPTY_MODEL_TURN`.

**Question for you:** none. Nothing else in the corpus reads `STOP` as
a value.

**Reversal cost:** one line.


### SandboxMechanism had three values in the file that defines it

**Decided:** added `fake` to the annotation and to startup check 4.

**Why:** the enum is never declared anywhere, so the single site that
enumerates its values is authoritative by default, and that site is
[bootstrap-and-composition.md](../plan/bootstrap-and-composition.md)'s
`Settings` comment, which listed three. Six other places say four:
sandbox-isolation.md's mechanism table and its seventeenth
requirement, ADR-0029 item thirteen, engineering-plan.md, and this
file. The comment is what an implementer codes `config.py` from, and
`sandbox: fake` would have failed to parse.

**Note:** the second half matters more than the first. The seventeenth
requirement says `fake` is refused in production by the same check
that refuses `docker`, and startup check 4 asserted only `sandbox !=
"docker"`. `fake` executes nothing and models the workspace as a
dictionary, so a production deployment configured with it would have
started, reported tool calls as run, and isolated less than the
fallback the check was written to refuse. The setting and its refusal
are both Milestone 1.

**Question for you:** none. Four sources agree and none of them is
ambiguous.

**Reversal cost:** one comment and one clause.


### The trajectory export used the tool vocabulary for a run outcome

**Decided:** changed the exported `outcome` from `SUCCEEDED` to
`COMPLETED`.

**Why:** the run-level terminal vocabulary is `COMPLETED`, `FAILED`,
`CANCELLED`, in `RunStatus`, in `OutcomeKind` lowercased, and in the
three places the corpus writes the terminal subset as a SQL tuple.
`SUCCEEDED` is the tool-invocation success spelling and exists nowhere
at the run level. The arity was already right; only the success word
was wrong.

**Note:** this one is persisted and externally consumed. The export
carries `schema_version: 1` and ADR-0016 and ADR-0032 put it in front
of readers who are not us, so a consumer filtering on the vocabulary
the rest of the system uses would have matched nothing and reported no
successful runs. The word `outcome` occurs on two lines in the whole
corpus, which is what made it invisible. A sentence under the fence
now ties the field to `RunStatus` so the next reader does not have to
rediscover which of the two vocabularies it belongs to.

**Question for you:** none, unless you want the export to keep a
vocabulary of its own, in which case the fix is to say so rather than
to leave the divergence unremarked.

**Reversal cost:** one word and one sentence.


### SuspensionKind is never declared and appears in two spellings

**Decided:** recorded, not edited.

**Why:** the type is annotated in `Suspension` and its members appear
only in trailing comments: `APPROVAL | USER | CHILD_RUN` in
runtime-loop.md and `user_input | child_run` on the `suspended_kind`
column in tool-system.md. No fence declares it. Unlike the three
above, this one is already visible:
[multi-device-and-surfaces.md](../plan/multi-device-and-surfaces.md)
names the divergence while proposing a hand-off as a fourth kind, and
readiness.md lists it among the identified gaps.

**Note:** declaring it now would settle the fourth-kind question by
writing the enum, and that question is yours. The two spellings are
the same three members, so nothing is ambiguous today except the
casing convention, which the corpus otherwise resolves by uppercasing
the stored status and lowercasing the domain value. Whoever answers
the hand-off question should declare the enum in the same pass.

**Question for you:** the hand-off question you already have. This
adds only that answering it should produce a declaration.

**Reversal cost:** none. Nothing was changed.
