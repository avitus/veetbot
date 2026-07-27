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
