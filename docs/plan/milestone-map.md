---
title: Milestone Map
status: design
canonical: true
---

# Milestone map

The gate registry makes `milestone` a required field on every gate. Ten
specifications declared eighty-nine gates between them when this
document was written. One of the ten supplied that field per gate, four
supplied it once for a whole section, and five did not supply it at
all.

That is not a formatting complaint. Registry rule 1 says a docs check
parses the hard-gate section of each spec, counts the declared gates,
and compares against the registry — and that check is a Milestone 0
deliverable, which means it has to run before there is an agent to
evaluate. Today it cannot be written. The gate sections carry three
different headings, two different declaration forms, and in two specs
the gates are interleaved with tracked metrics in a single bullet list
whose gate-ness is stated mid-sentence.

This document supplies the missing field for all eighty-nine, fixes the
declaration form so the check is writable, and finishes the milestone
assignment for the five build sequences that carry no tags. It also
closes two smaller reconciliations that turned up in the same sweep:
what the tool system's persistence build step means inside an in-memory
milestone, and what cancels a run at Milestone 1.

[http-api-and-streaming.md](http-api-and-streaming.md) was written after
this document and declares ten more gates, in the form fixed here and
with a milestone on each — which is what fixing the form was for. Its
gates appear in the table and the census below and needed no
reconciliation.
[sandbox-isolation.md](sandbox-isolation.md) was written later still
and declares thirteen more, in the same form and in a new twelfth
area. [skills.md](skills.md) followed and declares sixteen
more, in a new thirteenth area, and is the first spec to declare
gates at Milestone 10. [model-gateway.md](model-gateway.md) and
[event-log-and-persistence.md](event-log-and-persistence.md) each
gained two more on a later pass, all four at Milestone 3.
[knowledge-documents.md](knowledge-documents.md) declares twelve more, in a new
fourteenth area, all of them at Milestone 9. [web-access.md](web-access.md)
declares seven more in a fifteenth area, all at Milestone 10.
[browser-automation.md](browser-automation.md) adds ten more in a
sixteenth area, also at Milestone 10. [scheduling.md](scheduling.md) first
declared twenty-three Milestone 11 gates in a seventeenth area, then five at
Milestone 19 and six at Milestone 20.
[adaptive-memory-distillation.md](adaptive-memory-distillation.md) adds
twenty-four Milestone 21 gates in the existing memory area. The current counts
are reconciled in the gate table and census below.

## What this document is responsible for

It is a scheduling document. It decides *when* each stated requirement
must hold, and it changes no requirement's content.

1.  **In scope.** The milestone of every declared hard gate; the
    identifier each gate carries in the registry; the declaration form
    the docs check parses; the milestone of every build-sequence step
    in every spec that left them untagged; the three gates that are
    declared in two specs at once and therefore need one owner.
2.  **In scope, by consequence.** Two reconciliations that only became
    answerable once the gate milestones were fixed: tool-system build
    steps 3 and 4 against the prohibition on early PostgreSQL, and the
    Milestone 1 half of the cancellation split.
3.  **Not in scope.** No gate belonging to another document is added,
    removed, weakened, or strengthened. Where this document splits one
    stated gate into two registry entries it is because the spec says
    "both are hard gates", and where it moves a bullet out of a gate
    list it is because the spec calls that bullet a metric. The seven
    gates under [Hard gates](#hard-gates) are this document's own, and
    every one of them checks the corpus rather than the agent.
4.  **Not in scope.** The contents of `evals/gates/*.yaml`. This
    document is what those files are generated against and checked
    against; it is not those files.

## The three problems

### Headings

The gate section is spelled three ways across the corpus.

```text
heading                            specs
---------------------------------  -----------------------------
## Hard gates                      builtin-tools, evaluation-
                                   harness, model-gateway,
                                   runtime-loop, tool-system
## Evaluation                      context-engine, event-log-and-
                                   persistence, memory-retrieval-
                                   and-ranking, policy-and-
                                   approvals
## Evaluation (gates the           memory-formation-and-
milestone)                         consolidation
```

Two specs, `bootstrap-and-composition.md` and
`development-toolchain.md`, declare no gates at all. That is correct
and stays correct: both describe construction rather than behaviour,
and the four static checks bootstrap adds are declared as Milestone 0
consequences rather than as gates of their own. The secret scanner
`bootstrap-and-composition.md` specifies is a gate, but the engineering
plan declares it; bootstrap supplies the mechanism, not the
requirement.

### Forms

Six specs declare gates as a numbered list. Four declare them as a
bullet list. The registry example's `spec` field points at
`policy-and-approvals.md#evaluation`, so the anchor a gate cites is
already the `## Evaluation` spelling in at least one place, which is
why the fix below renames headings rather than leaving the check to
know three of them.

### Metrics mixed into gate lists

`memory-formation-and-consolidation.md` and
`memory-retrieval-and-ranking.md` list gates and tracked metrics in one
list, distinguishing them in prose: *"A hard gate"*, *"The primary
metric"*, *"A hard gate, not a metric to improve"*, *"Both are hard
gates, not metrics to improve"*. A count over those lists is not
defined until they are separated, and separating them is reading what
is written rather than deciding anything.

## The declaration form

One heading, one form, one suffix.

1.  **Every spec that declares gates spells the section `## Hard
    gates`.** Four `## Evaluation` sections and one `## Evaluation
    (gates the milestone)` are renamed, and where such a section also
    carried tracked metrics those move to a sibling `## Tracked
    metrics` section immediately after. No sentence stating a
    requirement changes; the only additions are a bolded lead where a
    bullet had none and the milestone token.
2.  **Every gate is a numbered list item whose first bolded phrase is
    its name.** The four bullet-form specs convert `- **Name.**` to
    `1.  **Name.**`. Where a bullet had no bolded lead, one is added
    from the bullet's own first phrase.
3.  **Every gate ends with its milestone as a bolded token**, in the
    form `**M2.**`, as `runtime-loop.md` already does for all
    fourteen of its gates. A section-level sentence such as *"Milestone
    3 does not pass until every one of these holds"* stays where it is;
    it is prose about the milestone, and the per-gate token is what the
    check reads.
4.  **The docs check reads the token, not the prose.** Its parse is:
    find `## Hard gates`, take every top-level numbered item, take the
    trailing `**M<number>.**`, and fail if any item lacks one. The
    token holds a number rather than a single digit, because
    [skills.md](skills.md) declares gates at Milestone 10. This is
    the weak count-and-identifier check registry rule 1 describes, and
    it is weak on purpose.
5.  **The `spec` field in the registry points at `#hard-gates`.** The
    two example entries in `evaluation-harness.md` are updated from
    `#evaluation`, which is the only place the old anchor is written
    down.

## Gate identifiers

The registry example uses `gate.policy.totality`. That form is adopted
and given a grammar.

```text
gate.<area>.<slug>

area  one of: structure, runtime, tool, builtin, model, policy,
      event, context, memory, harness, api, sandbox, skill,
      knowledge, web, browser, schedule, device, notify, delegate,
      surface, ops, email
slug  lowercase, underscore-separated, unique within its area
```

The area is not the filename. `structure` exists because three gates —
the import-boundary walk, the transaction-hygiene check, and the secret
scanner — are structural statements about the repository that no single
subject spec owns, and `memory` covers all four memory specs because
formation, retrieval, the Milestone 16 benchmark, and the Milestone 17
read surface share a harness and their gates cross-reference each other.
It is the only area more than two specifications share, and the two that
joined it did so on the same argument:
[memory-evaluation-and-lifecycle.md](memory-evaluation-and-lifecycle.md)
measures the subject the area already names and
[memory-read-api-and-browser.md](memory-read-api-and-browser.md) shows
it, so both read the beliefs the first two declare gates over. A route
that returns a belief is a statement about the belief store rather than
about the API, which is why the read surface's gates take `memory` and
not `api`.

`sandbox` is the twelfth and it follows the `memory` precedent rather
than the `structure` one: one spec owns all thirteen, and they are
statements about a subject rather than about the repository. The
alternative considered was a `security` area, which was rejected
because an area names a subject and security is a property of many of
them — the secret scanner, the import boundary, and the cross-tenant
404 would all have a claim on it, and none of them belongs with a
sandbox escape.

`skill` is the thirteenth and follows the same precedent for the same
reason. The alternative considered was splitting its gates across
`context`, `tool`, and `policy`, which was rejected because one spec
owns all sixteen and because two halves of one governance story —
what reaches the prefix, and who may write a skill — would otherwise
land in different areas.

`knowledge` is the fourteenth, on the same precedent again. It is not
folded into `memory` even though both stores are Milestone 9 and share
a harness, because the two memory specs are one subject read and
written while a knowledge document is a different store with a
different trust label, a different isolation predicate, and a
different unit of retrieval. The alternative considered was splitting
its gates across `memory`, `context`, and `tool`, and it was rejected
for the reason `skill` rejected the same split: `visibility` and
`no_belief_write` are two halves of one governance story.

`web` is the fifteenth. It owns the provider contract, routing,
default-off registration, context advertisement, invocation trust, failure
normalization, and fetch confinement as one public-web governance story rather
than distributing those invariants across `tool`, `policy`, and `context`.

`browser` is the sixteenth. It owns authenticated browser navigation, observation,
action authority, profile lifecycle, authentication, and standing grants as one
origin-confined governance story.

`schedule` is the seventeenth area. It is not folded into `event`, `runtime`, or
`api` because occurrence identity, civil-time behavior, authority refresh, and
lifecycle are one control-plane subject whose gates cross all three. The
ordinary run remains owned by those existing areas after materialization.

`device` is the eighteenth and `notify` the nineteenth, both declared by
[notifications-and-devices.md](notifications-and-devices.md) at Milestone 12.
They are two areas rather than one because device identity is the half of that
milestone Milestone 14's Surfaces reuse and deserves its own census line, while
`notify` owns the outbox, the content-free payload, dispatch, and the push
transport as one delivery story. Neither is folded into `api` or `event`: a
device has no session, and the outbox is a sibling write in the triggering
transaction rather than a new event type.

`delegate` is the twentieth, declared by
[subagents-and-delegation.md](subagents-and-delegation.md) at Milestone 13. It
owns the brief, materialization, the child-run suspension and join, derived
limits, subset scopes and tools, untrusted results, the ledger, and the
activation evidence as one delegation story; the child itself remains an
ordinary run owned by `runtime`, `tool`, and `event` after materialization.

`surface` is the twenty-first, declared by
[inbound-surfaces.md](inbound-surfaces.md) at Milestone 14. It owns pairing,
the session-key resolver, the ingress transaction, the trust and attribution
rules for a paired sender, the reply path, and the surface role's confinement
as one inbound-channel story; the run a paired message creates remains an
ordinary run owned by the existing areas.

`ops` is the twenty-second, declared by
[operational-hardening.md](operational-hardening.md) at Milestone 15. It is
`ops` rather than `deploy` because ADR-0048 deliberately says the delivery
jobs add no milestone gate; the subject is the operational lifecycle — backup,
restore, watch, harden, roll back — and it is one spec owning one area, as
`sandbox` was.

`email` is the twenty-third, declared by
[email-integration.md](email-integration.md) at Milestone 18. It is not
folded into `tool` even though every one of its tools crosses the MCP
adapter the tool system owns, because its gates are statements about the
Gmail servers, their rosters, their credential ceremony, and their policy
posture rather than about the adapter that carries them — the same argument
that kept `web` and `browser` out of `tool`. One spec owns all thirteen, as
`sandbox` and `ops` were owned.

Every identifier in the tables below is written in full. Thirteen rows
across four of them used to carry a truncated one, which this grammar
does not admit — a slug is underscore-separated and holds no dots, so
`gate.runtime.one_terminal_wr..` is not an identifier, and
`gate.harness.id_grammar` and `gate.harness.map_bijection` would both
fail on it, the first because it does not match and the second because
it cannot be compared as a set member against anything. Nine of the
twelve gates involved were spelled in full elsewhere in the corpus and
are restored from there. The other three had never been spelled
anywhere, and are completed here from their own declarations: runtime
loop gates 1, 11, and 12 are *"One terminal writer"*, *"A waiting run
holds nothing"*, and *"Cancellation never abandons an effect"*, giving
`gate.runtime.one_terminal_writer`,
`gate.runtime.waiting_holds_nothing`, and
`gate.runtime.cancel_keeps_effects` — the last of which the census
below already reads as *"cancellation keeps effects"*. The
malformed form above is quoted, not registered: the grammar gate
reads the registry tables and `evals/gates/*.yaml`, both of which
hold only real identifiers, and not the prose that explains what a
truncated one looked like.

## Ownership: the three gates declared twice

Three statements appear as gates in two specs each. Each gets one
registry entry, one owner, and an explicit alias so that a reader who
finds it in the non-owning spec knows it is not a second gate.

```text
gate id                            owner            also stated in
---------------------------------  ---------------  -----------------
gate.structure.import_boundary     engineering      tool-system #6
                                   plan, M0
gate.structure.txn_hygiene         event-log #7     runtime-loop #6
gate.event.checkpoint_dispensable  event-log #6     runtime-loop #9
```

The generic import-boundary walk is one of the two registry entries
whose owner is not a detailed-design spec. It is declared in the
engineering plan's Milestone 0 acceptance criteria, alongside the
transaction-hygiene check, the secret scanner, and contract-module
coverage, and its `spec` field points there. `tool-system.md` #6
restates it in one sentence and is its only alias. The secret scanner
is the other, and the gate table below says why.

`model-gateway.md` #1, `policy-and-approvals.md` #3, and
`evaluation-harness.md` #4 also run on the import-boundary walk and are
**not** aliases: each asserts a specific edge that the generic walk
does not name — no provider SDK reachable from the runtime, exactly one
function authorizing an invocation, nothing outside the evals package
importing it. They keep their own ids.

The alias rule matters for counting. A docs check that compares a
spec's gate count against the registry must count an alias in the
declaring spec and not in the registry, so the map records the alias
count per spec and the check subtracts it.

## The gate table

The 25 subject specifications declare 377 gates, the engineering plan
declares 2 more, and this document declares 7 over the corpus: 386
declarations, 383 registry entries once the 3 aliases are subtracted.
`make docs-check` reconciles this paragraph's digits against the
registry, so the arithmetic here cannot drift silently.
Each table gives the gate's number in its own spec, its registry
identifier, its kind, and its milestone.

```text
id                                   kind        M   declared in
-----------------------------------  ----------  --  ----------------
gate.structure.import_boundary       structural  0   plan, Milestone 0
gate.structure.no_committed_secrets  structural  0   plan, Milestone 0
```

The second is specified in
[bootstrap-and-composition.md](bootstrap-and-composition.md), which
gives its five rule families and the three properties that matter more
than its patterns. It is declared here rather than there for the same
reason as the first: both are named in the engineering plan's Milestone
0 acceptance criteria, and neither is a statement about a subject that
a detailed-design spec owns. It was carrying an identifier in a
`security` area that this document's grammar does not define, which is
how it went unregistered; the identifier is corrected to `structure`
and the arithmetic above absorbs it. ADR-0027 and ADR-0028 are not
amended — their totals are records of what was true when each was
decided, and this document is where the current one is stated.

Both are registry entries and both carry a `spec` field, and they are
the only two whose field does not end in `#hard-gates`. The
engineering plan has no such section, because it is organized by
milestone rather than by subject: it declares these two in Milestone
0's acceptance criteria and the prose beneath them, and there is
nowhere else in that file they could live. Their `spec` names
`engineering-plan.md` and the anchor of the heading that declares
them, *"Milestone 0: Repository and engineering foundation"*, which
is the only heading in that file either one could resolve to. Hard
gate 4 below is titled *"Every `spec` field resolves"*, and that is
what it checks: the anchor an entry names exists in the built site,
which is how [evaluation-harness.md](evaluation-harness.md) states it
too. The `#hard-gates` form in that gate's body is the shape every
other entry takes, not a further condition on these two.

### Runtime loop, fourteen gates

Already tagged per gate. Reproduced here so the map is complete and so
the two aliases are visible.

```text
#   id                                  kind         M
--  ----------------------------------  -----------  --
1   gate.runtime.one_terminal_writer    structural   1
2   gate.runtime.no_ambient_time        structural   1
3   gate.runtime.no_ambient_id          structural   1
4   gate.runtime.step_identity          case         1
5   gate.runtime.budget_stops           case         1
6   (alias of gate.structure.txn_hygiene)            2
7   gate.runtime.lease_once             case         2
8   gate.runtime.fenced_no_write        case         2
9   (alias of gate.event.checkpoint_dispensable)     2
10  gate.runtime.resume_idempotent      case         2
11  gate.runtime.waiting_holds_nothing  case         4
12  gate.runtime.cancel_keeps_effects   case         5
13  gate.runtime.build_fits             property     7
14  gate.runtime.build_stable           property     7
```

### Tool system, sixteen gates

The section carried no milestone. The assignment follows the build
order the same document already tags — steps 1 through 5 are Milestone
1, step 6 is Milestone 4, steps 7 and 8 are Milestone 6, step 9 is
Milestone 8 — by asking which build step each gate observes.

```text
#   id                              kind         M   step
--  ------------------------------  -----------  --  ----
1   gate.tool.registration_valid    structural   1   1
2   gate.tool.forced_trust          structural   1   1
3   gate.tool.watermark_first       case         1   4
4   gate.tool.outcome_shape         case         1   5
5   gate.tool.no_external_text      corpus       1   5
6   (alias of gate.structure.import_boundary)    0   --
7   gate.tool.crash_recovery        case         2   4
8   gate.tool.dedup_concurrent      case         2   3
9   gate.tool.normalization_stable  property     1   2
10  gate.tool.reserved_domains      structural   1   1
11  gate.tool.mcp_pipeline_parity   case         8   9
12  gate.tool.mcp_disconnect        case         8   9
13  gate.tool.mcp_sdk_confined      structural   8   9
14  gate.tool.mcp_auth_config       structural   8   9
15  gate.tool.mcp_reauth_bounded    case         8   9
16  gate.tool.mcp_stdio_env_built   case         8   9
```

Gates 11 through 16 are step 9's and say so themselves, so they need
no derivation. Three of the rest need their reasoning stated, because
the milestone is not the one a first reading gives.

1.  **Gate 5 is Milestone 1, not Milestone 8.** The statement is *"No
    external text appears in `message` for any failure path"*, and the
    spec illustrates it with a fake MCP server returning a hostile
    string. The rule is testable at Milestone 1 over a builtin failure
    whose `detail` carries hostile text, and the MCP server is a
    corpus member added at Milestone 8. Growing a corpus strengthens a
    corpus gate without touching its code, which is the property the
    harness separates that kind for. Deferring the whole gate to
    Milestone 8 would leave the rule that keeps prompt injection out of
    the model's input unasserted for seven milestones.
2.  **Gate 7 is Milestone 2, not Milestone 1**, although build step 4
    is a Milestone 1 step. It requires *"a recorded crash at each of
    the fourteen pipeline steps"* recovering to a specified state, and
    recovery is defined over persisted rows. An in-memory adapter that
    dies takes its state with it, so the gate has nothing to observe
    until the run survives the process.
3.  **Gate 8 is Milestone 2 for the same reason and one more.** *"Two
    concurrent submissions of the same call produce one execution"* is
    a statement about a unique index under concurrency. The Milestone 1
    in-memory adapter asserts the single-process half — a second
    submission in the same process returns the first result — and the
    concurrent half arrives with the index.

Gates 11 through 13 were added after the first pass over this document,
when the census made it visible that build step 9 was the only step in
the tool system with no gate observing it. They assert that the MCP
adapter added no path around the pipeline, that a disconnect stays
inside the outcome vocabulary, and that the SDK stops at the adapter
boundary. The third is the engineering plan's last Milestone 8
acceptance criterion promoted from prose to a walk over the import
graph.

Gates 14, 15, and 16 arrived later still, with the authentication
scheme the readiness review found missing behind `credential_ref`.
They divide by what each needs in order to run, which is why there are
three rather than one: gate 14 needs neither a server nor a broker and
tests the configuration validator alone, gate 15 needs a server that
will return 401 on demand, and gate 16 needs a child process it can
read the environment of. A single gate covering all three would be
unrunnable until the last of its dependencies existed, and the first
of them is checkable on the day the column is added.

### Builtin tools, fifteen gates

The first nine observe `math.calculate` or `system.current_time`,
both of which ship at Milestone 1. The last six arrived with the pass
that designed the four remaining builtins, and observe the three
`workspace.` tools and `demo.external_write` at Milestone 4. Gate 10
is structural and runs over the import and call graphs; the rest
observe behaviour.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.builtin.registration       structural   1
2   gate.builtin.reserved_domain    structural   1
3   gate.builtin.demonstration      case         1
4   gate.builtin.parser_property    property     1
5   gate.builtin.bounds_latency     property     1
6   gate.builtin.int_differential   property     1
7   gate.builtin.decimal_exact      case         1
8   gate.builtin.clock_stability    case         1
9   gate.builtin.message_table      structural   1
10  gate.builtin.handle_only        structural   4
11  gate.builtin.text_only          case         4
12  gate.builtin.write_idempotent   case         4
13  gate.builtin.listing_stable     property     4
14  gate.builtin.provenance         case         4
15  gate.builtin.demo_records       case         4
```

### Model gateway, twelve gates

The section already says Milestone 3 for all twelve, and the build
order's fourteen steps carry no tags because all fourteen of them are
Milestone 3 as well. Gate 10 is the one exception worth naming: it
skips when credentials are absent, which registry rule 3 forbids at or
past a gate's milestone.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.model.sdk_isolation        structural   3
2   gate.model.contract_identical   case         3
3   gate.model.call_id_roundtrip    case         3
4   gate.model.stream_invariants    case         3
5   gate.model.no_secret_leak       structural   3
6   gate.model.malformed_args       case         3
7   gate.model.pin_survives_resume  case         3
8   gate.model.cost_on_failure      case         3
9   gate.model.ollama_scenario      case         3
10  gate.model.live_smoke           case         3
11  gate.model.metadata_closed      structural   3
12  gate.model.profile_valid        corpus       3
```

Gate 10 is registered with `optional: true`, a field the registry does
not yet have and which this document adds for it alone: a gate that is
allowed to report `skipped` when a named precondition is absent, with
the precondition recorded. Without the field the choice is a forbidden
skip or a gate that fails on every machine without vendor keys, and
both are worse than naming the exception once.

### Policy and approvals, thirteen gates

All thirteen are Milestone 4, which the section states and the build
sequence confirms — steps 1 through 11 are Milestone 4 and step 12 is
sequenced separately and is not a dependency. The last three arrived
with the scope vocabulary, which that section owns because the check
runs at this milestone and the API document that enumerated the first
nine strings is Milestone 5.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.policy.totality            property     4
2   gate.policy.determinism         property     4
3   gate.policy.single_gate         structural   4
4   gate.policy.monotonicity        property     4
5   gate.policy.hardline_immutable  case         4
6   gate.policy.revalidation        case         4
7   gate.policy.cross_tenant        case         4
8   gate.policy.no_leakage          case         4
9   gate.policy.idempotent_resolve  case         4
10  gate.policy.prompt_not_authz    corpus       4
11  gate.policy.scope_grammar       structural   4
12  gate.policy.scope_match         case         4
13  gate.policy.scope_stamped       case         4
```

### Event log and persistence, fourteen gates

The section says Milestone 2 for eleven of the fourteen, and three
things need a word.

The first is the transaction-hygiene gate, already decided in ADR-0024:
the *check* is a Milestone 0 deliverable and its *gate* is a Milestone 2
acceptance criterion. The registry entry carries Milestone 2, because a
gate is the assertion and not the tool, and Milestone 0's obligation is
that the tool exists and passes against a nearly empty repository.

The second is the revision-graph walk, added with the migration
conventions ADR-0031 records. It registers at Milestone 0, and unlike
every other early registration in this document the spec says so
itself — the gate is tagged **M0.** where it is declared. Milestone 0
already requires that an empty Alembic migration runs, so there is a
graph to walk on the day the milestone closes. The rule is the same one
that produced every other assignment here: a gate lands at the milestone
that builds the last thing it observes, and what this one observes is
the graph.

The third is the pair of export gates, 13 and 14, which the spec tags
**M3.** where they are declared. Milestone 2 builds the projection's
scaffold and the `export_consent` column the stamp lands in; Milestone
3 builds the document builder, the redaction pipeline, and the consent
tables, which is what those two gates observe. The rule is the same
rule again.

```text
#   id                                 kind         M
--  ---------------------------------  -----------  --
1   gate.event.sequence_integrity      property     2
2   gate.event.projection_determ       case         2
3   gate.event.upcaster_totality       property     2
4   gate.event.exactly_once            case         2
5   gate.event.crash_recovery          case         2
6   gate.event.checkpoint_dispensable  case         2
7   gate.structure.txn_hygiene         structural   2
8   gate.structure.migration_graph     structural   0
9   gate.event.migration_clean         case         2
10  gate.event.migration_stepwise      case         2
11  gate.event.revision_pinned         case         2
12  gate.structure.orm_confined        structural   2
13  gate.event.export_redacted         case         3
14  gate.event.export_consent          case         3
```

The build sequence's nine steps are Milestone 2, except that step 8
keeps only the projection's scaffold and the consent stamp there and
places the document builder, the redaction pipeline, the consent
tables, and both export gates at Milestone 3.

### Context engine, six gates

The section says Milestone 7 for all five. One moves earlier, because
ADR-0024 already scheduled it: the Milestone 1 context builder is
build-sequence step 1, and its acceptance is that `build()` twice on
one checkpoint is byte-identical *and* that the request bytes differ in
Region B only. That is gate 1, exactly.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.context.determinism        property     1
2   gate.context.prefix_stability   case         7
3   gate.context.budget_conform     case         7
4   gate.context.tool_pair_integ    property     7
5   gate.context.trust_preserved    corpus       7
6   gate.context.history_cut        property     7
```

Gate 2 stays at Milestone 7 because its subject is a scripted
fifty-turn session with a forced compaction, a revoked tool, and a
memory correction — none of which exist before Milestone 7. Gate 1
stays true from Milestone 1 onward and is the reason the builder is
written deterministically from the first commit rather than
retrofitted. Gate 6 was added later, when the readiness review found
that history had a yield order and no rule saying which items were in
the request before yielding began; it is a property gate for the same
reason gate 1 is, because the claim is about every input rather than
about a chosen one.

The build sequence's seven steps are Milestone 7 except step 1, which
is Milestone 1 by the same decision.

### Memory formation, twenty gates and four metrics

The original list of eight bullets separates into four Milestone 9 gates and
four metrics by what the spec calls them. The trailing sentence *"Gate: memory
improves target eval cases without increasing policy failures"* is a fifth
gate, not a closing remark. Milestone 10 memory maturation adds fifteen explicit
gates: five for ordinary-conversation formation and lifecycle, then ten for the
governed inspection surface and evaluation-gated provider-assisted extractor.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.memory.contradiction       case         9
2   gate.memory.no_fabrication      corpus       9
3   gate.memory.form_injection      corpus       9
4   gate.memory.correction_durable  case         9
5   gate.memory.no_policy_regress   case         9
6   gate.memory.multi_candidate     case        10
7   gate.memory.source_integrity    property    10
8   gate.memory.idle_lifecycle      case        10
9   gate.memory.formation_bounded   case        10
10  gate.memory.correction_isolated case        10
11  gate.memory.inspection_governed case        10
12  gate.memory.extractor_contract  structural  10
13  gate.memory.provider_activation_bound property 10
14  gate.memory.provider_boundary   case        10
15  gate.memory.provider_audit_fallback case     10
16  gate.memory.provider_evidence_publish case   10
17  gate.memory.provider_claim_rendering structural 10
18  gate.memory.provider_failure_diagnostics case 10
19  gate.memory.provider_positive_coverage case  10
20  gate.memory.provider_source_safety case      10
```

Moved to `## Tracked metrics`: formation precision, recall of
consequential facts, rejection rate, and cost. Each is described in the
spec as something measured and improved rather than something that
blocks, and the first is explicitly *"The primary metric"*.

### Memory retrieval, nine gates and seven metrics

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.memory.currency            case         9
2   gate.memory.historical_correct  case         9
3   gate.memory.recall_injection    corpus       9
4   gate.memory.scope_isolation     property     9
5   gate.memory.trace_faithful      case         9
6   gate.memory.view_ceiling        property     9
7   gate.memory.retr_correction     case         9
8   gate.memory.cache_preserved     case         9
9   gate.memory.no_triple_regress   case         9
```

Gates 5 and 6 come from one bullet, because that bullet says *"Both are
hard gates"* about two distinct statements: that a sampled turn's trace
reproduces the rendered block the recorded hash covers, and that no
belief above `min(recall ceiling, viewing ceiling)` ever appears in a
view. They fail for different reasons and are separated here.

Moved to `## Tracked metrics`: consequential recall@k, noise ratio,
transfer precision, transfer lift, cost, latency, and end-to-end lift.

### Evaluation harness, eleven gates

The harness's own gates are the ones nothing else can check, and they
are the earliest in the corpus: four run against a repository with no
agent in it.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.harness.registry_complete  structural   0
2   gate.harness.no_stale_skip      structural   0
3   gate.harness.contract_coverage  structural   0
4   gate.harness.evals_isolation    structural   0
5   gate.harness.no_eval_in_prod    case         1
6   gate.harness.case_schema        structural   1
7   gate.harness.no_egress          case         1
8   gate.harness.corpus_minimum     structural   4
9   gate.harness.trajectory_source  structural   3
10  gate.harness.reason_code_table  structural   1
11  gate.harness.mcp_no_socket      case         8
```

Gates 1 through 4 are Milestone 0 and three of them are vacuously true
of an empty repository — no gates declared, no ports, no imports. That
is the point rather than an objection, and the engineering plan already
argues it: a structural check added later is added against existing
violations, which is the situation in which it gets relaxed rather than
obeyed. Gate 4 asserts one edge of the same walk that
`gate.structure.import_boundary` performs, and is a gate of its own
because the edge is specific to the evals package.

Gate 5 is Milestone 1 rather than Milestone 0 because it runs the
production loader, and the loader is the composition root's refuse
phase, which is Milestone 1. Gates 6, 7, and 10 are Milestone 1 for the
same class of reason: each needs something the vertical slice builds.
Gate 8 is Milestone 4 with the corpora, and gate 9 says its own
milestone. Gate 11 is Milestone 8 because it needs MCP cases to run
against, and it is a case gate rather than a structural one for the
same reason gate 7 is: an offline fixture layer is proven by running
the cases with egress blocked, not by reading them.

### HTTP API and streaming, ten gates

Ten gates, all Milestone 5, all new. The spec tags each one, so the
assignment is the spec's own rather than derived. They are the reason
Milestone 5 stops being the milestone with one gate and the largest
externally visible surface.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.api.code_vocabulary        case         5
2   gate.api.status_map_total       structural   5
3   gate.api.no_request_tenant      structural   5
4   gate.api.cross_tenant_404       case         5
5   gate.api.scope_declared         structural   5
6   gate.api.transient_no_id        case         5
7   gate.api.replay_exact           case         5
8   gate.api.cancel_observed        case         5
9   gate.api.submit_idempotent      case         5
10  gate.api.artifact_attachment    case         5
```

Gates 2, 3, and 5 are structural because they are assertions about the
shape of the code rather than about a run: a mapping that must be
total, an AST walk that must find no binding of `tenant_id` from a
request, and a walk over the route table that must find a declared
scope on every route but the two health probes. The remaining seven
need a running API and are cases.

### Sandbox isolation and artifacts, thirteen gates

Thirteen gates, all new, in a new twelfth area. Eleven are Milestone 6,
which had none; one is Milestone 1 and one is Milestone 4, because a
gate lands at the milestone that builds the last thing it observes and
those two observe code written before the sandbox exists. The spec
tags each one.

```text
#   id                                   kind         M
--  -----------------------------------  -----------  --
1   gate.sandbox.production_refuses_dev  structural   1
2   gate.sandbox.no_runtime_in_worker    structural   6
3   gate.sandbox.spec_has_no_host_path   structural   6
4   gate.sandbox.no_credential_reaches   case         6
5   gate.sandbox.network_denied          case         6
6   gate.sandbox.egress_allowlisted      case         6
7   gate.sandbox.limits_enforced         case         6
8   gate.sandbox.escape_denied           case         6
9   gate.sandbox.workspace_isolated      case         6
10  gate.sandbox.no_orphans              case         6
11  gate.sandbox.artifact_checksum       case         6
12  gate.sandbox.artifact_key_opaque     structural   6
13  gate.sandbox.workspace_containment   property     4
```

Gate 1 is Milestone 1 because the composition root refuses the
development mechanism from the first startup that has a mechanism to
choose, and gate 13 is Milestone 4 because `WorkspaceHandle` is what
the three `workspace.` tools resolve paths through. Gates 2, 3, and 12
are structural for the usual reason — an import walk, a check over
declared field types, and a check over a function's parameters — and
gate 13 is the corpus's sixteenth property test. The remaining eight
need a real sandbox and are cases. One of them, gate 8, is the
red-team escape test Section 28.7 has required since version 2.0 and
the harness had no case for.

### Skills, sixteen gates

Sixteen gates, all new, in a new thirteenth area. Ten are Milestone 8
and six are Milestone 10 — the two milestones in the plan that had
none, and Milestone 10 is the first milestone past 9 to acquire a
census row. The split is the substrate against the authoring loop:
everything that decides what a skill is, where it lives, and what it
may reach is Milestone 8, and everything that lets an agent write one
is Milestone 10. The spec tags each one.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.skill.metadata_only        case         8
2   gate.skill.catalog_pinned       case         8
3   gate.skill.no_tool_from_skill   structural   8
4   gate.skill.untrusted_body       case         8
5   gate.skill.revision_pinned      case         8
6   gate.skill.missing_tool_loads   case         8
7   gate.skill.catalog_capped       case         8
8   gate.skill.validation_total     property     8
9   gate.skill.body_cap             case         8
10  gate.skill.mcp_read_only        case         8
11  gate.skill.authoring_trust      corpus      10
12  gate.skill.authoring_scope      case        10
13  gate.skill.review_confined      case        10
14  gate.skill.review_never_fatal   case        10
15  gate.skill.provenance_complete  structural  10
16  gate.skill.edit_conflict        case        10
```

Gates 3 and 15 are structural because each is an assertion about the
shape of the code rather than about a run: a walk that must find no
path from the skills package to the tool registry's write path, and
an insert path with no branch that can omit provenance. Gate 8 is the
corpus's seventeenth property test. Gate 11 is a corpus gate because
"an untrusted turn" is a family of turns rather than one, and a
single case would prove only the one it wrote. The remaining twelve
need a running session and are cases.

### Knowledge documents, twelve gates

Twelve gates, all new, in a new fourteenth area, and all of them
Milestone 9 — the milestone whose second half had none. Milestone 9
goes from fourteen registry entries to twenty-six, which makes it the
largest single milestone in the census after Milestone 1.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.knowledge.ingest_trust     case         9
2   gate.knowledge.no_secrets       case         9
3   gate.knowledge.chunk_stable     property     9
4   gate.knowledge.visibility       case         9
5   gate.knowledge.verbatim         property     9
6   gate.knowledge.cite_resolves    property     9
7   gate.knowledge.budget_yield     case         9
8   gate.knowledge.supersession     case         9
9   gate.knowledge.delete_cascades  case         9
10  gate.knowledge.trace_complete   case         9
11  gate.knowledge.no_belief_write  case         9
12  gate.knowledge.corpus_recall    corpus       9
```

Gate 3 is the corpus's eighteenth property test, gate 5 its nineteenth,
and gate 6 its twentieth — determinism, verbatim rendering, and
citation resolution are each a statement over every chunk rather than
over one, and a single case would prove only the chunk it wrote. Gate
12 is a corpus gate because passage recall is a distribution over a
labelled question set. The remaining eight need a running session and
are cases. None is structural: every one of them is a statement about
what a run retrieves rather than about the shape of the repository.

### Web access, seven gates

Seven gates in the fifteenth area, all at Milestone 10. They turn the
public-web acceptance contract into blocking checks over the two shared
provider operations, capability routing, default-off composition, context
advertisement, durable trust, failure normalization, and fetch confinement.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.web.provider_contract      case        10
2   gate.web.capability_routing     case        10
3   gate.web.default_off            case        10
4   gate.web.context_advertisement  case        10
5   gate.web.invocation_trust       case        10
6   gate.web.failure_boundary       case        10
7   gate.web.fetch_confinement      property    10
```

The first gate executes the complete shared search-and-fetch contract against
both adapters. Gate 7 is a property gate because it covers a family of hostile
destinations and response/output sizes; the other five require concrete
composition or run outcomes.

### Authenticated browser automation, ten gates

Ten gates in the sixteenth area, all at Milestone 10. The first seven make the
implemented provider seam, isolation boundary, read path, approval path,
revision binding, and uncertain-write behavior blocking. The final three keep
profiles, authentication, and standing grants visibly incomplete until their
security contracts execute.

```text
#   id                                kind         M
--  --------------------------------  -----------  --
1   gate.browser.provider_contract    case        10
2   gate.browser.default_off          case        10
3   gate.browser.origin_isolation     property    10
4   gate.browser.observation_trust    case        10
5   gate.browser.action_authority     case        10
6   gate.browser.revision_binding     case        10
7   gate.browser.uncertain_write      case        10
8   gate.browser.profile_lifecycle    case        10
9   gate.browser.authentication       property    10
10  gate.browser.standing_grant       case        10
```

Origin isolation is a property over allowed and hostile URL/resource families;
authentication is a property over secret classes and user-intervention states.
The remaining eight require concrete provider, run, profile, or grant outcomes.

### Scheduled runs, twenty-three gates

Twenty-three gates, all new, in the seventeenth area and all at Milestone 11. They
cover the time, authority, atomicity, lifecycle, fairness, offline-result,
contract, schema, migration, erasure, and isolation boundaries introduced by a
scheduler rather than resting those guarantees on generic milestone checks.

```text
#   id                                kind         M
--  --------------------------------  -----------  --
1   gate.schedule.not_early           property     11
2   gate.schedule.materialize_once    case         11
3   gate.schedule.materialize_atomic  case         11
4   gate.schedule.civil_time          property     11
5   gate.schedule.misfire_bounded     case         11
6   gate.schedule.no_overlap          case         11
7   gate.schedule.authority_fresh     case         11
8   gate.schedule.scope_isolated      case         11
9   gate.schedule.revision_pinned     case         11
10  gate.schedule.lifecycle_linear    property     11
11  gate.schedule.cancel_separate     case         11
12  gate.schedule.run_bounded         case         11
13  gate.schedule.priority_fair       case         11
14  gate.schedule.offline_results     case         11
15  gate.schedule.no_credentials      corpus       11
16  gate.schedule.domain_invariants   property     11
17  gate.schedule.repository_contract structural   11
18  gate.schedule.persistence_schema  structural   11
19  gate.schedule.migration_clean     case         11
20  gate.schedule.migration_stepwise  case         11
21  gate.schedule.uow_atomic          case         11
22  gate.schedule.erasure_audited     case         11
23  gate.schedule.persistence_isolated case        11
```

Gates 1, 4, and 10 are properties because early firing, civil-time resolution,
state-transition linearizability are claims over generated clocks, zones, and
interleavings; gate 16 applies the same standard to domain construction. Gate
15 is a corpus because credential-shaped input is a family rather than one
example. Gates 17 and 18 are structural because they inspect contract coverage
and declared schema. The remaining sixteen are boundary cases over the
application, PostgreSQL, queue, and API seams.

### Notifications and devices, twenty gates

Twenty gates, all new, in the eighteenth and nineteenth areas and all at
Milestone 12. Six `device` gates cover registration identity, live-token
uniqueness, revocation, lifecycle audit, schema, and isolation; fourteen
`notify` gates cover enqueue atomicity, the closed trigger catalog,
deduplication, content-free payloads, single delivery, bounded retry,
staleness, token invalidation, the APNs adapter, port contracts, the offline
inbox, default-off confinement, and the migration pair.

```text
#   id                                  kind         M
--  ----------------------------------  -----------  --
1   gate.device.register_idempotent     case         12
2   gate.device.token_unique            case         12
3   gate.device.revoke_immediate        case         12
4   gate.device.lifecycle_audited       case         12
5   gate.device.persistence_schema      structural   12
6   gate.device.persistence_isolated    case         12
7   gate.notify.enqueue_atomic          case         12
8   gate.notify.trigger_catalog         case         12
9   gate.notify.dedupe                  property     12
10  gate.notify.content_free            corpus       12
11  gate.notify.dispatch_once           case         12
12  gate.notify.retry_bounded           case         12
13  gate.notify.stale_suppressed        case         12
14  gate.notify.token_revoked_on_410    case         12
15  gate.notify.apns_auth               case         12
16  gate.notify.port_contracts          structural   12
17  gate.notify.offline_inbox           case         12
18  gate.notify.default_off             case         12
19  gate.notify.migration_clean         case         12
20  gate.notify.migration_stepwise      case         12
```

Gate 9 is a property because repeated triggers are a family of interleavings,
not one example; gate 10 is a corpus because secret-shaped and content-bearing
inputs are a family; gates 5 and 16 are structural because they inspect
declared schema and contract coverage. The remaining sixteen are boundary cases
over the terminal writer, the scheduler, the dispatcher, the transport, the
API, and PostgreSQL.

### Subagents and delegation, twenty-one gates

Twenty-one gates, all new, in the twentieth area and all at Milestone 13. They
cover the brief, atomic materialization, the dedicated session, the child-run
suspension, subset tools and scopes, derived limits, additive usage, depth,
fan-out, untrusted results, the single join, child failure, cancellation,
prefix stability, the separate trace, artifact references, schema, migration,
default-off, and the outcome evidence the gate for multi-agent work requires.

```text
#   id                                         kind         M
--  -----------------------------------------  -----------  --
1   gate.delegate.brief_schema                 case         13
2   gate.delegate.materialize_atomic           case         13
3   gate.delegate.dedicated_session            case         13
4   gate.delegate.parent_suspends              case         13
5   gate.delegate.tools_subset                 case         13
6   gate.delegate.scopes_intersected           case         13
7   gate.delegate.limits_derived               property     13
8   gate.delegate.usage_additive               case         13
9   gate.delegate.depth_one                    case         13
10  gate.delegate.fanout_capped                case         13
11  gate.delegate.result_untrusted             case         13
12  gate.delegate.join_once                    case         13
13  gate.delegate.child_failure_is_tool_error  case         13
14  gate.delegate.cancel_propagates            case         13
15  gate.delegate.prefix_stable                case         13
16  gate.delegate.trace_separate               case         13
17  gate.delegate.artifact_refs                case         13
18  gate.delegate.persistence_schema           structural   13
19  gate.delegate.migration_stepwise           case         13
20  gate.delegate.default_off                  case         13
21  gate.delegate.changes_outcome              case         13
```

Gate 7 is a property because limit derivation is a claim over generated
parents and briefs; gate 18 is structural because it inspects declared schema.
The remaining nineteen are boundary cases over the tool pipeline, the terminal
writer, the queue, PostgreSQL, and the evaluation harness.

### Inbound surfaces, twenty-one gates

Twenty-one gates, all new, in the twenty-first area and all at Milestone 14.
They cover the default-deny before any run, the pairing ceremony and lockout,
ordinary submission, inbound idempotency, ingress atomicity, the session key,
input routing, revocation, the scope ceiling, the bot token, replies,
approvals and questions, rate limits, default-off, transport confinement,
schema, isolation, contracts, and the migration pair.

```text
#   id                                         kind         M
--  -----------------------------------------  -----------  --
1   gate.surface.unpaired_denied               case         14
2   gate.surface.pairing_ceremony              case         14
3   gate.surface.pairing_lockout               case         14
4   gate.surface.paired_submits_ordinary_run   case         14
5   gate.surface.inbound_idempotent            case         14
6   gate.surface.ingest_atomic                 case         14
7   gate.surface.session_key_stable            case         14
8   gate.surface.input_routing                 case         14
9   gate.surface.revocation_immediate          case         14
10  gate.surface.scope_ceiling                 case         14
11  gate.surface.no_token_leak                 corpus       14
12  gate.surface.reply_chunked_redacted        case         14
13  gate.surface.approval_roundtrip            case         14
14  gate.surface.rate_limited                  case         14
15  gate.surface.default_off                   case         14
16  gate.surface.transport_confined            structural   14
17  gate.surface.persistence_schema            structural   14
18  gate.surface.persistence_isolated          case         14
19  gate.surface.repository_contract           structural   14
20  gate.surface.migration_clean               case         14
21  gate.surface.migration_stepwise            case         14
```

Gate 11 is a corpus because a token can leak through a family of surfaces;
gates 16, 17, and 19 are structural because they inspect the transport's
declared confinement, the schema, and contract coverage. The remaining
seventeen are boundary cases over the ingress transaction, the submission
path, the outbox, and PostgreSQL.

### Operational hardening, sixteen gates

Sixteen gates, all new, in the twenty-second area and all at Milestone 15.
They cover the declared backup set, the round trip, the restore rehearsal and
its corruption detection, client-side encryption, retention, production
refusal, the health-check signal list, alert deduplication and payload
closure, the dead-database fallback, rollback promotion and schema-drift
refusal, unit and sudoers reconciliation, the minimal public boundary, and the
worker watchdog.

```text
#   id                                              kind         M
--  ----------------------------------------------  -----------  --
1   gate.ops.backup_set_complete                    structural   15
2   gate.ops.backup_roundtrip                       case         15
3   gate.ops.restore_rehearsal_passes               case         15
4   gate.ops.restore_rehearsal_detects_corruption   case         15
5   gate.ops.backup_encrypted_offhost               case         15
6   gate.ops.backup_retention_policy                property     15
7   gate.ops.rehearsal_never_touches_production     case         15
8   gate.ops.healthcheck_signals                    case         15
9   gate.ops.alert_enqueued_deduped                 case         15
10  gate.ops.alert_payload_closed                   structural   15
11  gate.ops.db_down_fallback                       case         15
12  gate.ops.rollback_promotes_previous             case         15
13  gate.ops.rollback_refuses_schema_drift          case         15
14  gate.ops.units_and_sudoers_reconciled           structural   15
15  gate.ops.public_boundary_minimal                structural   15
16  gate.ops.worker_watchdog                        case         15
```

Gate 6 is a property because retention is a claim over generated listings;
gates 1, 10, 14, and 15 are structural because they inspect a manifest, a
schema, unit and sudoers files, and firewall and proxy declarations. The
remaining eleven are boundary cases over the scripts, a throwaway database,
the outbox, and the release tree.

### Memory evaluation and lifecycle, twenty gates

Twenty gates, all new, in the existing `memory` area and all at Milestone
16. Nine cover the benchmark — the corpus shape, reproducibility, the two
baseline comparisons, protected content, supersession currency, live evidence
publication, the cost ceiling, and the external dataset adapters — and ten
cover the lifecycle the two older memory specifications describe and the code
lacks: the profile document, trace retention, decay, usage feedback, the
recall delta, correction lines, established facts, conflict surfacing, the
resolver's ordering, and re-derivation. The twentieth versions the expanded
provider-formation corpus coverage independently of Milestone 10's completed
historical gate.

```text
#   id                                              kind         M
--  ----------------------------------------------  -----------  --
1   gate.memory.bench_corpus_shape                  structural   16
2   gate.memory.bench_reproducible                  property     16
3   gate.memory.bench_no_regression                 case         16
4   gate.memory.bench_baseline_current              case         16
5   gate.memory.bench_protected_never_rendered      case         16
6   gate.memory.bench_supersession_current          case         16
7   gate.memory.bench_evidence_publish              case         16
8   gate.memory.bench_cost_ceiling                  case         16
9   gate.memory.bench_external_adapters             structural   16
10  gate.memory.profiles_wired                      structural   16
11  gate.memory.trace_retention                     case         16
12  gate.memory.decay_lifecycle                     case         16
13  gate.memory.usage_feedback                      case         16
14  gate.memory.recall_delta                        case         16
15  gate.memory.correction_lines                    case         16
16  gate.memory.established_facts_form              case         16
17  gate.memory.conflict_surfaced                   case         16
18  gate.memory.authority_recency                   property     16
19  gate.memory.rederive_opt_in                     case         16
20  gate.memory.provider_positive_coverage_v2       case         16
```

Gates 2 and 18 are properties because reproducibility and the resolver's
ordering are claims over generated inputs rather than over one scenario; gates
1, 9, and 10 are structural because they inspect a checked-in document, a set
of adapters and what the repository does not contain, and a configuration
document against its models. The remaining fifteen are boundary cases over
the benchmark run, the belief store, the recall trace, the context builder,
the command line, and the versioned provider-evidence corpus.

### Memory read API and browser, ten gates

Ten gates, all new, in the same `memory` area and all at Milestone 17. Seven
are statements about what the two read routes may return — the required
ceiling, the strict ceiling filter, principal isolation, keyset paging under
concurrent writes, the filters, the exposure list, and the closed error
vocabulary — and three are statements about the surface itself: that every
route is a GET declaring exactly `memory.read`, that the router is absent
without its flag, and that both store adapters browse identically.

```text
#   id                                              kind         M
--  ----------------------------------------------  -----------  --
1   gate.memory.read_api_ceiling_required           case         17
2   gate.memory.read_api_ceiling_filter             property     17
3   gate.memory.read_api_principal_isolation        case         17
4   gate.memory.read_api_pagination                 case         17
5   gate.memory.read_api_filters                    case         17
6   gate.memory.read_api_read_only                  structural   17
7   gate.memory.read_api_flag_absent                case         17
8   gate.memory.browse_contract_parity              structural   17
9   gate.memory.read_api_view_projection            structural   17
10  gate.memory.read_api_error_vocabulary           case         17
```

Gate 2 is a property because the ceiling claim is over generated pairs of
belief sensitivity and request ceiling rather than over one arrangement of
rows; gates 6, 8, and 9 are structural because they inspect the router's
declared methods and scopes, the adapters against one contract suite, and a
serialized model against its exposure list. The remaining six are boundary
cases over the two routes.

### Email integration, thirteen gates

Thirteen gates, all new, in the new `email` area and all at Milestone 18.
Five are statements about the servers themselves — the package boundary, the
shared contract, the rosters, the declared classifications, and the failure
taxonomy — five are statements about credentials and configuration — the
constructed environment, token confinement, the default-off flag, scope
confinement, and the bootstrap ceremony — and three are statements about
policy and composition in use: the read-allows-write-approves split, the
untrusted-origin send, and the monitoring recipe.

```text
#   id                                              kind         M
--  ----------------------------------------------  -----------  --
1   gate.email.package_isolation                    structural   18
2   gate.email.contract_parity                      case         18
3   gate.email.roster_confinement                   structural   18
4   gate.email.classification                       case         18
5   gate.email.read_allow_write_approve             case         18
6   gate.email.untrusted_origin                     case         18
7   gate.email.credential_confinement               property     18
8   gate.email.token_confinement                    case         18
9   gate.email.default_off                          case         18
10  gate.email.scope_confinement                    case         18
11  gate.email.bootstrap_consent                    case         18
12  gate.email.failure_taxonomy                     case         18
13  gate.email.monitoring_recipe                    case         18
```

Gate 7 is a property because the credential claim is over generated server
configurations rather than one arrangement; gates 1 and 3 are structural
because they inspect the import graph and the advertised rosters rather than
a behavior. The remaining ten are boundary cases over the three servers, the
policy engine, the composition root, and the bootstrap command.

### Conversational schedule creation, five gates

Five gates extend the existing `schedule` area at Milestone 19. They cover the
narrow model-callable bridge from a reminder request to the already governed
schedule service: composition and classification, successful approved
creation, exact-scope denial, invalid-time rejection, and idempotent replay.

```text
#   id                                              kind         M
--  ----------------------------------------------  -----------  --
24  gate.schedule.model_create_contract             structural   19
25  gate.schedule.model_create_happy_path           case         19
26  gate.schedule.model_create_authorization        case         19
27  gate.schedule.model_create_validation           case         19
28  gate.schedule.model_create_retry                case         19
```

Gate 24 is structural because it inspects the registered tool contract and its
feature-gated composition. The remaining four are boundary cases over the
tool pipeline and the schedule application service.

### Calendar recurrence and conversational schedules, six gates

Six gates extend the existing `schedule` area at Milestone 20. They cover the
new calendar values and algorithms, bounded downtime behavior, the existing
HTTP control plane's widened union, and approval-gated conversational creation
and replay for every recurring kind.

```text
#   id                                                   kind         M
--  ---------------------------------------------------  -----------  --
29  gate.schedule.calendar_values                        property     20
30  gate.schedule.calendar_recurrence                    property     20
31  gate.schedule.calendar_misfire_bounded               case         20
32  gate.schedule.calendar_http_roundtrip                case         20
33  gate.schedule.model_create_recurring                 case         20
34  gate.schedule.model_create_recurring_validation      case         20
```

Gates 29 and 30 are properties because canonical calendar values and civil-time
resolution range over generated selectors, dates, and zones. The remaining
four are boundary cases over materialization, HTTP, and the ordinary tool
pipeline.

### Adaptive memory distillation, twenty-four gates

Twenty-four gates extend the existing `memory` area at Milestone 21. Six cover
the three-stage provider pipeline and its persisted episode input, seven cover
high-recall grounded formation and accounting, seven cover the separation of
evidence, use, forgetting, promotion, and rendered uncertainty, and four cover
corrections, migration, corpus coverage, comparative evidence, and activation.

```text
#   id                                              kind         M
--  ----------------------------------------------  -----------  --
1   gate.memory.distill_versions_frozen             structural   21
2   gate.memory.episode_integration                 case         21
3   gate.memory.episode_repository_parity           structural   21
4   gate.memory.anticipation_blinded                property     21
5   gate.memory.prediction_error_calls              case         21
6   gate.memory.distill_fallback                    case         21
7   gate.memory.direct_high_recall                  case         21
8   gate.memory.hypothesis_high_recall              case         21
9   gate.memory.compound_recall                     case         21
10  gate.memory.candidate_schema                    structural   21
11  gate.memory.predictability_attributed           case         21
12  gate.memory.source_grounding                    property     21
13  gate.memory.formation_reason_telemetry          case         21
14  gate.memory.evidence_clock                      case         21
15  gate.memory.usage_clock                         case         21
16  gate.memory.hypothesis_retirement               case         21
17  gate.memory.ongoing_retirement                  case         21
18  gate.memory.evidence_promotion                  case         21
19  gate.memory.uncertainty_rendered                case         21
20  gate.memory.correction_durable_v3               case         21
21  gate.memory.schema_backfill                     structural   21
22  gate.memory.formation_corpus_v3                 structural   21
23  gate.memory.comparative_evidence                case         21
24  gate.memory.distill_activation_bound            property     21
```

Gates 4, 12, and 24 are properties because causal blinding, source ownership,
and exact tuple activation quantify over generated prefixes, event identities,
and tuple differences. Gates 1, 3, 10, 21, and 22 are structural because they
inspect frozen controls, both repositories against one contract, closed
schemas, migrations, and the checked-in corpus. The remaining sixteen are
boundary cases over provider orchestration, formation, lifecycle, retrieval,
and evidence publication.

### This document, seven gates

The seven gates stated under [Hard gates](#hard-gates) below are this
document's own. Their identifiers are listed here so that the table
covers every registry entry and not merely every gate this document
inherited.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.harness.token_present      structural   0
2   gate.harness.map_bijection      structural   0
3   gate.harness.alias_arithmetic   structural   0
4   gate.harness.anchor_resolves    structural   0
5   gate.harness.id_grammar         structural   0
6   gate.harness.census_derived     structural   0
7   gate.harness.milestone_order    structural   1
```

They take the `harness` area because the harness owns the registry and
the docs check that reads it. This document declares them rather than
the harness because every one of them is a check over the scheduling
record, and the scheduling record is what this document is. Gate 7 is
Milestone 1 rather than Milestone 0 because the build-sequence table it
reads is only meaningful once there are build steps to order.

## The census

What each milestone must turn green, counting registry entries and not
alias restatements.

```text
milestone  new gates  cumulative  the earliest of them
---------  ---------  ----------  -----------------------------
0                 13          13  the import boundary, the secret
                                  scanner, ten checks over documents,
                                  and the migration graph
1                 28          41  the vertical slice, both
                                  builtins, deterministic build
2                 16          57  persistence, fencing, resume, the
                                  ORM boundary, the migration
                                  round trips
3                 15          72  five adapters, the profile schema,
                                  the closed metadata key set, the
                                  redacted export
4                 22          94  policy, approvals, corpora, the
                                  four remaining builtins, the
                                  scope vocabulary
5                 11         105  the API surface, the stream,
                                  cancellation keeps effects
6                 11         116  isolation, egress, artifacts
7                  7         123  budgeting and compaction
8                 17         140  the MCP adapter, authentication,
                                  the pinned catalog, the metadata
                                  boundary, package validation
9                 26         166  formation, retrieval, ingestion,
                                  and the corpus
10                38         204  the authoring loop, background review,
                                  governed memory maturation, public-web
                                  access, and browser automation
11                23         227  recurrence, occurrence atomicity,
                                  authority refresh, offline results,
                                  contracts, migration, erasure, isolation
12                20         247  device identity, the outbox, content-
                                  free payloads, dispatch, the APNs
                                  transport, the offline inbox
13                21         268  the brief, materialization, the
                                  child-run suspension and join, derived
                                  limits, the ledger, the evidence
14                21         289  pairing, the session key, ingress
                                  atomicity, the scope ceiling, replies,
                                  the surface role's confinement
15                16         305  the declared backup set, the
                                  rehearsal, alerts, rollback, the
                                  public boundary, the watchdog
16                20         325  the benchmark corpus, the baseline,
                                  live evidence, decay, usage feedback,
                                  the recall delta, conflicts, expanded
                                  provider-formation coverage
17                10         335  the required ceiling, the ceiling
                                  filter, principal isolation, keyset
                                  paging, the read-only router, the
                                  exposure list
18                13         348  the package boundary, the server
                                  contract, honest classification,
                                  credential and token confinement,
                                  the bootstrap ceremony, the
                                  monitoring recipe
19                 5         353  the model-callable schedule contract,
                                  approval, exact-scope authorization,
                                  time validation, idempotent replay
20                 6         359  monthly and yearly calendar rules,
                                  bounded catch-up, HTTP union parity,
                                  recurring conversational creation
21                24         383  integrated episodes, causal anticipation,
                                  high-recall direct and hypothesis formation,
                                  evidence-based forgetting and activation
```

Two facts fall out of the table and both are worth stating rather than
leaving for someone to notice.

1.  **No milestone with work in it adds zero gates.** Milestones 6, 8,
    and 10 were the three exceptions, each resting entirely on its own
    section's criteria in the engineering plan.
    [sandbox-isolation.md](sandbox-isolation.md) gave Milestone 6
    eleven, and [skills.md](skills.md) gave Milestone 8 ten and
    Milestone 10 six. The memory-formation specification later gave
    Milestone 10 fifteen more, for twenty-one total. That is what milestones
    that add a new trust boundary, a new context class, and new write paths
    should carry. Milestone 8's MCP half was the last to be covered: it
    contributed corpus members to gates 2 and 5 of the tool system and
    nothing of its own, which reads as the right shape for a milestone
    that widens an existing surface until you notice it leaves build
    step 9 unobserved. It now carries seven — six in the tool system
    and one in the harness — and they are the ones that say the widened
    surface is still the same surface.
2.  **Forty-one of three hundred and eighty-three gates are green before
    Milestone 2.** Less than a fifth of the plan's stated invariants are
    checkable against the in-memory slice, and thirteen of them against
    a repository with no agent in it at all. That is the number that
    makes the in-memory tier worth building as real adapters rather
    than as test doubles.

The cumulative column reaches three hundred and eighty-three, which is every
registry entry, at Milestone 21. Six of Milestone 10's gates are
`gate.skill.*`, fifteen are `gate.memory.*`, seven are `gate.web.*`, ten are
`gate.browser.*`, all twenty-three Milestone 11 gates are `gate.schedule.*`,
Milestone 12's twenty are six `gate.device.*` and fourteen `gate.notify.*`,
Milestone 13's twenty-one are `gate.delegate.*`, Milestone 14's twenty-one are
`gate.surface.*`, Milestone 15's sixteen are `gate.ops.*`, Milestone
16's twenty and Milestone 17's ten are `gate.memory.*` again, in the area
those specs already shared, Milestone 18's thirteen are `gate.email.*` in
an area of their own, Milestone 19's five return to the existing
`gate.schedule.*` area, Milestone 20 adds six more there, and Milestone 21
adds twenty-four more to `gate.memory.*`. Every authorized milestone now has a specification
that declares its gates; the roadmap's items add none until the owner
authorizes one and a specification lands for it. Routing remains deferred and
adds none.

## Build-sequence milestones

Six specs left their build sequences untagged. Five are single-
milestone documents where the tag is the section's own milestone; the
sixth is context-engine, whose step 1 moves to Milestone 1 with its
gate.

```text
spec                       steps  assignment
-------------------------  -----  ----------------------------
context-engine                 7  step 1 M1, steps 2-7 M7
event-log-and-persistence      9  all M2, export step M3
memory-formation               6  all M9
memory-retrieval               7  all M9
knowledge-documents            7  all M9
model-gateway                 14  all M3
```

Three specs already tag their build sequences and are unchanged:
`evaluation-harness.md` per step, `policy-and-approvals.md` with a
trailing rule, `tool-system.md` with a trailing rule.

## Tool-system build steps 3 and 4 without a database

Build step 3 is *"Persistence, steps 8, 9, and 13. The schema
additions, the idempotency key, dedup on insert, the claim, the
terminal write."* Steps 1 through 5 are Milestone 1. The engineering
plan says *"Do not implement PostgreSQL persistence … until the
in-memory vertical slice is complete"*, and Section 8.4's idempotency
is a Milestone 2 implement item. Read together those say a Milestone 1
step must write schema that Milestone 1 forbids.

The two readings separate the same way ADR-0024 separated an event
*repository* from event *storage*: one port, two adapters.

1.  **The port and its semantics are Milestone 1.** The idempotency
    key's composition, the four terminal states a row can hold, the
    rule that a duplicate key returns the first result rather than
    executing again, the claim's meaning, and the terminal write's
    exactly-once obligation are all statements about
    `ToolInvocationRepository`, and all of them are asserted by the
    contract suite that both adapters run.
2.  **The DDL is Milestone 2.** *"The schema additions"* names tables
    and a unique index. Those arrive with the rest of the schema, in
    the same migration, under the same review.
3.  **The in-memory adapter satisfies the same contract suite** and
    declares which capability groups it does not satisfy, per
    ADR-0024's checked-in table. It declares one gap: concurrent
    dedup. A dictionary keyed by the idempotency key gives correct
    single-process behaviour and cannot tell the truth about two
    processes racing on an index.
4.  **Therefore gate 8 is Milestone 2** and the Milestone 1 obligation
    is the single-process half, which the contract suite asserts. This
    is the same shape as the absent in-memory `RunQueue`: the parts a
    simulation would lie about are named rather than simulated.
5.  **Step 4's *"This is where Milestone 1's builtins become real"*
    stands unchanged.** Execution, timeouts, the effect watermark,
    output limits, and the recovery *table* are all Milestone 1. What
    Milestone 2 adds is the recovery *test* — gate 7 — because
    recovering requires having survived.

## Cancellation at Milestone 1

`runtime-loop.md` defines `CancellationToken` as a Protocol with
`reason`, `raise_if_cancelled`, and `wait`, and tabulates six
observation points. It assigns *"CancellationToken, points 1-3"* to
Milestone 1. Points 1, 2, and 3 are the loop's own: top of the loop,
before each model attempt, after the model stream closes.

What the assignment does not say is what sets the token at Milestone 1.
All three writers it names — the cancellation poller, the deadline
timer, and the heartbeat supervisor — live in a supervisor task that
reads `runs.cancel_requested_at` in the same query that refreshes the
lease. There is no lease at Milestone 1, no supervisor, and no queue.
Read literally, Milestone 1 ships a token nothing can set.

The three `CancelReason` values do not have the same dependencies, and
separating them resolves it.

```text
reason     needs                          milestone
---------  -----------------------------  ---------
DEADLINE   Clock, run.deadline_at         1
FENCED     lease, lease_epoch             2
REQUESTED  cancel_requested_at or a       2 (poll)
           process signal                 5 (endpoint)
```

1.  **`DEADLINE` is Milestone 1 and needs nothing new.** `Clock` is a
    Milestone 1 port and `runs.deadline_at` is a Milestone 1 column.
    The Milestone 1 token evaluates the deadline lazily inside
    `raise_if_cancelled` rather than being set by a timer, which is
    what makes it work without a supervisor: the loop asks, at each of
    points 1 through 3, whether the deadline has passed.
2.  **The Milestone 1 token has one more writer, and it is the
    process.** The CLI installs a `SIGINT` handler that calls
    `token.cancel(CancelReason.REQUESTED)`. This is what makes
    `Ctrl-C` during `agent run` produce a `CANCELLED` run with a
    terminal event rather than a traceback and a half-written row, and
    it is the only cancellation an operator can issue at Milestone 1.
3.  **`wait()` is on the Protocol from the first commit and returns a
    future that Milestone 1 never completes** except through those two
    paths. The sandbox adapter that selects on it does not exist until
    Milestone 6, and the method exists earlier so that the object
    handed to `ToolExecutionContext` is the same object throughout.
4.  **The effects rule holds at Milestone 1 unchanged.** A
    cancellation observed after `effect_sent_at` is set does not
    abandon the call. At Milestone 1 the only tools are
    `math.calculate` and `system.current_time`, neither of which sets
    a watermark, so the rule is vacuously satisfied and its test is
    gate 12 at Milestone 5. It is stated at Milestone 1 anyway, because
    the code path that would violate it is written at Milestone 1.
5.  **Gate 12 stays at Milestone 5.** It asserts that no case produces
    a cancelled run with a set watermark, and no case can produce one
    until a tool sets one.

This makes the split three-way rather than two-way, and the third part
is the one the milestone table already named without saying what drove
it. Recorded as a question: if the intent was that no cancellation at
all ships before Milestone 5, the `SIGINT` handler is the piece to drop
and points 1 through 3 become dead observation sites for four
milestones.

## What changes in each spec

Mechanical, and listed so the diff is reviewable as a whole.

```text
spec                       heading   form    tokens  metrics
-------------------------  --------  ------  ------  -------
builtin-tools              --        --      9       --
context-engine             rename    bullet  5       split
evaluation-harness         --        --      10      --
event-log-and-persistence  rename    bullet  7       split
memory-formation           rename    bullet  5       split
memory-retrieval           rename    bullet  9       split
model-gateway              --        --      10      --
policy-and-approvals       rename    --      10      split
runtime-loop               --        --      0       --
tool-system                --        --      10      --
```

`tokens` counts the `**M<n>.**` suffixes added; `runtime-loop` shows
zero because it already has fourteen. `metrics` marks the specs whose
tracked metrics move to a sibling `## Tracked metrics` section.

## Hard gates

1.  **Every gate in every spec carries a milestone token.** The docs
    check parses each `## Hard gates` section and fails on a numbered
    item with no trailing `**M<number>.**`. **M0.**
2.  **Every gate identifier in this document appears exactly once in
    `evals/gates/*.yaml`, and every registry entry appears here**,
    compared as sets and not as counts. **M0.**
3.  **Every alias is declared.** A spec's gate count minus its declared
    alias count equals the number of registry entries citing that
    spec's anchor. **M0.**
4.  **Every `spec` field resolves.** Each registry entry's
    `docs/plan/<file>.md#hard-gates` anchor exists in the built site.
    **M0.**
5.  **Every gate identifier matches the grammar** and its area is one
    of the twenty-three. **M0.**
6.  **The census is derived, not written.** A test computes the
    per-milestone counts from the registry and compares them against
    the table in this document, so the table cannot drift. **M0.**
7.  **No gate is registered at a milestone later than the build step it
    observes**, for the specs whose build sequences carry step
    numbers, asserted against the build-sequence table above. **M1.**

## Conflicts this document resolves

1.  **The registry requires `milestone`; nine of ten specs do not
    supply it per gate.** Resolved by supplying it for all
    eighty-nine, deriving each from the build sequence or section
    milestone the spec already states.
2.  **Registry rule 1 requires a parseable gate section; three
    headings and two forms exist.** Resolved by one heading, one form,
    and one suffix, with the registry's own example anchors updated.
3.  **Two specs interleave gates and metrics.** Resolved by the
    specs' own words, moving nine bullets that carry eleven tracked
    metrics to `## Tracked metrics` and leaving fourteen gates. Two of
    the fourteen are trailing `Gate:` sentences that were never
    bullets, and one bullet becomes two gates because it says *"Both
    are hard gates"*.
4.  **The harness's gate table counts memory formation at seven; the
    spec lists eight bullets of which five are gates.** Resolved at
    five. The table was written when the spec's list was read as
    entirely gates, and the spec itself calls four of those bullets
    metrics. The harness table is updated, and its stale sentence
    *"The six written specs"* becomes eleven specs and the engineering
    plan.
5.  **Three gates are declared in two specs each.** Resolved by one
    owner and an explicit alias, so a count check does not
    double-count and a reader does not implement twice.
6.  **`model-gateway` gate 10 skips without credentials; registry rule
    3 forbids skipping at or past a milestone.** Resolved by an
    `optional` field carrying its precondition, used once.
7.  **Tool-system build step 3 places persistence inside an in-memory
    milestone.** Resolved by separating the port's semantics
    (Milestone 1, contract suite) from the DDL (Milestone 2), and by
    naming concurrent dedup as the in-memory adapter's declared gap.
8.  **Milestone 1 ships a `CancellationToken` with no writer.**
    Resolved by a lazily evaluated deadline and a `SIGINT` handler,
    which are the two writers that need nothing Milestone 2 builds.
9.  **Context-engine gates are all Milestone 7 while ADR-0024 assigns
    the builder's determinism test to Milestone 1.** Resolved by
    moving gate 1 and build step 1 to Milestone 1 and leaving the
    other four where they are.
10. **Hard gate 4 requires a `#hard-gates` anchor and the two entries
    the engineering plan owns have no such section to name.**
    Resolved by that gate's own title: what is checked is that the
    anchor an entry names resolves in the built site, and those two
    name the Milestone 0 heading that declares them.

## Decisions

1.  **The milestone is a property of the gate, not of the section.**
    A section-level sentence is prose; the token is data. Four specs
    that carry a section-level milestone keep the sentence and gain
    tokens, and two of those four turn out to have a gate that does
    not match the section.
2.  **A gate lands at the milestone that builds the last thing it
    observes.** This is the rule that produced every assignment above,
    and it is stated so the next gate added has an answer before the
    argument starts.
3.  **A gate whose subject exists earlier than its full assertion is
    registered at the earlier milestone when the earlier form is a
    real assertion**, and at the later one when the earlier form would
    be vacuous. Tool-system gate 5 is the first case; gate 12 of the
    runtime loop is the second.
4.  **Corpus gates are registered once and grown.** Adding an MCP
    server to `gate.tool.no_external_text` at Milestone 8 is a corpus
    change, not a new gate, which is exactly the separation the
    harness introduced the corpus kind for.
5.  **Structural gates restated across specs take the `structure`
    area.** Two do today: the import-boundary walk and transaction
    hygiene. The area exists so that a gate with two declaring specs
    has a home belonging to neither, which is what makes the ownership
    rule above look like a rule rather than a tiebreak.
6.  **Aliases are declared, not deduplicated silently.** The
    non-owning spec keeps its sentence, because a reader of the tool
    system should learn that the import boundary is checked; what
    changes is that the sentence says it is the same gate.
7.  **The `optional` field is added for one gate and its use is
    bounded**: a gate may be optional only if its precondition is an
    external credential, and the registry records the precondition. A
    second use is a design smell and should be argued in review.
8.  **The census is generated.** A written table drifts; a derived one
    fails the build when it disagrees. The table above is the expected
    output, not the source.
9.  **Milestones 6, 8, and 10 adding no gates was reported, not
    fixed.** The honest finding was that their acceptance rested on
    the engineering plan's own criteria, and inventing gates to fill a
    column would have been worse than naming the shape. All three
    zeros were closed the way the decision implies they should be: by
    specifications that had gates to declare, not by the column —
    [sandbox-isolation.md](sandbox-isolation.md) for Milestone 6 and
    [skills.md](skills.md) for Milestones 8 and 10. The decision
    stands for the next milestone that shows a zero. Milestones 12
    through 15 each showed one on the day they were authorized and
    each closed it the way the decision implies, with a specification
    that had gates to declare. Milestones 16 and 17 never showed one:
    each milestone's authorization and its specification landed in the
    same change, so the rows they added were already twenty and ten.
10. **Milestone 1's cancellation is `SIGINT` plus a lazy deadline.**
    Both are cheap, both exercise the observation points from the
    first commit, and neither requires the queue. The alternative —
    three dead observation points until Milestone 5 — costs the same
    to write and is untested when it matters.
11. **The idempotency port is Milestone 1 and its index is Milestone
    2.** One port, two adapters, one contract suite, one declared gap.
    This is ADR-0024's repository-versus-storage separation applied a
    second time, and its second application is what makes it a rule
    rather than a special case.
12. **This document is a scheduling document and owns no requirement
    about the agent.** If a gate's statement is wrong, the fix belongs
    in the spec that declares it, and this document's table follows.
    The seven gates it does declare are checks over the scheduling
    record itself, which is the one thing no other document is in a
    position to check.
13. **Hard gate 7 is asserted against the build-sequence table and
    not against a step recorded per gate.** Only the tool system
    records which step each of its gates observes, and two of its
    rows sit at Milestone 2 against a Milestone 1 step under
    decision 3 above, because the earlier form of each would be
    vacuous: nothing recovers before there is persistence to recover
    from, and a single-process dictionary cannot lose a race. A
    check reading that column per gate would fail on both and be
    relaxed within a milestone. The table is the input, and the
    question it answers is whether a spec's gates outrun the steps
    that build them.

## Open questions for review

1.  **Whether cancellation should exist at Milestone 1 at all.** The
    runtime loop already recorded the M1/M4/M5 split as a question. The
    answer here — `SIGINT` and a lazy deadline — is the cheapest thing
    that makes the split real rather than notional. If the intent was
    that nothing cancels before Milestone 5, drop the handler and the
    deadline check and points 1 through 3 become unreachable until
    then. Reversal cost: low, one handler and one predicate.
2.  **The original Milestone 9 memory-formation count is reconciled.**
    The old harness table said seven, but the formation specification
    classifies four of its original bullets as tracked metrics and declares
    four hard gates plus the trailing no-policy-regression gate. Milestone 10
    memory maturation later added fifteen more: five for ordinary-conversation
    formation and ten for governed inspection and provider assistance. The
    current formation census is therefore twenty owned gates — five at
    Milestone 9 and fifteen at Milestone 10 — with no unstated threshold turning
    formation precision or rejection rate into a gate.
3.  **Whether Milestone 8 should acquire gates of its own** —
    answered yes, by [skills.md](skills.md), which gave it ten and
    gave Milestone 10 its first six. The sentence about a skill doing something
    no gate was watching turned out to describe a real hole, as the
    same sentence about the sandbox had. The MCP half was answered
    the same way on two later passes: three gates when this census
    made build step 9 the only unobserved step in the tool system,
    and three more when the readiness review found nothing behind
    `credential_ref`. It registers six invariants of its own now,
    rather than corpus members in two gates belonging to the
    pipeline it widens.
4.  **Whether the `optional` field is worth its precedent.** One gate
    uses it. The alternative is a live smoke test that is not a gate
    at all but a manually run script, which is honest about its status
    and loses the registry's record of it.
5.  **Whether the engineering plan should grow a `## Hard gates`
    section.** It owns two registry entries and is the only
    declaring document without one, which is why their `spec` field
    is the single exception to the anchor rule. Giving it the
    section would make the rule uniform, and would also move two
    requirements out of the acceptance criteria that state them and
    into a heading a milestone-ordered document has no natural place
    for. The exception is recorded instead. Reversal cost: low, one
    section and two `spec` fields.
5.  **Whether the census belongs in a document at all** once it is
    generated. Keeping it here makes the shape of the plan visible to
    a reader; generating it makes it correct. The compromise taken —
    written here, asserted by gate 6 — costs one test and one edit
    whenever the numbers move.
