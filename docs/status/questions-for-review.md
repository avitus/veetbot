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
