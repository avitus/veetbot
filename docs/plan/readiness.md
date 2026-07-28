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
    section titled *"The two tools this document does not design"* —
    six when this review ran, four of which have since been designed
    — that names each one, assigns it a milestone, and lists what it
    still owes. That is a smaller and better-understood hole than an
    item nobody has looked at, and it is scored separately.
3.  **A reserved seam is not an omission** where the document says so.
    `tool-system.md:1698` states that device tools are *"a reserved
    seam, not a design"*. The seam is the decision.

The gate census in [milestone-map.md](milestone-map.md) was used as an
independent check on every verdict below. It was derived mechanically
from the specifications, the plan, and the map, without reference to
this review, and where the two disagree the disagreement is reported
rather than smoothed.

## The verdict

| M | Subject | Verdict | Gates | What stands between it and code |
| --- | --- | --- | --- | --- |
| 0 | Repository and engineering foundation | Ready | 13 | Nothing |
| 1 | In-memory vertical slice | Ready | 28 | Nothing |
| 2 | PostgreSQL persistence and durable worker | Ready | 16 | Nothing |
| 3 | Model adapters and normalized streaming | Ready | 15 | Nothing |
| 4 | Policy, approvals, and tool lifecycle | Ready | 22 | Nothing |
| 5 | HTTP API and SSE | Ready | 11 | Nothing |
| 6 | Isolated execution and artifacts | Ready | 11 | Nothing |
| 7 | Context budgeting and working state | Ready with named gaps | 6 | No evaluation cases |
| 8 | Skills and MCP integration | Ready with named gaps | 14 | MCP auth scheme |
| 9 | Long-term memory and knowledge | Ready with named gaps | 14 | Knowledge documents |
| 10 | Scheduling, routing, and subagents | Not a milestone yet | 6 | No acceptance criteria exist |

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
Milestone 6's eleven arrived the same way, from
[sandbox-isolation.md](sandbox-isolation.md), and took one of the two
zeros in the table that belonged to a milestone doing real work.
[skills.md](skills.md) took the other, giving Milestone 8 ten; the
four that bring it to fourteen came later, from the pass that noticed
build step 9 of the tool system had nothing observing it, and they
are the reason the mock-server gap this table first named is gone.
Its six at Milestone 10 are a different case: they are the authoring
loop's gates, registered against a milestone whose own acceptance
criteria still do not exist, which is why that row's verdict is
unchanged. Milestone 3's eleven became fifteen last, and the same way:
two from [model-gateway.md](model-gateway.md) and two from
[event-log-and-persistence.md](event-log-and-persistence.md), each
pair arriving with the design that closed one of this section's named
gaps rather than being added to make a count look better.

## Milestone 0: ready

Thirteen registry entries, every deliverable expanded.

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
[event-log-and-persistence.md](event-log-and-persistence.md) supplies
the thirteenth, and it is the one gate here that belongs to a later
milestone's subject: the migration-graph walk registers at Milestone 0
because Milestone 0 already requires that an empty Alembic migration
runs, and a walk that only begins once twelve revisions exist has
already missed the branch it exists to prevent.

Nothing here is blocked. This is the milestone the corpus has been
examined against most often, and it is the one with the least left to
decide.

## Milestone 1: ready

Twenty-eight registry entries, the largest count of any milestone, and
every implement bullet has a body. The twenty-eighth arrived later,
with [sandbox-isolation.md](sandbox-isolation.md): the composition
root refuses to build the development sandbox mechanism when the
environment is production.

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

**Forty-one of one hundred and fifty-six gates are green before
Milestone 2 begins**, thirteen of them against a repository with no
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

Sixteen registry entries. Nine of eleven implement bullets were
covered on the first pass by
[event-log-and-persistence.md](event-log-and-persistence.md) and
[runtime-loop.md](runtime-loop.md): the append-only event store, the
projections and their rebuild, `FOR UPDATE SKIP LOCKED` claim,
`lease_epoch` fencing, checkpoint and resume, the reaper, and the
transaction-hygiene gate ADR-0024 separated from its Milestone 0
check.

Two bullets were partial, and both were tooling rather than
architecture. Both are closed now, in the document that already owns
the schema and the `gate.event.*` area rather than in a twentieth
specification, and recorded as
[ADR-0031](../adr/0031-persistence-authoring.md).

1.  **The SQLAlchemy adapter had no ORM surface.** The schema was
    fully specified, table by table, but no document stated whether
    mappings are declarative or imperative, where the session factory's
    boundary sits relative to the unit of work, or what the repository
    method bodies look like against it. ADR-0024 fixed the one
    property that could be checked without any of that — a factory is
    constructed, never a session, and no `AsyncSession` exists at
    module scope. *"The ORM surface"* answers the rest, and answers
    it by elimination rather than by preference: declarative mapping
    of a domain type fails rule 1 on the import walk, imperative
    mapping fails rule 7 silently, and what survives is a separate row
    class per table, two hand-written translation functions beside it,
    and a repository constructed with a live session that never
    commits. `gate.structure.orm_confined` asserts the confinement.
2.  **Alembic had no authoring conventions.**
    `engineering-plan.md:1619` says *"Create Alembic migrations for at
    least these tables"* and `development-toolchain.md:183` supplies
    the `make migrate` target. Between them there was no statement of
    naming, no branch policy, no rule for data migrations versus
    schema migrations, and no design for how the composition root
    asserts the revision it refuses to start without — which ADR-0024
    decision 6 requires and does not specify. *"Authoring
    migrations"* supplies all four: a linear graph with one head,
    slugged file names that carry no order, structural and data
    revisions kept separate, and `EXPECTED_REVISION` as a constant
    rather than a head computed at runtime from the migrations that
    shipped alongside the code.

Closing the second bullet surfaced a defect this review had missed.
Two of Section 24's criteria — migrations upgrade from a clean
database, and migrations upgrade from the previous revision — are
conditions of *every* milestone, and nothing evaluated either of them.
That is the same class of finding the milestone map produced by
counting gates, arrived at from the opposite direction, and it is why
four of the five gates added here observe migrations rather than the
ORM.

One milestone conflict is reported and not resolved here. The plan
places *"Usage token classes and cost-source precedence in the schema
(Section 6.5)"* in Milestone 2 at `engineering-plan.md:2450`, while
[model-gateway.md](model-gateway.md) designs it and sequences it to
Milestone 3, and the map follows the gateway. The schema column can
exist a milestone before anything writes to it, so this is a question
of whether the migration ships early or the whole item ships late; it
does not change what gets built.

## Milestone 3: ready

Fifteen registry entries. [model-gateway.md](model-gateway.md) is one
of the more complete specifications in the corpus: both provider
adapters, the normalized streaming event set with the exact field
mappings pinned for Anthropic Messages and OpenAI Responses, retry
classification, the reasoning-state handling ADR-0007 requires, and
the usage and cost model.

Three items fell short, one of them completely.

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
    production half of a section whose consumption half is fully
    specified.

### What closed it

All three, in documents that already owned the subject.

Item 1 is closed by [model-gateway.md](model-gateway.md), which makes
`provider_metadata` a closed set: a declared field list, a persisted
column on `model_calls`, and exactly two readers. The discipline is
the point, not the field — an open dictionary is where
provider-specific data accumulates until something depends on it, and
a closed one cannot. It registers `gate.model.metadata_closed`.

Item 2 is closed in the same document, which gives the provider
profile a document schema, a required set, a rule table the loader
enforces, and a stated answer for a profile that claims a capability
its adapter does not have. Its gate,
`gate.model.profile_valid`, is a corpus gate rather than a case,
because one invalid profile proves only the rule it violates and the
rule table has a row for each.

Item 3 is closed by
[event-log-and-persistence.md](event-log-and-persistence.md) and
[ADR-0032](../adr/0032-trajectory-export-redaction-and-consent.md),
which choose the format, make the export an artifact rather than a
table, specify redaction as three stages that fail closed, and design
the consent record the corpus had asserted four times and never
defined. It is covered below under Section 31, and it registers
`gate.event.export_redacted` and `gate.event.export_consent`.

Four gates for three gaps, all at Milestone 3, which is the same
correlation the verdict table reports: a milestone acquires gates when
somebody writes down what it must be true of. The verdict is ready.

## Milestone 4: ready

Twenty-two registry entries.
[policy-and-approvals.md](policy-and-approvals.md) covers the
deterministic decision function, the hardline set, profile compilation
and freezing, `policy_version`, the approval record and its lifecycle,
expiry, resume revalidation, and the injection corpus.
[tool-system.md](tool-system.md) covers the execution pipeline
end to end. The thirteenth entry arrived later, with
[sandbox-isolation.md](sandbox-isolation.md): a property test over
`WorkspaceHandle.resolve`, which acquires its first callers at this
milestone. The last nine arrived later still, from the two passes that
closed the two gaps below.

Two things were outstanding.

**Four builtin tools were classified but not designed**, and the
document said so. `builtin-tools.md` named `workspace.read_text`,
`workspace.write_text`, `workspace.list_files`, and
`demo.external_write`, assigned each to Milestone 4, and listed what
each still owed: path resolution and traversal rejection, encoding and
binary-file rules, the checksum algorithm, the listing's limit and
ordering, and the `WorkspaceHandle` the execution context already
carries. It also fixed two constraints in advance rather than leaving
them to be noticed — the reader lowers `output_trust` to
`EXTERNAL_UNTRUSTED` for any file whose provenance in the run is not
established, and the establishing set is what this run's
`workspace.write_text` produced.

That was a bounded and well-understood hole, but one consequence was
worth stating plainly: the Milestone 4 acceptance criterion *"Path
traversal is rejected"* and eval case 19 both stood on an algorithm
that no document then contained. A rejection rule is exactly the kind
of thing that is written three different ways by three implementers,
two of which are subtly wrong.

**Principal scopes were half-designed.** The `Principal` model lived
only at `engineering-plan.md:459`, the policy spec identified where
scopes are checked, and nothing stated the scope vocabulary, its
grammar, or the comparison algorithm — whether a scope was an opaque
string, a hierarchy, or a pattern. Relatedly,
`bootstrap-and-composition.md:450` named `ApprovalService` as one of
the services `build` returns, and no document gave it a method
signature.

### What closed it

Both, on two passes.

The traversal half closed first and separately.
[sandbox-isolation.md](sandbox-isolation.md) specifies
`WorkspaceHandle.resolve` as a five-step containment rule, says for
each step why it rejects rather than normalizes, and registers
`gate.sandbox.workspace_containment` at this milestone — the
thirteenth entry counted above. The sentence about an algorithm no
document contained was true when it was written and stopped being
true with that document, which is why it is now in the past tense.

What remained was the tool half, and
[builtin-tools.md](builtin-tools.md) now designs all four: the
prohibition that no `workspace.` tool resolves a path at all, strict
UTF-8 with a NUL byte as the binary test, a SHA-256 checksum over the
encoded bytes, a listing capped at a thousand entries and ordered so
that a truncated one is a prefix of the full one, six JSON schemas,
four reason codes, and `demo.external_write`'s record as the
`structured` result the pipeline already persists.

Two of its answers are more than transcription. Provenance became a
property of the workspace rather than a repository query, because
`ToolExecutionContext` deliberately carries no database session:
`WorkspaceHandle` gains one method and one enum, `write` records
`TOOL_WRITTEN` in the same operation that writes the bytes, and
Milestone 6's `SANDBOX_WRITTEN` is defined two milestones early so
that it cannot later be treated as establishing trust. And the reader
has no size-limit failure of its own, deferring instead to the
execution pipeline's excerpt-and-artifactize step, because a second
ceiling is a second truncation policy to keep in agreement with the
first.

Six gates, all at Milestone 4: `gate.builtin.handle_only`,
`gate.builtin.text_only`, `gate.builtin.write_idempotent`,
`gate.builtin.listing_stable`, `gate.builtin.provenance`, and
`gate.builtin.demo_records`.

The scope gap closed on the pass after that one, and half of it had
already closed on its own.
[http-api-and-streaming.md](http-api-and-streaming.md) gave
`ApprovalService` the three-method Protocol the composition spec had
been naming without one, and enumerated the nine scopes the API
checks. What was left was the tool half — the five more that appear
as `ToolSpec.required_scopes` on the builtin roster — and the three
questions no document had answered: what a scope is, how two of them
are compared, and where a worker gets the set.

[policy-and-approvals.md](policy-and-approvals.md) answers all three
in one section. The vocabulary is one closed set of fourteen strings
rather than an API namespace and a tool namespace, because
`artifact.read` and `artifact.write` are two actions on one resource
and not two vocabularies that happen to collide. A scope is an opaque
string compared by exact match, the check is a set difference and
all-of, and there is no hierarchy, no wildcard, and no prefix rule —
so `run.write` does not satisfy `run.read`, and a tool that means
both declares both.

The closed list has one deliberate seam. An MCP tool's
`required_scopes` are operator-declared, so a list closed against
them is a list the operator routes around. The rule is that a
declared scope is legal if it is one of the fourteen or its first
segment is `mcp` and its second is the server id. That lets an
operator classify a remote tool's risk without letting one borrow
`session.write`, which is the escalation worth blocking: a
requirement that reads as a restriction and grants.

The worker's copy is a stamp. `runs.principal_scopes` is written at
submission and `PrincipalResolver.for_run` reads it rather than a
principal table, because a worker holds no credential, and
re-deriving would make the runtime loop's *"takes effect on the next
run"* depend on queue latency rather than on submission order. It is
a Milestone 2 column with a Milestone 4 reader, which is the shape
[ADR-0032](../adr/0032-trajectory-export-redaction-and-consent.md)
already chose for the consent stamp and for the same reason.

Three gates, all at Milestone 4: `gate.policy.scope_grammar`,
`gate.policy.scope_match`, and `gate.policy.scope_stamped`. Both
named gaps are closed, so the verdict changes with them.

## Milestone 5: ready

Eleven registry entries. There was one when this review was written —
the fewest of any milestone that adds work — and that number was the
finding.

Section 16 of the engineering plan, at
`engineering-plan.md:1793-1946`, designs the API more thoroughly than
a summary of this milestone's coverage would suggest. It specifies
nine endpoints with methods, paths, and where relevant headers;
request and response JSON for session creation, message submission,
and approval resolution; the SSE frame format with `id`, `event`, and
`data` lines; the reconnect rule that replays persisted events after
`Last-Event-ID` and then continues streaming; the five cooperative
cancellation observation points; the error envelope; and the
readiness constraint that a probe must not call a provider.

What did not exist was any expansion of that section. No
detailed-design specification covered the API layer. The only HTTP
routes designed outside the plan were three: the two approvals reads
at `policy-and-approvals.md:995-996` and the resolve at
`policy-and-approvals.md:1005`, and one reference in
`runtime-loop.md:1172` to `POST /runs/{id}/input` that routed to an
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
    `engineering-plan.md:1835` and as an implement bullet, and the
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

## Milestone 6: ready

Eleven registry entries. There were none when this review was written
— the only zero in the table belonging to a milestone that does work
— and eight of twelve implement bullets had no
design outside the plan: the container-backed execution adapter,
workspace lifecycle, resource limits, no-network execution,
`sandbox.run_command`, the filesystem artifact store, artifact
metadata and content endpoints, and workspace cleanup.

Section 28 of the plan is not empty — `engineering-plan.md:3152-3229`
states a six-item threat model that assumes model-generated code is
hostile, and is recorded as ADR-0008. But it was not expanded, and
two specifications pointed at the expansion as though it already
existed. `tool-system.md:977` constrains MCP server URLs by *"the
egress allowlist the sandbox spec establishes"*, and there was no
sandbox spec.
`bootstrap-and-composition.md:180` and `:183` assign ownership of
`ArtifactStore` and `ExecutionEnvironment` to the engineering plan
itself, which is the corpus recording that nothing below the plan owns
them.

Two bullets are covered. Output truncation and artifactization are
specified at `tool-system.md:708`, and the programmatic orchestration
bridge Section 8.5 requires is specified from `tool-system.md:1169`.

Two further items deserved naming.

1.  **The plan demands a red-team test with no case behind it.**
    `engineering-plan.md:3215` requires a container-escape attempt as
    a security test. The twenty-five-case table contains no such case
    and no Milestone 6 security row.
2.  **`sandbox.run_command` was placed at two milestones.**
    `builtin-tools.md` said Milestone 5 where `builtin-tools.md:1399`
    now says Milestone 6; the plan's Milestone 6 implement list
    contains it. The map follows the plan. This was reported rather
    than resolved, because the right answer depended on a sandbox
    specification that did not exist: if the tool can ship against the
    development mechanism at Milestone 5 and gain container backing at
    Milestone 6, both documents are right about different things.

The zero in the gate column was worth dwelling on. Milestone 6 is the
milestone whose failure mode is a container escape, and it was one of
two milestones that registered no gate at all. Every invariant its
work strengthens was registered against an earlier milestone, which
meant the gate registry contained no statement that becomes true
because the sandbox was built.

### What closed it

[sandbox-isolation.md](sandbox-isolation.md) and ADR-0029 exist now,
and they settle the eight uncovered bullets, both named items, and the
zero.

The eight types the corpus referenced and never declared are declared:
`EnvironmentSpec`, `ResourceLimits`, `EnvironmentHandle`,
`ExecutionCommand`, `ExecutionResult`, `ArtifactMetadata`,
`ArtifactRef`, and the `FileChange` and `KillReason` that
`ExecutionResult` needs, together with `WorkspaceHandle`,
`ArtifactWriter`, and `CredentialResolver` from
`ToolExecutionContext`. That removes the last of the
referenced-and-undeclared types the API specification named as
remaining. The egress allowlist `tool-system.md:977` depends on by
name gets a grammar, an owner, and two enforcement points, of which
the address denylist runs first and no allowlist entry can waive it.
Workspace lifecycle is settled by a rule rather than a mechanism —
the workspace is a cache held for a worker's lease, not state held
for a run — which makes cleanup and crash-resume the same operation.
Resource limits become numbers with operator ceilings. Artifacts get
a derived storage key, a checksum verified on the way out, and a
retention rule.

Item 1 is closed by harness case 26, a security case at Milestone 6
registered as `gate.sandbox.escape_denied`, added without renumbering
any of the twenty-five. Item 2 is closed against `builtin-tools.md`:
Section 8.2's *"only after the sandbox milestone"* is Milestone 6, and
the spec's Milestone 5 was an off-by-one against a milestone list in
which 5 is the HTTP API. The tool does not ship early against the
development mechanism, because the development mechanism refuses to
run in production and a tool that only works in development is not a
milestone deliverable.

The zero is closed by thirteen gates in a new twelfth registry area,
`sandbox`, of which eleven are Milestone 6. One is Milestone 1, where
the composition root learns to refuse the development mechanism, and
one is Milestone 4, where `WorkspaceHandle` acquires its first
callers. The verdict is ready.

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

**What changed.** Shortfall 2 is closed.
[evaluation-harness.md](evaluation-harness.md) case 28 gives this
milestone its first row: the fifty-turn session with one distinct
`prefix_sha256`, which is `gate.context.prefix_stability` given
something to run. Shortfall 1 stands and is the one to close first,
because a selection order with no predicate is the kind of gap that
looks specified until two runs disagree.

## Milestone 8: ready with named gaps

Fourteen registry entries: ten from [skills.md](skills.md), which was
written after this review and closes the half that was missing, and
four added later still for the MCP half. The verdict below is the
review as it stood, followed by what changed.

**MCP is substantively covered.** [tool-system.md](tool-system.md)
designs nine of eleven bullets: server configuration and lifecycle,
tool discovery and namespacing, the trust labeling that makes MCP
results `EXTERNAL_UNTRUSTED`, failure isolation, timeouts, and the
reserved-domain collision rules. Two are partial: authentication
configuration has a `credential_ref` column and no auth scheme and no
refresh or re-auth path, and the mock MCP server the *"Mock MCP
server tests"* implement bullet requires is never designed, with no
MCP fixture format in the harness and no Milestone 8 row in the case
table. The bullet, not a criterion: this review said "acceptance
criteria" and the acceptance criteria say nothing about mocks.

**Skills had no specification below the tool system.** The stronger
claim this review first made — that skills have no specification at
all — was wrong, and the correction matters because it changes what
had to be written. `tool-system.md:1110-1157` draws the line between
a skill and a tool, fixes the metadata block at four fields, puts
`required_tools` checking at load rather than at authoring, assigns
trust by author, and classifies `skill_manage`. That is real design.
What was missing was everything underneath it: no package format, no
manifest schema, no types, no storage, no reference grammar, no
context accounting, and no gates. The acceptance criterion *"A
selected skill is version-pinned in the run"* at
`engineering-plan.md:2696` had no design behind it — and no document
outside the plan and ADR-0013 mentioned `SKILL.md`, which was true
and remains the sharper of the two observations.

Section 30, self-improving skills, compounded this. It is referenced
from eleven places in the corpus as though the mechanism it describes
were settled.

**What changed.** [skills.md](skills.md) supplies the package format,
the manifest schema, the `SkillRef` grammar, two tables and an
archive, the session-open catalog with its caps, `skill.load` and its
stickiness, two new context classes, the authoring loop at Milestone
10, and sixteen gates in a new `skill` area — ten of them here. It
also corrects `skill_manage`'s classification, which
`tool-system.md` had called a control tool while giving it a write
scope, and settles the scope's spelling. Harness case 27 gives this
milestone its first row in the case table, so the clause above about
having none is true only of the review as it stood.

The MCP half was closed on a later pass, after the milestone map's
census made it visible that build step 9 of the tool system was the
only step in that document with no gate observing it.
[tool-system.md](tool-system.md) gains three gates —
`gate.tool.mcp_pipeline_parity`, `gate.tool.mcp_disconnect`, and
`gate.tool.mcp_sdk_confined`, the last of which promotes the *"no
direct dependency on MCP SDK types"* criterion from prose to a walk
over the import graph — and
[evaluation-harness.md](evaluation-harness.md) gains a fourth fixture
kind, the scripted MCP server, plus `gate.harness.mcp_no_socket` and
cases 29 and 30. The mock server the implement bullet asked for is
that fixture kind: authored YAML, loaded at collection time, no socket
and no subprocess.

One named gap survives, and it is the smaller one: authentication
configuration still has a `credential_ref` column, no auth scheme, and
no refresh or re-auth path. It is a Milestone 8 implementation
question rather than a design hole in the corpus, because the broker
that resolves the reference is specified and only the scheme it
resolves is not.

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
completeness and because its gate column is no longer zero:
[skills.md](skills.md) registers six gates here for the skill-authoring
loop. Those gates come from a section of the plan rather than from
this milestone's own criteria, which it still does not have, so the
verdict is unchanged by them.

## The three plan sections no specification expands

Sections 29 through 31 are the only major sections of the engineering
plan with no outward cross-reference paragraph. A scan of
`engineering-plan.md:3231-3384` for links to other documents returned
nothing when this review was written, where every other major section
acquired one during the specification work. Two of the three are
genuinely unexpanded; the third is half-expanded from the consuming
side.

Section 28 was the fourth, and it is expanded now, by
[sandbox-isolation.md](sandbox-isolation.md), which gives it the
outward paragraph the scan looked for. It was the
highest-consequence gap in the corpus while it lasted — the only
unexpanded section whose failure mode was an escape from the trust
boundary rather than a feature that does not work — and it is covered
above under Milestone 6.

### 29. Multi-device operation and the shared core

Eighty-seven lines, ADR-0011, four inbound consuming references, and
no expansion. The section's core claim is defensible without one —
because PostgreSQL is the source of truth and devices are API clients,
sharing is a consequence of the existing architecture rather than a
new mechanism. What it introduces beyond that is the `Device` concept
and four named ports for capabilities that are inherently local to one
machine, and none of the four has a contract.

`tool-system.md:1252` does open a *"Device-scoped tools"* section, and
`tool-system.md:1698` states that device tools are *"a reserved seam,
not a design"*. That is an explicit deferral rather than an oversight,
and it is the right call for a Milestone 10-adjacent concern. The
`Device` model itself still has no home.

### 30. Self-improving skills

Forty-three lines, ADR-0013, **eleven inbound consuming references**,
and no expansion. The reference count is what made this notable. Ten
other documents treated the skill mechanism as settled and built on
top of it, and the mechanism did not exist below the plan. Combined
with the Milestone 8 finding, this was the largest undesigned area in
the corpus and the one with the most load already resting on it.

[skills.md](skills.md) and ADR-0030 close it. Section 30's six
subsections keep their content: 30.1's milestone placement, 30.2's
two authoring paths, 30.3's six governance guarantees — versioning,
provenance, gating, restricted review, injection resistance, and
sandboxed scripts — 30.4's metadata-only loading rule, 30.5's rollout
criterion, and 30.6's constraints are each carried forward rather
than reinterpreted. Two citation errors are corrected in the process.
One is this review's: the version-pinning criterion is at
`engineering-plan.md:2696`, and the line this review first named was
an MCP configuration bullet a few lines above it. A line-number
citation into the plan is correct only until the plan is next edited,
which is why every citation in this corpus is now recorded in
`docs/status/citation-ledger.yaml` and checked by
`scripts/check_citations.py` rather than remembered. One the corpus
carried: `policy-and-approvals.md` attributes the policy-and-approval
gating requirement to Section 30.4, which Section 30.3 states.
What remains open is a threshold for 30.5's eval delta, which is a
number nobody has yet had the data to choose.

### 31. Trajectory capture and export

Twenty-two lines, ADR-0016, and the only one of the three with real
design outside the plan — on one side.
[evaluation-harness.md](evaluation-harness.md) fully specifies the
consumption path: the conversion from a captured run to a case, the
`source: trajectory` marking that keeps promoted cases distinguishable
from authored ones, the `agent eval promote` command, and a hard gate.

The production path had nothing. The export format the section names
as an example — ShareGPT or messages — was not chosen. The redaction
rules that make the acceptance criterion *"no secrets, raw reasoning,
or restricted PII"* checkable did not exist, and neither did the
consent gate that criterion also requires. Redaction of raw reasoning
in particular interacts with ADR-0006 and ADR-0007, which forbid
persisting it in the first place, so the two documents needed to be
read together by whoever wrote this.

[event-log-and-persistence.md](event-log-and-persistence.md) and
ADR-0032 close it, in the document that already owns the log the
export reads and the `gate.event.*` area, rather than in a fourteenth
specification. The format is one versioned JSON document in the
`messages` shape, with ShareGPT left to a consumer as the rename of a
role vocabulary that it is. The export is written into the artifact
store under a new `TRAJECTORY_EXPORT` origin, which supplies content
addressing, an authorized read path, and the `expires_at` sweeper a
governed export needs and a new table would have had to grow.
Redaction is structural exclusion, then pattern replacement reusing
the committed-secret scanner's five rule families and the log
processor's key-name families, then a verification scan that raises
and writes nothing rather than redacting a second time. The
ADR-0006 interaction resolves in the simplest available direction:
reasoning is never persisted, so the builder has nothing to redact
and the exclusion is structural. Consent becomes a record — granted
per principal, evaluated at run start and stamped on the run,
withdrawn backward across every run and every artifact already
produced, with the deletion routed through `expires_at` and the
sweeper so the rarest governance operation runs on the most exercised
code. `agent run export` is a subcommand rather than a thirteenth
top-level command, on the precedent
[evaluation-harness.md](evaluation-harness.md) set with `agent eval`.

That leaves one. Section 29's `Device` model is the only part of
Sections 29 through 31 that no specification now expands.

## What the evaluation suite does not reach

The twenty-five initial cases carried milestones 1, 2, 4, 5, and 6,
and so did the twenty-sixth, added later by
[sandbox-isolation.md](sandbox-isolation.md). The twenty-seventh,
added by [skills.md](skills.md), was the first Milestone 8 row.
Milestones 3, 7, 9, and 10 had no case rows at all, which was this
section's finding.

For Milestone 3 that is defensible and stays: provider adapters are
covered by contract suites and live smoke tests rather than by
end-to-end cases, and the harness says so. Milestone 10 keeps its
empty row deliberately, since the authoring loop is an optional
extension. Milestones 7 and 9 were real gaps and lined up exactly with
the gaps found by reading the specs: Milestone 7 had a long-session
gate with no case behind it, and Milestone 9's memory criteria are
stated in terms of evaluation improvement — *"Memory improves defined
evaluation cases without increasing policy failures"* — against a case
set that contained no memory case. Milestone 8's gap was half of one,
its MCP side having a bullet and no row.

**All three are closed.** Cases 28 through 31 were added to
[evaluation-harness.md](evaluation-harness.md) on the pass that
followed the milestone map's census, which is what made the three
holes countable rather than merely noticed. Case 28 is the fifty-turn
prefix-stability run at Milestone 7. Cases 29 and 30 are the MCP round
trip and the mid-call disconnect at Milestone 8, both against a
scripted server fixture. Case 31 is the memory delta at Milestone 9,
and it needed a mechanism the harness did not have: a case that
declares two arms and asserts a relation between them rather than a
number, because *"without increasing policy failures"* is a comparison
and no single run can express it. That mechanism — `arms`, `carry`,
and `delta` — is also what case 27 had been describing in prose
without a schema to write it in.

Two additional holes were worth naming because a criterion existed
and nothing could check it: the container-escape red-team test
Section 28 demands, and the path-traversal algorithm case 19
exercises but no document specified. Both are closed by
[sandbox-isolation.md](sandbox-isolation.md) — the first by case 26
and `gate.sandbox.escape_denied`, the second by the five-rule
containment function `WorkspaceHandle.resolve` and the property gate
`gate.sandbox.workspace_containment` over generated paths.

The harness was not at fault. It specifies how cases are written, what
the assertion types are, and how a case declares its milestone. What
was missing was cases, which are cheap to add once the mechanisms they
exercise are designed and impossible to write before — which is
exactly why they arrived last, and why two of the four needed a
schema change rather than only a table row.

## Conflicts this document resolves

This document resolves none. It reports four and defers each to the
document that owns the subject. Three have since been resolved by the
documents they were deferred to, and each resolution is recorded
under the conflict it settles.

1.  **`sandbox.run_command` at Milestone 5 or Milestone 6.** The
    spec against the plan's Milestone 6 implement list. The map
    follows the plan. Resolution belongs to the sandbox
    specification, because whether the tool can ship against the
    development mechanism before container backing exists is a sandbox
    question. Resolved there in the plan's favour: the development
    mechanism refuses to start in production, so a tool that only
    works against it is not a milestone deliverable, and the spec's
    Milestone 5 was an off-by-one against a list in which 5 is the
    HTTP API. `builtin-tools.md:1399` now says Milestone 6.
2.  **Usage token classes and cost-source precedence at Milestone 2 or
    Milestone 3.** `engineering-plan.md:2450` against
    `model-gateway.md:1735` and `milestone-map.md:804`. The map
    follows the gateway. Nothing is built differently either way; only
    the migration's timing changes.
3.  **`Idempotency-Key` and the idempotency port.** Named as an HTTP
    header at Milestone 5 and as a tool-call port at Milestone 1.
    Whether these are one mechanism or two is undecided, and belongs
    to the API specification. Resolved there as two: two scopes, two
    tables, two milestones, one unfortunate name.
4.  **The container-escape test and the case table.**
    `engineering-plan.md:3215` requires a test the harness's case set
    does not contain. Belongs to the sandbox specification and the
    harness together. Resolved by both: the case set gains a
    twenty-sixth row, a Milestone 6 security case backed by
    `gate.sandbox.escape_denied`, and none of the twenty-five is
    renumbered. The same shape repeated four more times afterwards,
    for cases 28 through 31, and the rule held each time: the case set
    grows at the end and Section 20's own numbering never moves.

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
    documents get written. Answered by writing it early. The
    expansion found the egress allowlist had no grammar and no
    owner, that eight types the corpus referenced were never
    declared, and that `builtin-tools.md` had the tool at the wrong
    milestone — none of which would have surfaced by building
    Milestone 5 first.
3.  **Should Milestone 6 acquire gates before it is built?** The map
    reported the zero rather than inventing entries, which was right.
    But a sandbox with no registered invariant is a different kind of
    zero from a Milestone 8 with no registered invariant, and the
    sandbox specification is the natural place to fix it. It did:
    thirteen gates in a new `sandbox` area, eleven of them at
    Milestone 6. Milestones 8 and 10 kept theirs until
    [skills.md](skills.md), which closed both the same way and for
    the same reason: there is something designed there now to assert
    about.
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
