---
title: Readiness
status: design
canonical: true
---

# Readiness

This document answers one question: can an implementer open this
corpus and start writing code, and if so, how far can they get before
the corpus runs out?

The original answer was that Milestones 0 through 5 were implementable from the
documents alone and Milestones 6 through 10 were not. The historical findings
below retain that answer because they explain why the missing specifications
were written. The present answer is different: Milestones 0 through 11 are
complete, and Milestone 12 notifications and device identity is specified and
in progress.

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
subsection heading under Milestones 0 through 11 in
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
    `tool-system.md:1980` states that device tools are *"a reserved
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
| 7 | Context budgeting and working state | Ready | 7 | Nothing |
| 8 | Skills and MCP integration | Ready | 17 | Nothing |
| 9 | Long-term memory and knowledge | Ready | 26 | Nothing |
| 10 | Memory maturation, self-authored skills, web access, browser automation | Complete | 38 | Tenant activation remains roadmap item B1 (ADR-0061) |
| 11 | Scheduled runs | Complete | 23 | Nothing |
| 12 | Notifications and device identity | In progress | 20 | The Apple push key and capability are owner actions outside the corpus |
| 13 | General-purpose subagents and delegation | Authorized | 21 | Nothing in the corpus; tenant activation needs the owner's failed trajectory scored against the delegating re-run |
| 14 | Inbound surfaces and pairing | Authorized | 21 | Nothing in the corpus; the Telegram bot and its private token file are owner actions outside it |
| 15 | Operational hardening | Authorized | 16 | Nothing in the corpus; the bucket, the `age` identity, the first escrow, and the first off-host rehearsal are owner actions outside it |

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
seven that bring it to seventeen came later in two passes — four from
the one that noticed build step 9 of the tool system had nothing
observing it, and they are the reason the mock-server gap this table
first named is gone, then three more with the authentication design
that closed the other half of the same milestone.
Its six at Milestone 10 were a different case when first measured: they were
the authoring loop's gates, registered against a milestone whose own acceptance
criteria did not yet exist. The later authorization added five automatic-memory
gates and an explicit completion contract. Milestone 11 follows the same
discipline with twenty-three scheduling gates arriving with its detailed
design, including explicit contract, migration, erasure, and isolation gates.
Milestone 3's eleven became fifteen the same way: two from
[model-gateway.md](model-gateway.md) and two from
[event-log-and-persistence.md](event-log-and-persistence.md), each
pair arriving with the design that closed one of this section's named
gaps rather than being added to make a count look better. Milestone
9's fourteen became twenty-six on the same principle and by the
largest margin, when the knowledge half of that milestone acquired a
design to be true of.

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

**Forty-one gates are green before
Milestone 2 begins**, thirteen of them against a repository with no
agent in it. The current registry total is derived in the
[milestone map](milestone-map.md), not restated here.
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
    `engineering-plan.md:1636` says *"Create Alembic migrations for at
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
(Section 6.5)"* in Milestone 2 at `engineering-plan.md:2518`, while
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
    a `dict[str, Any]` field at `engineering-plan.md:1222`. No document
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
only at `engineering-plan.md:476`, the policy spec identified where
scopes are checked, and nothing stated the scope vocabulary, its
grammar, or the comparison algorithm — whether a scope was an opaque
string, a hierarchy, or a pattern. Relatedly,
`bootstrap-and-composition.md:577` named `ApprovalService` as one of
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
in one section. The vocabulary is one closed set of fifteen strings
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
declared scope is legal if it is one of the fifteen or its first
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
`engineering-plan.md:1810-2015`, designs the API more thoroughly than
a summary of this milestone's coverage would suggest. At the time of the
Milestone 5 review it specified nine endpoints with methods, paths, and where
relevant headers; ADR-0050 later added the authoritative session list and
delete contracts without changing that milestone's gate census. The section
also specifies
request and response JSON for session creation, message submission,
and approval resolution; the SSE frame format with `id`, `event`, and
`data` lines; the reconnect rule that replays persisted events after
`Last-Event-ID` and then continues streaming; the five cooperative
cancellation observation points; the error envelope; and the
readiness constraint that a probe must not call a provider.

What did not exist was any expansion of that section. No
detailed-design specification covered the API layer. The only HTTP
routes designed outside the plan were three: the two approvals reads
at `policy-and-approvals.md:1030-1031` and the resolve at
`policy-and-approvals.md:1040`, and one reference in
`runtime-loop.md:1182` to `POST /runs/{id}/input` that routed to an
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
    `engineering-plan.md:1903` and as an implement bullet, and the
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

Section 28 of the plan is not empty — `engineering-plan.md:3591-3668`
states a six-item threat model that assumes model-generated code is
hostile, and is recorded as ADR-0008. But it was not expanded, and
two specifications pointed at the expansion as though it already
existed. `tool-system.md:1010` constrains MCP server URLs by *"the
egress allowlist the sandbox spec establishes"*, and there was no
sandbox spec.
`bootstrap-and-composition.md:205` and `:183` assign ownership of
`ArtifactStore` and `ExecutionEnvironment` to the engineering plan
itself, which is the corpus recording that nothing below the plan owns
them.

Two bullets are covered. Output truncation and artifactization are
specified at `tool-system.md:724`, and the programmatic orchestration
bridge Section 8.5 requires is specified from `tool-system.md:1374`.

Two further items deserved naming.

1.  **The plan demands a red-team test with no case behind it.**
    `engineering-plan.md:3666` requires a container-escape attempt as
    a security test. The twenty-five-case table contains no such case
    and no Milestone 6 security row.
2.  **`sandbox.run_command` was placed at two milestones.**
    `builtin-tools.md` said Milestone 5 where `builtin-tools.md:1473`
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
remaining. The egress allowlist `tool-system.md:1010` depends on by
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

## Milestone 7: ready

Seven registry entries and the best per-bullet coverage of any milestone
in the second half. [context-engine.md](context-engine.md) covers
seven of eight implement bullets fully: the token budgeter, the
compaction boundary, the structured working set, the deterministic
assembly and `prefix_sha256`, the two regions, trust labeling in
assembled context, and the recall trace as a second consumer.

Two shortfalls.

1.  **History selection had an order but no predicate.** The yield
    order and the floor were specified. What decided that a given turn
    was in or out was not, which is the part that determines whether
    the result is stable across two runs with the same input.
2.  **Long-session evaluation has a gate and no case.** The
    twenty-five-case table carries milestones 1, 2, 4, 5, and 6 and
    nothing else. There is no Milestone 7 row, so three of this
    milestone's acceptance criteria have no case backing them. The
    harness specifies how to add cases and the gate exists; what does
    not exist is the case.

**What changed.** Shortfall 2 was closed first.
[evaluation-harness.md](evaluation-harness.md) case 28 gives this
milestone its first row: the fifty-turn session with one distinct
`prefix_sha256`, which is `gate.context.prefix_stability` given
something to run.

### What closed it

Shortfall 1 was closed on a later pass, and it turned out to be two
questions wearing one name. Which items are in the request is decided
twice — once when a run seeds from the session and once on every
assembly — and the two decisions read different inputs, so a single
predicate could not have covered both.

[context-engine.md](context-engine.md) answers the seeding half with a
cut rather than a rule about content. The session-history projection
is a live read model that advances on a timer, so reading it for "the
session's history" returns whatever has been applied at the moment of
the call. That matters more than it looks: `seed_checkpoint` has two
call sites, the second being the rebuild the Milestone 2
dispensability gate forces, and the two can be hours apart. The seed
now reads the log below `runs.seed_event_sequence`, the session
sequence of the `user.message.created` event the run answers, written
in the transaction that already allocates it. Projections were already
required to be deterministic over a log prefix; pinning which prefix
is what converts that into a statement about seeding, and it needs no
gate of its own, because the dispensability gate is the test and it
only tested anything once the cut was fixed.

The assembly half is answered by making the retained set a contiguous
suffix — one cut index, computed by a backward scan against
`history_tokens`, floored at the compaction boundary, moved later past
any tool pair it would split, and taken after the never-yield items
have been subtracted. `select_history` returns the index rather than
the list, so contiguity is carried by the return type instead of by a
test someone has to remember to write, and `TokenEstimator.estimate`
picks up a purity requirement it was missing: approximate was always
allowed, but an estimator that answers differently on two identical
calls moves the cut, which is the whole failure.

The argument against the obvious alternative is the part worth
keeping. A relevance ranking over past turns would be a second
retrieval system beside the one the corpus already has, and it would
make a missing turn ambiguous between a selection defect and a ranking
miss. History is recency and in-turn recall is relevance; the two stay
separate so that either can be tested.

One gate, at Milestone 7: `gate.context.history_cut`, a property gate
for the same reason gate 1 is. Both named shortfalls are closed, so
the verdict changes with them.

## Milestone 8: ready

Seventeen registry entries: ten from [skills.md](skills.md), which
was written after this review and closes the half that was missing,
and seven added later still for the MCP half. The verdict below is
the review as it stood, followed by what changed.

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
had to be written. `tool-system.md:1306-1353` draws the line between
a skill and a tool, fixes the metadata block at four fields, puts
`required_tools` checking at load rather than at authoring, assigns
trust by author, and classifies `skill_manage`. That is real design.
What was missing was everything underneath it: no package format, no
manifest schema, no types, no storage, no reference grammar, no
context accounting, and no gates. The acceptance criterion *"A
selected skill is version-pinned in the run"* at
`engineering-plan.md:2764` had no design behind it — and no document
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

One named gap survived, and it was the smaller one: authentication
configuration still had a `credential_ref` column, no auth scheme,
and no refresh or re-auth path. It was a Milestone 8 implementation
question rather than a design hole in the corpus, because the broker
that resolves the reference is specified and only the scheme it
resolves was not.

### What closed it

The gap was a column with no counterpart. `credential_ref` says where
a secret is; nothing said what the reference resolves to, and the
broker cannot infer it, because a bearer token and an OAuth client
secret are both opaque strings and a resolver that guesses between
them eventually presents a client secret as a bearer token to a server
that logs its `Authorization` headers.

[tool-system.md](tool-system.md) closes it by making the scheme
configuration rather than inference: a closed set of five — `none`,
`bearer`, `header`, `oauth2_client`, and `env` — declared in the
`mcp_servers` row beside the reference, with `auth_name`,
`token_endpoint`, and `token_scopes` for the schemes that need them.
The scheme lives in the row rather than inside the secret for three
reasons that each stand alone: validating configuration would
otherwise require dereferencing a secret, secrets rotate and protocols
do not, and the scheme appears in operator-facing errors and in
`mcp.server.disconnected`, which is a place the emitter is forbidden
to look for secrets.

Because the scheme is a column it is validated when the row is written
rather than when the server is dialled, which turns most of this into
a configuration error a human sees before anything connects: a scheme
outside the five, `header` naming `Authorization`, `env` naming a
tier-0 variable, an `oauth2_client` token endpoint the egress
allowlist does not permit, and the transport cross-check that rejects
`env` over HTTP and headers over a pipe.

The refresh half is a bounded ladder that routes through the recovery
table already in the spec rather than around it: one re-authentication
per server per session, one retry and only where recovery permits,
`UNCERTAIN` rather than a retry for a non-idempotent call whose
watermark is set — a 401 arriving after `mark_effect_sent` says
nothing about whether the effect landed — and `unavailable` with
`tool.server_unauthorized` thereafter. Expiry is checked when a header
is built rather than on a timer, because a background refresh is a
second clock that keeps tokens alive for idle connections and removes
none of the 401 path anyway.

The `env` scheme dragged in the sharper question, which is what else
is in a stdio server's child environment. It is constructed — the
synthesized sandbox tier plus the one declared variable — rather than
inherited, and it is a gate rather than a paragraph because
inheritance is the default behaviour of every process-spawning API in
the standard library, and what would be inherited is the worker's
database URL and every provider key.

What is deferred is said out loud. The user-delegated flows need a
browser redirect, a callback URL, and a per-principal token store, and
`conversation.ask_user` suspends a run for text, not for a redirect. A
server that requires one fails to connect with `tool.auth_unsupported`,
which is a refusal a reader can find rather than a connection that
hangs.

Three gates, all Milestone 8, dividing by what each needs in order to
run: `gate.tool.mcp_auth_config` over the validator with no server and
no broker, `gate.tool.mcp_reauth_bounded` against a server that
returns 401 on demand, and `gate.tool.mcp_stdio_env_built` against a
child process whose environment can be read back. The named gap is
closed, so the verdict changes with it.

## Milestone 9: ready

Twenty-six registry entries, the second-largest count, and the three
specifications behind them are among the most complete documents here.
[memory-formation-and-consolidation.md](memory-formation-and-consolidation.md)
covers the autonomous formation loop, the tiers, the provisional tier
and its promotion rules, contradiction handling, and cross-project
belief carry-forward.
[memory-retrieval-and-ranking.md](memory-retrieval-and-ranking.md)
covers retrieval, ranking, the snapshot budget, and the recall trace
with its two consumers.
[knowledge-documents.md](knowledge-documents.md) covers the other half
of the milestone's name.

Several items remain partial.

The remaining partials are: session history and artifacts as retrieval sources
(named as sources, not designed as such); expiration (a policy with no sweep
job in the corpus reviewed here); the external memory provider port (named, no
contract); and the persona and identity surface over
`AgentSpec.instructions` (no statement of how a formed belief reaches the
instruction text, or whether it may).

The human-editable surface gap identified by the readiness review is now
closed through the CLI decision in ADR-0045. `agent memory list`, `get`, `edit`,
and `delete` call the governed service and expose full provenance and lifecycle
metadata; listing can include inactive beliefs and filter by source session.
`agent memory formations` exposes consolidation policy, watermark, and outcome
counts, while `agent memory trace` exposes the authenticated principal's
persisted retrieval diagnostics. Boundary and repository tests cover the
commands, tenant/principal isolation, ordering, and session filters. The HTTP
route set remains unchanged.

One item was absent, and it was the larger one. **Knowledge documents
had no design.** The milestone's own heading is *"Long-term memory and
knowledge retrieval"*, the separate-stores subsection distinguished
memory from knowledge, and no document stated what a knowledge
document is, how one is ingested, chunked, indexed, or scoped, or how
retrieval over it differs from retrieval over memory. The memory specs
were explicit that they cover memory; nothing covered the other half
of the milestone's name.

### What closed it

The gap was a name with no referent. Milestone 9 promised knowledge
retrieval, Section 18.4 said knowledge is not memory, and the
corollary — that a store which is not memory needs a document model,
an ingestion path, an index, a scope predicate, and a retrieval path
of its own — was never drawn. What made this the larger of the two
gaps is that a reader could not tell whether knowledge was a missing
document or a missing decision.

[knowledge-documents.md](knowledge-documents.md) and ADR-0033 draw the
corollary. A knowledge document is a thing a principal admitted so
that later runs can quote passages from it verbatim and cite them,
which is a different answer to a different question than a belief: a
belief answers *what is true*, and the unit of retrieval is the claim;
a document answers *what does the source say*, and the unit of
retrieval is the passage. That distinction is what forced a fourteenth
specification rather than a section in either memory document, both of
whose scope lines say beliefs and episodes. The trajectory-export
precedent, which rejected a fourteenth spec once already, applies a
test rather than a quota — does the new document own what it needs, or
borrow it — and knowledge owns ten things and borrows four, where
trajectory export borrowed almost everything.

The design decisions that carry the most weight elsewhere in the
corpus are four. Ingestion is a tool, `knowledge.ingest`, rather than
a route or a CLI noun, because the Milestone 5 API baseline was closed at
fourteen routes and an artifact is not uploaded through it in 0.1; ADR-0050's
later list and delete routes do not add an upload surface. Ingestion requires `USER`
origin trust, so an agent cannot admit what it fetched. The secret
scan blocks an ingest and the injection scan does not — a credential
in a permanent corpus is unrecoverable, while instruction-like text is
survivable by labelling, which is what `instruction_like` on the chunk
and `TrustLevel.KNOWLEDGE` on the rendered block are for. Chunking is
deterministic under a `chunker_version` and carries no overlap,
because the chunk id *is* the citation and heading paths give back the
context overlap was buying. And `visibility ∈ {principal, project,
tenant}` replaces `principal_id` as the isolation predicate, which is
the exact inverse of the carry-by-default rule the memory layer took
for beliefs — a document is shared unless it is scoped, a belief
travels unless it is pinned.

Twelve gates in a new fourteenth area, `knowledge`, all at Milestone 9:
eight cases, three property gates over chunk stability, verbatim
extraction, and citation resolution, and one corpus gate with a floor
on passage recall and a ceiling on noise. They take Milestone 9 from
fourteen registry entries to twenty-six, and the corpus from one
hundred and sixty to one hundred and seventy-two. The named gap is
closed, so the verdict changes with it.

## Milestone 10: complete; activation evidence remains separate

The project authorized automatic memory formation, the independently
deliverable self-authored-skills tranche, provider-neutral public-web access,
and authenticated browser automation. The six registered `gate.skill.*`
entries and fifteen automatic-memory, inspection, and provider gates form the
first two delivery contracts. Routing remains a deferred direction on the
plan's roadmap; general-purpose subagents were authorized as Milestone 13 on
2026-08-20. Scheduling moved to the separately authorized Milestone 11 on
2026-08-19.

The readiness review originally found this milestone structurally unlike every
other one in the plan. The repository owner explicitly authorized it on
2026-08-17 and selected memory maturation as the first workstream. Thirty-eight
registry entries belong to Milestone 10: six for skill authoring, fifteen for
automatic memory formation, inspection, and governed provider assistance, seven for
public-web access, and ten for authenticated browser automation. Authorization
permitted implementation; the passing delivery evidence now makes it complete.

When this review was written, it had no `#### Implement` heading and no
`#### Acceptance criteria` heading. It opened with *"These are separate optional
extensions"* and divided into scheduling, a second model provider with routing,
subagents, and a `#### Gate for multi-agent work` — which is an entry
condition stating when subagents may be built, not a statement of what
is true once they are. Milestone 9 also lacks an `#### Implement`
heading but does have acceptance criteria. The 2026-08-17 authorization adds a
memory-maturation implement subsection and five criteria; the other extensions
retain the structure assessed here.

At the time of the original review, all seven scheduling requirements were
undesigned. [scheduling.md](scheduling.md) and ADR-0059 supersede that verdict
without rewriting the historical finding. Of six routing
considerations, data residency and evaluation performance have no
design; the other four are at least touched by the model gateway's
routing section. Of nine subagent requirements, five had none when
this section was written — the explicit objective, restricted
context, child deadline, separate trace, and artifact references
rather than a full transcript — and two more were partial.

That subagent count is now stale, and it is the only verdict in this
review that later documents overtook. Re-measured against the corpus
as it stands, five of the nine are supplied. `parent_run_id` is a
Section 15 column at `engineering-plan.md:1691`, and the sibling join
at `runtime-loop.md:1140` reads it. Restricted context is
`context-engine.md:282`, where `runs.seed_event_sequence` is nullable
for child runs because they *"seed from a parent's concise
instruction rather than from session history"*, together with the
child-run recall class at `memory-retrieval-and-ranking.md:87`, which
gets fifteen beliefs against an interactive run's forty. The
restricted tool set is `tool-system.md:977`: *"the registry resolves
the child's set through `specs_for_session` with the child's
principal, not the parent's"*. The child deadline is
`runtime-loop.md:1147`: *"the parent's `deadline_at` is copied onto
every child at creation"*. The concise return is the sibling join
plus the `EXTERNAL_UNTRUSTED` label the returned result carries at
`tool-system.md:973`. Two are partial: the explicit objective has a
carrier but no schema, since `delegate.run` is a control tool at
`tool-system.md:931` and no input type for it exists anywhere, and
the child budget is additive by `engineering-plan.md:570` while no
rule derives a child's own `limits`. Two still have none — the
separate trace and the artifact references, stated at
`engineering-plan.md:3575` and `engineering-plan.md:2967` and picked
up by no specification.

Re-measuring surfaced a conflict the stale count was hiding.
`event-log-and-persistence.md:782` declares a unique index on
`session_id` where status is not one of `COMPLETED`, `FAILED`, or
`CANCELLED`, to enforce Section 27.5's one active run per session. A
parent suspended on a child waits in `WAITING_FOR_APPROVAL` carrying
a typed suspension kind, which `runtime-loop.md:290` chose over an
eighth state, and that is not one of the three — so a child run in
the parent's own session cannot be inserted. Section 27.6 offers both
placements, *"the parent's session or a dedicated child session per
policy"*, and only the second survives the index, with no policy
written to choose between them. It is recorded here rather than
resolved: resolving it is Milestone 10 work, and this review
authorizes none.

The historical verdict was that this was a direction rather than a milestone.
The authorization and new memory-maturation acceptance criteria changed that
operational verdict. All thirty-eight Milestone 10 gates, the cumulative
registry, hosted CI, and the final review passed on final head `90e9142`, so the
milestone is complete. Skill-authoring activation is roadmap item B1 rather
than a completion condition (ADR-0061); routing remains deferred; subagents are
Milestone 13; scheduling is outside this milestone.

The owner separately authorized provider-neutral public-web access on
2026-08-18. [web-access.md](web-access.md) now covers its port, two tools,
capability-level provider selection, fixed egress targets, credential handling,
trust labels, bounds, failure vocabulary, and acceptance criteria. Seven formal
`gate.web.*` checks now cover that contract, taking the registry to 190 entries
without changing the Milestone 9 verified gate ceiling.

The owner separately authorized provider-neutral authenticated browser
automation on 2026-08-19. [browser-automation.md](browser-automation.md) covers
its threat model, provider port, read/write tool split, profile and login
lifecycle, isolation, origin confinement, standing grants, uncertainty rules,
scheduler handoff, delivery slices, and acceptance criteria. Ten formal
`gate.browser.*` checks now cover that contract, taking the registry to 204
entries without changing the Milestone 9 verified ceiling. All ten resolve to
executable profile-lifecycle, authentication, grant, provider, policy, trust,
revision, origin, and uncertainty checks.
The later Milestone 11 scheduling design raises the complete registry to 227
and, after final verification, advances the verified gate ceiling through 11.

Open question 4 below closes the remaining half of this, which was
whether the missing criteria are an omission or a choice. They are a
choice. Each of the four parts carries an entry condition and a
must-have list already, and two of those conditions gate on evidence
rather than on a date: a scheduler comes *"only after durable
on-demand runs are reliable"*, and subagents come *"only when
evaluation evidence shows that a single agent fails"*. Acceptance
criteria are a promise about a delivery, and a promise cannot be made
about work that must not start until evidence arrives. What Milestone
10 is missing is the heading, not the content the heading would hold.

## Milestone 11: scheduled runs complete

The scheduling entry condition is now true: Milestone 2's durable worker and
queue, Milestone 4's policy and approvals, and Milestone 5's authenticated HTTP
surface are complete. The owner authorized the work as Milestone 11 on
2026-08-19 rather than changing Milestone 10's established completion contract.

[scheduling.md](scheduling.md) closes every gap the original review named. It
defines a versioned schedule and immutable occurrence, the recurrence and
civil-time algorithm, the four-table schema, the atomic materialization
transaction, a current-principal directory, exact schedule scopes, lifecycle
and cancellation semantics, bounded misfires, no-overlap behavior, dedicated
sessions, async priority and admission, the eight-route API, default-off
deployment requirements, metrics, and twenty-three hard gates. ADR-0059 records why
those mechanisms reuse PostgreSQL and the ordinary run path.

The readiness verdict is therefore **Complete**. There is no unnamed design
choice in the delivered contract. All twenty-three scheduling gates and the
227-gate cumulative registry passed, and hosted CI plus the final review passed
on final head `90e9142`. Production scheduling remains default-off until
explicitly activated.

## Milestone 12: notifications and device identity, authorized and specified

[notifications-and-devices.md](notifications-and-devices.md) closes the half
of Section 29 it was written for. It defines the `Device` registry with a
client-minted installation identity, per-device muted kinds, and a live-token
uniqueness rule; the durable outbox written in the triggering transaction
through a savepoint-wrapped hook on the single terminal writer and the
scheduling accountant and materializer; the closed five-trigger catalog; the
content-free payload; claim-lease dispatch with staleness checks, a closed
retry schedule, and token invalidation; the APNs adapter behind a
`PushTransport` port a fake satisfies; the `notify` role and its credential
confinement; three scopes and seven routes including the offline inbox; the
Apple client's registration and deep-link duties; process-event lifecycle
audit; and twenty hard gates across the `device` and `notify` areas. ADR-0062
records why two ports replace the `NotificationService` name, why the
broadcaster stays, why registration replay has a separate principal-scoped
repository, and why the push key lives in one role.

The readiness verdict is therefore **In progress**: there is no unnamed design
choice between the corpus and the implementation. What remains outside the
corpus is the owner's Apple Developer work — an APNs key and the push capability
on the bundle identifier. The verified ceiling is 11 while Milestone 12's gates
are implemented.

## Milestone 13: subagents and delegation, authorized and specified

[subagents-and-delegation.md](subagents-and-delegation.md) supplies the four
items this review measured as partial or absent under Milestone 10 and
resolves the conflict it recorded: the structured brief as the objective's
schema; the rule deriving a child's limits from the parent's remainder with
cost reserved at materialization; the `delegations` ledger as the carrier for
the separate trace and the artifact references; and a dedicated child session
always, so the one-active-run index is neither widened nor contended. The
rest was already decided — the control-tool contract, the `CHILD_RUN`
suspension in place of an eighth state, the post-terminal join, the untrusted
result label, the brief-seeded child context — and the document cites each
decision rather than restating it. It honours the gate for multi-agent work as
written: construction is authorized, activation requires a capability scenario
admitted from a real failed trajectory and a two-arm case 32, and twenty-one
hard gates in the `delegate` area cover the rest. ADR-0063 records the
decisions.

The readiness verdict is therefore **Authorized**: there is no unnamed design
choice between the corpus and the first red tests. What remains outside the
corpus is the owner's failed trajectory for the capability scenario, and the
verified ceiling, which advances in order.

## Milestone 14: inbound surfaces and pairing, authorized and specified

[inbound-surfaces.md](inbound-surfaces.md) gives pairing its home and
endpoint, builds the session-key resolver the seam audit called the one
genuinely new mechanism in Section 29, and attributes the origin on the write.
A Surface is a Milestone 12 device with an empty capability set; a
least-privilege `surface` role polls Telegram and holds the bot token and
nothing else; an unknown sender is rejected before any content is stored; a
paired sender's message becomes an ordinary run through the submission
function the HTTP API uses, as a `USER` message for the bound principal with
scopes intersected fresh; notifications travel the Milestone 12 outbox and
replies a separate surface-reply outbox back to the chat. The document states the trust rule the
audit left open and bounds it to the owner's own pairing, leaving a
third-party label to the roadmap. Twenty-one hard gates in the `surface`
area; ADR-0064 records the decisions.

The readiness verdict is therefore **Authorized**: there is no unnamed design
choice between the corpus and the first red tests. What remains outside the
corpus is the owner's bot and its private token file, and Milestone 12
landing first.

## Milestone 15: operational hardening, authorized and specified

[operational-hardening.md](operational-hardening.md) turns the deployment
page's accepted "unrecoverable" into recoverable within a backup window: a
declared, encrypted, off-host daily backup of the database, the artifact
store, and the browser-profile ciphertext with a manifest and a manual secret
escrow; a restore rehearsal with a five-part verdict run on the host, in CI,
and by the owner from the off-host copy; a host health check on a closed
signal list delivering deduplicated alerts through the Milestone 12 outbox
with a dead-man's switch and an external uptime check for the states that take
the outbox down; cloud and host firewall, SSH hardening, proxy rate limits, and
a loopback-only structural gate; a code-only rollback that refuses to cross a
schema boundary without an override and never downgrades; and systemd
watchdogs for the worker roles. Sixteen hard gates in the `ops` area; ADR-0065
records the decisions and amends ADR-0046.

The readiness verdict is therefore **Authorized**: there is no unnamed design
choice between the corpus and the first red tests. What remains outside the
corpus is the owner's bucket and scoped key, the `age` identity, the first
escrow, and the first off-host rehearsal.

## The three plan sections no specification expanded

Sections 29 through 31 were the only major sections of the
engineering plan with no outward cross-reference paragraph. A scan of
`engineering-plan.md:3670-3828` for links to other documents returned
nothing when this review was written, where every other major section
acquired one during the specification work. Two of the three were
genuinely unexpanded; the third was half-expanded from the consuming
side. All three carry the paragraph now, and what follows is the
finding in each case and what closed it.

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

`tool-system.md:1457` does open a *"Device-scoped tools"* section, and
`tool-system.md:1980` states that device tools are *"a reserved seam,
not a design"*. That is an explicit deferral rather than an oversight,
and it is the right call for a Milestone 10-adjacent concern. What it
left behind was a model with no home.

[multi-device-and-surfaces.md](multi-device-and-surfaces.md) and
ADR-0034 close it, and they close it as an audit rather than a design,
because Section 29.8 defers its own subject and a specification that
wrote the four contracts would be building the deferred thing. What
an audit produces instead is the answer to the question a deferral
leaves hanging: whether the `Device` lands additively when it lands.
Eight places in the corpus already hold a device-shaped hole and need
no edit — the `DEVICE` tool source, forced untrusted output at
registration and its enforcement in the composition root, the
reserved `device.` domain, `ExecutionTarget.kind` with `device_id`
beside it, the single authorization gate that already names the
device channel, `tool.device_offline` as a row of the availability
table rather than an exception to it, and idempotent approval
resolution that cites Section 29 by name. Five do not, and naming
them is the point of the exercise: attach is a third registration
source and not at session open; device lifecycle events have no
session to be charged to against a `NOT NULL` column; a capability
hand-off is a fourth suspension kind against a closed two-value
vocabulary; no client is attributed on a write, which matters for
inbound content rather than for tool output; and
`NotificationService` is a port name appearing twice in the corpus
with no mechanism, transport, or durability behind it, which
`LISTEN`/`NOTIFY` does not supply and was never meant to.

The strongest result is the one that costs nothing. Section 29.4's
per-device scopes read like a second evaluation path and are not one.
The scope set is captured at submission and stamped on the run, and
`PrincipalResolver.for_run` reads that stamp and never a table, so
narrowing is an intersection computed once at submission and the
policy engine is never told that a device exists. The constraint
travelling with it is that a `device.` scope prefix would mean
rewriting the fifteen-string grammar and the gate that asserts it,
and the `device.` that already exists is a tool-name domain rather
than a scope prefix. Three conflicts between Section 29 and later
specifications are named and resolved in the specifications' favour,
one of them the question `tool-system.md:1471` reserved by name —
whether a device tool may be advertised in a session opened while the
device was absent — which resolves against the pinned prefix on the
same precedent that governs an MCP catalog change mid-session. None
of it is 0.1 work, and 29.8's own scope paragraph is already
satisfied: reads and writes are principal-scoped and served from the
core, and a second client attaching and replaying is
`gate.api.replay_exact` at Milestone 5.

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
`engineering-plan.md:2764`, and the line this review first named was
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

That leaves none. Every part of Sections 29 through 31 is expanded
by a specification now. The last of the three is expanded as an audit
rather than a design, which is the only shape available for a section
whose own scope paragraph defers it, and the audit is what turns
"defer this" into a list of what deferring it will cost.

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
    HTTP API. `builtin-tools.md:1473` now says Milestone 6.
2.  **Usage token classes and cost-source precedence at Milestone 2 or
    Milestone 3.** `engineering-plan.md:2518` against
    `model-gateway.md:1795` and `milestone-map.md:1248`. The map
    follows the gateway. Nothing is built differently either way; only
    the migration's timing changes.
3.  **`Idempotency-Key` and the idempotency port.** Named as an HTTP
    header at Milestone 5 and as a tool-call port at Milestone 1.
    Whether these are one mechanism or two is undecided, and belongs
    to the API specification. Resolved there as two: two scopes, two
    milestones, a table and a column, one unfortunate name.
4.  **The container-escape test and the case table.**
    `engineering-plan.md:3666` requires a test the harness's case set
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
    come first: `tool-system.md:1010` already depends on an egress
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
    an open direction?** The original review answered “open direction” because
    it was the only milestone with neither an implement list nor acceptance
    criteria. The owner authorization on 2026-08-17 supersedes that operational
    answer for memory maturation and adds five criteria without converting the
    evidence gates for the other extensions into calendar promises. The re-measure that produced the
    answer also corrected this review: five of the nine subagent
    requirements are designed now, by documents that landed after the
    verdict above was written.
5.  **Is knowledge retrieval a Milestone 9 deliverable or its own
    milestone?** Answered by designing it: a Milestone 9 deliverable,
    sequenced after the memory half. Splitting it out would have made
    Milestone 9 shippable against what was actually designed, and that
    was the argument for it. The argument against won on a
    dependency: knowledge retrieval reuses the fusion, the ranking
    shape, the budget mechanics, and the trace record that
    [memory-retrieval-and-ranking.md](memory-retrieval-and-ranking.md)
    establishes, so a separate milestone would have been a milestone
    that cannot start until the previous one finishes — which is what
    a build step inside a milestone already is. Twelve gates now sit
    where the split would have been.
