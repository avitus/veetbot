---
title: Readiness
status: design
canonical: true
---

# Readiness

This document answers one question: can an implementer open this
corpus and start writing code, and if so, how far can they get before
the corpus runs out?

The answer is that Milestones 0 through 5 are implementable from the
documents alone and Milestones 6 through 10 are not implementable
without design work that has not been done. The boundary is sharp and
it falls in a useful place, because Section 21 forbids working on
more than one milestone at a time and six milestones of runway is
more than any first implementation will consume before the missing
documents can be written.

Milestone 5 crossed that boundary after this review was written, and
it crossed because of it. The finding was that the API had a plan
section and no specification below it, and the finding is kept below
rather than deleted, because it is the evidence for why
[http-api-and-streaming.md](http-api-and-streaming.md) was written and
the record of what writing it turned up.

This document owns no requirement. It states what is covered and what
is not, and where it finds a conflict between two documents it names
the conflict rather than deciding it. The two exceptions are recorded
under [Decisions](#decisions), and both are placements rather than
requirements.

## What was checked, and how

Every `#### Implement` bullet, every acceptance criterion, and every
subsection heading under Milestones 0 through 10 in
[engineering-plan.md](engineering-plan.md) was taken as a work item
and traced to the document that designs it. An item counts as
**covered** when some specification states its mechanism in enough
detail that two implementers would build the same thing. It counts as
**partial** when the seam exists — a type is named, a column is
declared, a check site is identified — but the algorithm, the schema,
or the signature behind it does not. It counts as **absent** when no
document outside the plan expands it at all.

Three things are deliberately not treated as evidence of absence.

1.  **A plan section is a design.** Section 16 specifies nine HTTP
    endpoints with methods, paths, headers, and request and response
    bodies. That it lives in the plan rather than in a spec changes
    what a reader must open, not whether the thing is designed. The
    distinction still matters and is reported, because every other
    milestone's mechanisms were expanded once more after the plan
    stated them, and the expansions found real conflicts each time.
2.  **An explicit deferral is not a gap.** `builtin-tools.md` has a
    section titled *"The six tools this document does not design"*
    that names each one, assigns it a milestone, and lists what it
    still owes. That is a smaller and better-understood hole than an
    item nobody has looked at, and it is scored separately.
3.  **A reserved seam is not an omission** where the document says so.
    `tool-system.md:1665` states that device tools are *"a reserved
    seam, not a design"*. The seam is the decision.

The gate census in [milestone-map.md](milestone-map.md) was used as an
independent check on every verdict below. It was derived mechanically
from the specifications, the plan, and the map, without reference to
this review, and where the two disagree the disagreement is reported
rather than smoothed.

## The verdict

| M | Subject | Verdict | Gates | What stands between it and code |
| --- | --- | --- | --- | --- |
| 0 | Repository and engineering foundation | Ready | 11 | Nothing |
| 1 | In-memory vertical slice | Ready | 27 | Nothing |
| 2 | PostgreSQL persistence and durable worker | Ready | 12 | Migration authoring conventions |
| 3 | Model adapters and normalized streaming | Ready with named gaps | 11 | Provider response metadata |
| 4 | Policy, approvals, and tool lifecycle | Ready with named gaps | 12 | Four deferred tools; scope vocabulary |
| 5 | HTTP API and SSE | Ready | 11 | Nothing |
| 6 | Isolated execution and artifacts | Not ready | 0 | No sandbox specification exists |
| 7 | Context budgeting and working state | Ready with named gaps | 6 | No evaluation cases |
| 8 | Skills and MCP integration | Split | 0 | MCP ready; skills undesigned |
| 9 | Long-term memory and knowledge | Ready with named gaps | 14 | Knowledge documents |
| 10 | Scheduling, routing, and subagents | Not a milestone yet | 0 | No acceptance criteria exist |

The gate column is the count of registry entries whose `milestone`
field names that milestone. Its correlation with the verdict column is
the strongest single piece of corroboration in this review, and it was
not constructed to produce that correlation. When this review was
written Milestone 5 registered one gate and Milestone 6 registered
none, which is what a corpus looks like when nobody has written down
what those milestones must be true of. Milestone 5's eleven are the
ten the API specification declares plus the one it already had, and
they arrived with that document rather than being added to it
afterwards, which is the same correlation running the other way.
Milestone 6 still registers none.

## Milestone 0: ready

Eleven registry entries, every deliverable expanded.

[development-toolchain.md](development-toolchain.md) and ADR-0025
specify the Makefile target bodies, the compose service, the CI
workflow, and why `docker compose up -d postgres` and `alembic upgrade
head` remain two commands.
[bootstrap-and-composition.md](bootstrap-and-composition.md) and
ADR-0024 specify the secret scanner — five rule families, a report
that never prints what it matched, an allowlist whose entries require
prose — and add four static checks to the import-boundary walk, all
four of which are true of an empty repository.
[evaluation-harness.md](evaluation-harness.md) supplies the four
harness gates. The map supplies six of its own, all of them statements
about documents rather than about a running system.

Nothing here is blocked. This is the milestone the corpus has been
examined against most often, and it is the one with the least left to
decide.

## Milestone 1: ready

Twenty-seven registry entries, the largest count of any milestone, and
every implement bullet has a body.

The three bullets ADR-0024 found empty — in-memory repositories, the
inline run dispatcher, the minimal context builder — now have one.
The in-memory tier is five real adapters running the same contract
suites as their PostgreSQL counterparts, with a checked-in capability
table so a skip is a reviewed fact. `RunDispatcher` has one method and
one postcondition that both adapters satisfy. The context builder is
build-sequence step 1, and its test asserts both halves rather than
the half that a builder which never changes anything would also pass.

Two things about this milestone are worth stating because they are
easy to misread as problems.

**Thirty-eight of one hundred and four gates are green before
Milestone 2 begins**, eleven of them against a repository with no
agent in it.
That is not a sign that the gates are weak. It is the consequence of
building the in-memory tier as real adapters rather than as test
doubles: the invariants that hold for the slice hold for the durable
system, and they can be asserted four milestones before the durable
system exists.

**Cancellation arrives here rather than at Milestone 5.** Section 21
lists cancellation under Milestone 5 and
[runtime-loop.md](runtime-loop.md) proposes splitting it. The map
resolves the split: a lazily evaluated deadline and a `SIGINT` handler
are Milestone 1 because both need only `Clock` and the process, and
`CancelReason` divides by dependency across Milestones 1, 2, and 5.
This is the one placement in the whole reconciliation that overrides a
milestone the plan states rather than one it leaves silent, and it is
recorded as an open question in
[questions-for-review.md](../status/questions-for-review.md).

## Milestone 2: ready

Twelve registry entries. Nine of eleven implement bullets are fully
covered by [event-log-and-persistence.md](event-log-and-persistence.md)
and [runtime-loop.md](runtime-loop.md): the append-only event store,
the projections and their rebuild, `FOR UPDATE SKIP LOCKED` claim,
`lease_epoch` fencing, checkpoint and resume, the reaper, and the
transaction-hygiene gate ADR-0024 separated from its Milestone 0
check.

Two bullets are partial, and both are tooling rather than
architecture.

1.  **The SQLAlchemy adapter has no ORM surface.** The schema is
    fully specified, table by table, but no document states whether
    mappings are declarative or imperative, where the session factory's
    boundary sits relative to the unit of work, or what the repository
    method bodies look like against it. ADR-0024 fixes the one
    property that matters — a factory is constructed, never a session,
    and no `AsyncSession` exists at module scope — and leaves the rest
    to whoever writes it.
2.  **Alembic has no authoring conventions.**
    `engineering-plan.md:1619` says *"Create Alembic migrations for at
    least these tables"* and `development-toolchain.md:183` supplies
    the `make migrate` target. Between them there is no statement of
    naming, no branch policy, no rule for data migrations versus
    schema migrations, and no design for how the composition root
    asserts the revision it refuses to start without — which ADR-0024
    decision 6 requires and does not specify.

Neither blocks the milestone. Both are the kind of decision an
implementer makes correctly on the first attempt and expensively on
the third, which is the argument for writing them down before rather
than after.

One milestone conflict is reported and not resolved here. The plan
places *"Usage token classes and cost-source precedence in the schema
(Section 6.5)"* in Milestone 2 at `engineering-plan.md:2444`, while
[model-gateway.md](model-gateway.md) designs it and sequences it to
Milestone 3, and the map follows the gateway. The schema column can
exist a milestone before anything writes to it, so this is a question
of whether the migration ships early or the whole item ships late; it
does not change what gets built.

## Milestone 3: ready with named gaps

Eleven registry entries. [model-gateway.md](model-gateway.md) is one
of the more complete specifications in the corpus: both provider
adapters, the normalized streaming event set with the exact field
mappings pinned for Anthropic Messages and OpenAI Responses, retry
classification, the reasoning-state handling ADR-0007 requires, and
the usage and cost model.

Three items fall short, one of them completely.

1.  **Provider response metadata is designed nowhere.**
    `provider_metadata` appears exactly once in the entire corpus, as
    a `dict[str, Any]` field at `engineering-plan.md:1205`. No document
    states which keys go in it, whether the set is open or closed,
    whether it is persisted, or where. It is absent from the
    `model_calls` schema. A field of that shape with no key discipline
    is where provider-specific data accumulates until something
    depends on it, which is the failure mode ADR-0002's
    provider-neutrality argument exists to prevent.
2.  **Declarative provider plugins have no document schema.** Section
    10.7 and ADR-0012 establish that a new OpenAI-compatible provider
    should be addable without code, and the gateway specifies how a
    profile is consumed. What no document states is the profile
    document's own schema — its fields, its required set, its
    validation, and what happens when a profile names a capability the
    adapter cannot satisfy.
3.  **Minimal redacted trajectory export has no design.** See
    [Section 31](#31-trajectory-capture-and-export) below; this is the
    consumption half of a section whose production half is fully
    specified.

## Milestone 4: ready with named gaps

Twelve registry entries.
[policy-and-approvals.md](policy-and-approvals.md) covers the
deterministic decision function, the hardline set, profile compilation
and freezing, `policy_version`, the approval record and its lifecycle,
expiry, resume revalidation, and the injection corpus.
[tool-system.md](tool-system.md) covers the execution pipeline
end to end.

Two things are outstanding.

**Four builtin tools are classified but not designed**, and the
document says so. `builtin-tools.md` names `workspace.read_text`,
`workspace.write_text`, `workspace.list_files`, and
`demo.external_write`, assigns each to Milestone 4, and lists what
each still owes: path resolution and traversal rejection, encoding and
binary-file rules, the checksum algorithm, the listing's limit and
ordering, and the `WorkspaceHandle` the execution context already
carries. It also fixes two constraints in advance rather than leaving
them to be noticed — the reader lowers `output_trust` to
`EXTERNAL_UNTRUSTED` for any file whose provenance in the run is not
established, and the establishing set is what this run's
`workspace.write_text` produced.

This is a bounded and well-understood hole, but one consequence should
be stated plainly: the Milestone 4 acceptance criterion *"Path
traversal is rejected"* and eval case 19 both stand on an algorithm
that no document contains. A rejection rule is exactly the kind of
thing that is written three different ways by three implementers, two
of which are subtly wrong.

**Principal scopes are half-designed.** The `Principal` model lives
only at `engineering-plan.md:459`, the policy spec identifies where
scopes are checked, and nothing states the scope vocabulary, its
grammar, or the comparison algorithm — whether a scope is an opaque
string, a hierarchy, or a pattern. Relatedly,
`bootstrap-and-composition.md:450` names `ApprovalService` as one of
the services `build` returns, and no document gives it a method
signature.

## Milestone 5: ready

Eleven registry entries. There was one when this review was written —
the fewest of any milestone that adds work — and that number was the
finding.

Section 16 of the engineering plan, at lines 1791 through 1943,
designs the API more thoroughly than a summary of this milestone's
coverage would suggest. It specifies nine endpoints with methods,
paths, and where relevant headers; request and response JSON for
session creation, message submission, and approval resolution; the SSE
frame format with `id`, `event`, and `data` lines; the reconnect rule
that replays persisted events after `Last-Event-ID` and then continues
streaming; the five cooperative cancellation observation points; the
error envelope; and the readiness constraint that a probe must not
call a provider.

What did not exist was any expansion of that section. No
detailed-design specification covered the API layer. The only HTTP
routes designed outside the plan were three in
`policy-and-approvals.md` — the two approvals reads at lines 819 and
820 and the resolve at line 829 — and one reference in
`runtime-loop.md:1165` to `POST /runs/{id}/input` that routed to an
endpoint it did not design.

That matters more than it would for a milestone whose plan section was
merely a summary, because every comparable section in this corpus was
expanded exactly once and every expansion found conflicts the plan had
not noticed. ADR-0024 found two outright milestone contradictions in
the bootstrap path. The milestone map found three gates declared
twice and a required field absent from most of the corpus. There is no
reason to believe Section 16 was the one section that would survive
expansion unchanged, and several specific reasons to believe it would
not. It did not: the expansion found nine contradictions and recorded
each of them in a table of its own.

Six things were visibly unsettled inside it.

1.  **The error envelope has one worked example and no code list.**
    A stable error contract is the part of an API that clients depend
    on hardest and that is most expensive to change after release.
2.  **Request IDs are an implement bullet and nothing else.** No
    document states where the identifier comes from, whether a client
    may supply one, how it reaches the event log, or its relationship
    to the trace identifier the observability section requires.
3.  **`Idempotency-Key` handling is named in two places and specified
    in neither.** It appears as a header at
    `engineering-plan.md:1830` and as an implement bullet, and the
    idempotency port the map schedules at Milestone 1 is a tool-call
    concern rather than an HTTP one. Whether these are the same
    mechanism is undecided.
4.  **The SSE consumer side is one sentence.** ADR-0010 chose
    PostgreSQL `LISTEN`/`NOTIFY`, and
    [event-log-and-persistence.md](event-log-and-persistence.md)
    designs the producer side completely. How a connected client's
    stream is fed from that channel, what happens when a notification
    is missed, and how replay hands off to live streaming without a
    gap or a duplicate are the substance of eval case 22, and they are
    forwarded to Section 16 rather than designed.
5.  **API authentication is designed only at its refusal.** ADR-0024
    fixes that production startup fails without configured
    authentication and puts the mode and token in `Settings`. Token
    format, validation, the mapping from a credential to a
    `Principal`, and rotation are all undecided.
6.  **The cancel endpoint is distinct from the cancellation
    mechanism.** `runtime-loop.md` specifies the token, the six
    observation points, and the `effect_sent_at` rule completely. What
    turns an HTTP `POST` into an observation by a worker in another
    process is not specified. The worker half is — the poller, its
    cadence, and its query are all in `runtime-loop.md`. It is the API
    half that is missing, and it is one column write.

The verdict on that evidence was that coding could reach this
milestone and should not enter it until an API specification existed,
and that this was the single most valuable document not yet written.
It exists now.
[http-api-and-streaming.md](http-api-and-streaming.md) and ADR-0028
settle all six. The code list is the error taxonomy already in the
corpus, snake-cased, rather than a second vocabulary that would have
to be kept in step with the first. Request identifiers get four rules,
of which the load-bearing one is that an identifier a client supplies
is never trusted with anything. The two idempotency keys are separated
as two mechanisms that share an unfortunate name, with different
scopes, tables, and milestones. The consumer side of the stream is
specified down to the subscribe-before-read handoff that makes replay
gapless and duplicate-free. Authentication is specified at what it
produces and not only at what it refuses. And the cancel path gets the
one sentence it was missing.

The same document supplies response bodies for the twelve routes that
had none, and declares ten hard gates, which is what took this
milestone's gate column from one to eleven. The verdict is ready.

## Milestone 6: not ready

Zero registry entries, and eight of twelve implement bullets have no
design outside the plan: the container-backed execution adapter,
workspace lifecycle, resource limits, no-network execution,
`sandbox.run_command`, the filesystem artifact store, artifact
metadata and content endpoints, and workspace cleanup.

Section 28 of the plan is not empty — it runs from line 3136 to line
3212, states a six-item threat model that assumes model-generated code
is hostile, and is recorded as ADR-0008. But it was never expanded,
and two specifications point at the expansion as though it exists.
`tool-system.md:977` constrains MCP server URLs by *"the egress
allowlist the sandbox spec establishes"*. There is no sandbox spec.
`bootstrap-and-composition.md:180` and `:183` assign ownership of
`ArtifactStore` and `ExecutionEnvironment` to the engineering plan
itself, which is the corpus recording that nothing below the plan owns
them.

Two bullets are covered. Output truncation and artifactization are
specified at `tool-system.md:708`, and the programmatic orchestration
bridge Section 8.5 requires is specified from `tool-system.md:1161`.

Two further items deserve naming.

1.  **The plan demands a red-team test with no case behind it.**
    `engineering-plan.md:3209` requires a container-escape attempt as
    a security test. The twenty-five-case table contains no such case
    and no Milestone 6 security row.
2.  **`sandbox.run_command` is placed at two milestones.**
    `builtin-tools.md:909` says Milestone 5; the plan's Milestone 6
    implement list contains it. The map follows the plan. This is
    reported rather than resolved, because the right answer depends on
    the sandbox specification that does not exist: if the tool can
    ship against the development mechanism at Milestone 5 and gain
    container backing at Milestone 6, both documents are right about
    different things.

The zero in the gate column is worth dwelling on. Milestone 6 is the
milestone whose failure mode is a container escape, and it is one of
two milestones that register no gate at all. Every invariant its work
strengthens is registered against an earlier milestone, which means
the gate registry currently contains no statement that becomes true
because the sandbox was built. That is recorded as an open question in
the map and is repeated here because the sandbox is where it matters
most.

## Milestone 7: ready with named gaps

Six registry entries and the best per-bullet coverage of any milestone
in the second half. [context-engine.md](context-engine.md) covers
seven of eight implement bullets fully: the token budgeter, the
compaction boundary, the structured working set, the deterministic
assembly and `prefix_sha256`, the two regions, trust labeling in
assembled context, and the recall trace as a second consumer.

Two shortfalls.

1.  **History selection has an order but no predicate.** The yield
    order and the floor are specified. What decides that a given turn
    is in or out is not, which is the part that determines whether the
    result is stable across two runs with the same input.
2.  **Long-session evaluation has a gate and no case.** The
    twenty-five-case table carries milestones 1, 2, 4, 5, and 6 and
    nothing else. There is no Milestone 7 row, so three of this
    milestone's acceptance criteria have no case backing them. The
    harness specifies how to add cases and the gate exists; what does
    not exist is the case.

## Milestone 8: split

Zero registry entries, and the two halves of this milestone are in
completely different states.

**MCP is substantively covered.** [tool-system.md](tool-system.md)
designs nine of eleven bullets: server configuration and lifecycle,
tool discovery and namespacing, the trust labeling that makes MCP
results `EXTERNAL_UNTRUSTED`, failure isolation, timeouts, and the
reserved-domain collision rules. Two are partial: authentication
configuration has a `credential_ref` column and no auth scheme and no
refresh or re-auth path, and the mock MCP server the acceptance
criteria require is never designed, with no MCP fixture format in the
harness and no Milestone 8 row in the case table.

**Skills have no specification at all.** No document outside the plan
and ADR-0013 mentions `SKILL.md`. There is no package format, no
manifest schema, no selection algorithm, no loading or sandboxing
rule, and no versioning scheme. The acceptance criterion *"A selected
skill is version-pinned in the run"* at `engineering-plan.md:2684` has
no design anywhere in the corpus — not a partial one, none. The
`#### Skills` subsection is also one of the few major subsections with
no cross-reference paragraph pointing outward, which is consistent:
there is nothing to point at.

Section 30, self-improving skills, compounds this. It is referenced
from eleven places in the corpus as though the mechanism it describes
were settled.

## Milestone 9: ready with named gaps

Fourteen registry entries, the second-largest count, and the two
memory specifications are among the most complete documents here.
[memory-formation-and-consolidation.md](memory-formation-and-consolidation.md)
covers the autonomous formation loop, the tiers, the provisional tier
and its promotion rules, contradiction handling, and cross-project
belief carry-forward.
[memory-retrieval-and-ranking.md](memory-retrieval-and-ranking.md)
covers retrieval, ranking, the snapshot budget, and the recall trace
with its two consumers.

One item is absent and several are partial.

**Knowledge documents have no design.** The milestone's own heading is
*"Long-term memory and knowledge retrieval"*, the separate-stores
subsection distinguishes memory from knowledge, and no document states
what a knowledge document is, how one is ingested, chunked, indexed,
or scoped, or how retrieval over it differs from retrieval over
memory. The memory specs are explicit that they cover memory; nothing
covers the other half of the milestone's name.

The partials are: session history and artifacts as retrieval sources
(named as sources, not designed as such); expiration (a policy with no
sweep job); the human-editable surface (no UI contract, no edit
semantics, no statement of what happens to a belief a human edits);
the external memory provider port (named, no contract); and the
persona and identity surface over `AgentSpec.instructions` (no
statement of how a formed belief reaches the instruction text, or
whether it may).

## Milestone 10: not a milestone yet

Zero registry entries, and this milestone is structurally unlike every
other one in the plan.

It has no `#### Implement` heading and no `#### Acceptance criteria`
heading. It opens with *"These are separate optional extensions."* and
divides into scheduling, a second model provider with routing,
subagents, and a `#### Gate for multi-agent work` — which is an entry
condition stating when subagents may be built, not a statement of what
is true once they are. Milestone 9 also lacks an `#### Implement`
heading but does have acceptance criteria; Milestone 10 is the only
one with neither.

All seven scheduling requirements are undesigned. Of six routing
considerations, data residency and evaluation performance have no
design; the other four are at least touched by the model gateway's
routing section. Of nine subagent requirements, five have none — the
explicit objective, restricted context, child deadline, separate
trace, and artifact references rather than a full transcript — and two
more are partial.

The honest verdict is that this is a direction rather than a
milestone, and the plan says as much. It is listed here for
completeness and because its zero in the gate column, unlike Milestone
6's, is not an anomaly to explain.

## The four plan sections no specification expands

Sections 28 through 31 are the only major sections of the engineering
plan with no outward cross-reference paragraph. A scan of lines 3136
through 3364 for links to other documents returns nothing, where every
other major section acquired one during the specification work. Three
of the four are genuinely unexpanded; the fourth is half-expanded from
the consuming side.

### 28. Sandbox isolation architecture

Seventy-seven lines, ADR-0008, a threat model, and no expansion.
Covered above under Milestone 6. This is the highest-consequence gap
in the corpus: it is the only unexpanded section whose failure mode is
an escape from the trust boundary rather than a feature that does not
work.

### 29. Multi-device operation and the shared core

Eighty-seven lines, ADR-0011, four inbound consuming references, and
no expansion. The section's core claim is defensible without one —
because PostgreSQL is the source of truth and devices are API clients,
sharing is a consequence of the existing architecture rather than a
new mechanism. What it introduces beyond that is the `Device` concept
and four named ports for capabilities that are inherently local to one
machine, and none of the four has a contract.

`tool-system.md:1238` does open a *"Device-scoped tools"* section, and
`tool-system.md:1665` states that device tools are *"a reserved seam,
not a design"*. That is an explicit deferral rather than an oversight,
and it is the right call for a Milestone 10-adjacent concern. The
`Device` model itself still has no home.

### 30. Self-improving skills

Forty-three lines, ADR-0013, **eleven inbound consuming references**,
and no expansion. The reference count is what makes this notable. Ten
other documents treat the skill mechanism as settled and build on top
of it, and the mechanism does not exist below the plan. Combined with
the Milestone 8 finding — no package format, no manifest schema, no
selection algorithm — this is the second-largest undesigned area in
the corpus after the sandbox, and the one with the most load already
resting on it.

### 31. Trajectory capture and export

Twenty-two lines, ADR-0016, and the only one of the four with real
design outside the plan — on one side.
[evaluation-harness.md](evaluation-harness.md) fully specifies the
consumption path: the conversion from a captured run to a case, the
`source: trajectory` marking that keeps promoted cases distinguishable
from authored ones, the `agent eval promote` command, and a hard gate.

The production path has nothing. The export format the section names
as an example — ShareGPT or messages — is not chosen. The redaction
rules that make the acceptance criterion *"no secrets, raw reasoning,
or restricted PII"* checkable do not exist, and neither does the
consent gate that criterion also requires. Redaction of raw reasoning
in particular interacts with ADR-0006 and ADR-0007, which forbid
persisting it in the first place, so the two documents need to be read
together by whoever writes this.

## What the evaluation suite does not reach

The twenty-five initial cases carry milestones 1, 2, 4, 5, and 6.
Milestones 3, 7, 8, 9, and 10 have no case rows at all.

For Milestone 3 this is defensible: provider adapters are covered by
contract suites and live smoke tests rather than by end-to-end cases,
and the harness says so. For Milestones 7, 8, and 9 it is a real gap,
and it lines up exactly with the gaps found by reading the specs.
Milestone 7 has a long-session gate with no case behind it. Milestone
8's acceptance criteria require a mock MCP server that is not
designed. Milestone 9's memory criteria are stated in terms of
evaluation improvement — *"Memory improves defined evaluation cases
without increasing policy failures"* — against a case set that
contains no memory case.

Two additional holes are worth naming because a criterion exists and
nothing can check it: the container-escape red-team test Section 28
demands, and the path-traversal algorithm case 19 exercises but no
document specifies.

The harness is not at fault here. It specifies how cases are written,
what the sixteen assertion types are, and how a case declares its
milestone. What is missing is cases, which are cheap to add once the
mechanisms they exercise are designed, and impossible to write before.

## Conflicts this document resolves

This document resolves none. It reports four and defers each to the
document that owns the subject. One has since been resolved by the
document it was deferred to, and the resolution is recorded under it.

1.  **`sandbox.run_command` at Milestone 5 or Milestone 6.**
    `builtin-tools.md:909` against the plan's Milestone 6 implement
    list. The map follows the plan. Resolution belongs to the sandbox
    specification, because whether the tool can ship against the
    development mechanism before container backing exists is a sandbox
    question.
2.  **Usage token classes and cost-source precedence at Milestone 2 or
    Milestone 3.** `engineering-plan.md:2444` against
    `model-gateway.md:1259` and `milestone-map.md:561`. The map
    follows the gateway. Nothing is built differently either way; only
    the migration's timing changes.
3.  **`Idempotency-Key` and the idempotency port.** Named as an HTTP
    header at Milestone 5 and as a tool-call port at Milestone 1.
    Whether these are one mechanism or two is undecided, and belongs
    to the API specification. Resolved there as two: two scopes, two
    tables, two milestones, one unfortunate name.
4.  **The container-escape test and the case table.**
    `engineering-plan.md:3209` requires a test the harness's case set
    does not contain. Belongs to the sandbox specification and the
    harness together.

## Decisions

1.  **Coverage is judged against the whole corpus, not against the
    specifications alone.** A mechanism designed in the engineering
    plan and nowhere else is covered, and is reported as
    plan-only so the distinction stays visible. The alternative —
    scoring Section 16 as absent because no file under `docs/plan/`
    other than the plan expands it — would have produced a Milestone 5
    verdict that is false in the way that matters, since an
    implementer reading Section 16 can build most of that milestone.
2.  **An explicit deferral scores as partial, not absent.**
    `builtin-tools.md`'s six-tool section and `tool-system.md`'s
    device-tool seam both name what they do not design. A hole
    somebody has measured is a different object from one nobody has
    looked at, and collapsing the two would make this document less
    useful precisely where the corpus is most self-aware.
3.  **The gate census is treated as independent evidence.** It was
    derived mechanically before this review began and was not
    consulted while tracing implement bullets. Where it agrees with a
    verdict, that agreement is reported as corroboration; where it
    would disagree, the disagreement would be reported. It does not
    disagree anywhere.
4.  **This document reports conflicts and resolves none.** The
    milestone map owns scheduling and the specifications own their
    requirements. A readiness review that also decided things would
    become a document other documents have to be reconciled against,
    which is the problem the map was written to solve.

## Open questions for review

1.  **Is the API specification the next document, or does the API get
    built from Section 16?** Answered by writing it. The
    recommendation was that it should be the next document, on the
    evidence that every other section expanded exactly once and every
    expansion found a conflict. The counter-argument was real: Section
    16 is more detailed than the sections that turned out to hide
    contradictions, HTTP has stronger conventions than composition
    roots do, and the six unsettled items listed above are
    individually small. The expansion found nine contradictions, which
    settles it on evidence rather than on the prediction.
2.  **Does the sandbox specification precede Milestone 5, or follow
    it?** Milestone order says it follows. Two arguments say it should
    come first: `tool-system.md:977` already depends on an egress
    allowlist it establishes, and it is the only undesigned area whose
    failure mode is a security boundary rather than a missing feature.
    Writing it early costs nothing except the order in which two
    documents get written.
3.  **Should Milestone 6 acquire gates before it is built?** The map
    reported the zero rather than inventing entries, which was right.
    But a sandbox with no registered invariant is a different kind of
    zero from a Milestone 8 with no registered invariant, and the
    sandbox specification is the natural place to fix it.
4.  **Does Milestone 10 need acceptance criteria, or is it correctly
    an open direction?** It is the only milestone with neither an
    implement list nor acceptance criteria, and the plan calls its
    contents *"separate optional extensions"*. Leaving it as a
    direction is defensible; leaving it in a numbered milestone
    sequence while every other entry has criteria is what makes it
    look like an omission rather than a choice.
5.  **Is knowledge retrieval a Milestone 9 deliverable or its own
    milestone?** It is half of Milestone 9's title and has no design,
    while the memory half has two complete specifications and fourteen
    gates. Splitting it out would make Milestone 9 shippable against
    what is actually designed.
