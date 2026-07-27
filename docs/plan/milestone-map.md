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
area. The counts throughout this document are the corpus as it now
stands: one hundred and twelve declared across twelve specs, one
hundred and eighteen registry entries.

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
    trailing `**M<digit>.**`, and fail if any item lacks one. This is
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
      event, context, memory, harness, api, sandbox
slug  lowercase, underscore-separated, unique within its area
```

The area is not the filename. `structure` exists because three gates —
the import-boundary walk, the transaction-hygiene check, and the secret
scanner — are structural statements about the repository that no single
subject spec owns, and `memory` covers both memory specs because
formation and
retrieval share a harness and their gates cross-reference each other.

`sandbox` is the twelfth and it follows the `memory` precedent rather
than the `structure` one: one spec owns all thirteen, and they are
statements about a subject rather than about the repository. The
alternative considered was a `security` area, which was rejected
because an area names a subject and security is a property of many of
them — the secret scanner, the import boundary, and the cross-tenant
404 would all have a claim on it, and none of them belongs with a
sandbox escape.

## Ownership: the three gates declared twice

Three statements appear as gates in two specs each. Each gets one
registry entry, one owner, and an explicit alias so that a reader who
finds it in the non-owning spec knows it is not a second gate.

```text
gate id                          owner            also stated in
-------------------------------  ---------------  -----------------
gate.structure.import_boundary   engineering      tool-system #6
                                 plan, M0
gate.structure.txn_hygiene       event-log #7     runtime-loop #6
gate.event.checkpoint_dispens..  event-log #6     runtime-loop #9
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

One hundred and twelve gates declared across twelve specs, two more
declared in the engineering plan, and seven this document declares over
the corpus: one hundred and twenty-one declarations, one hundred and
eighteen registry entries once the three aliases are subtracted. Each
table gives the gate's number in its own spec, its registry identifier,
its kind, and its milestone.

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

### Runtime loop, fourteen gates

Already tagged per gate. Reproduced here so the map is complete and so
the two aliases are visible.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.runtime.one_terminal_wr..  structural   1
2   gate.runtime.no_ambient_time    structural   1
3   gate.runtime.no_ambient_id      structural   1
4   gate.runtime.step_identity      case         1
5   gate.runtime.budget_stops       case         1
6   (alias of gate.structure.txn_hygiene)        2
7   gate.runtime.lease_once         case         2
8   gate.runtime.fenced_no_write    case         2
9   (alias of gate.event.checkpoint_dispensable) 2
10  gate.runtime.resume_idempotent  case         2
11  gate.runtime.waiting_holds_no.  case         4
12  gate.runtime.cancel_keeps_eff.  case         5
13  gate.runtime.build_fits         property     7
14  gate.runtime.build_stable       property     7
```

### Tool system, ten gates

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
```

Three of these need their reasoning stated, because the milestone is
not the one a first reading gives.

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

### Builtin tools, nine gates

All nine observe `math.calculate` or `system.current_time`, both of
which ship at Milestone 1.

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
```

### Model gateway, ten gates

The section already says Milestone 3 for all ten, and the build order's
fourteen steps carry no tags because all fourteen are Milestone 3. Gate
10 is the one exception worth naming: it skips when credentials are
absent, which registry rule 3 forbids at or past a gate's milestone.

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
```

Gate 10 is registered with `optional: true`, a field the registry does
not yet have and which this document adds for it alone: a gate that is
allowed to report `skipped` when a named precondition is absent, with
the precondition recorded. Without the field the choice is a forbidden
skip or a gate that fails on every machine without vendor keys, and
both are worse than naming the exception once.

### Policy and approvals, ten gates

All ten are Milestone 4, which the section states and the build
sequence confirms — steps 1 through 11 are Milestone 4 and step 12 is
sequenced separately and is not a dependency.

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
```

### Event log and persistence, seven gates

The section says Milestone 2 for all seven. One qualification, already
decided in ADR-0024: the transaction-hygiene *check* is a Milestone 0
deliverable and its *gate* is a Milestone 2 acceptance criterion. The
registry entry carries Milestone 2, because a gate is the assertion and
not the tool, and Milestone 0's obligation is that the tool exists and
passes against a nearly empty repository.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.event.sequence_integrity   property     2
2   gate.event.projection_determ    case         2
3   gate.event.upcaster_totality    property     2
4   gate.event.exactly_once         case         2
5   gate.event.crash_recovery       case         2
6   gate.event.checkpoint_dispens.  case         2
7   gate.structure.txn_hygiene      structural   2
```

The build sequence's nine steps are Milestone 2, except that the step
naming trajectory export says *"export itself is Milestone 3"*.

### Context engine, five gates

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
```

Gate 2 stays at Milestone 7 because its subject is a scripted
fifty-turn session with a forced compaction, a revoked tool, and a
memory correction — none of which exist before Milestone 7. Gate 1
stays true from Milestone 1 onward and is the reason the builder is
written deterministically from the first commit rather than
retrofitted.

The build sequence's seven steps are Milestone 7 except step 1, which
is Milestone 1 by the same decision.

### Memory formation, five gates and four metrics

The list of eight bullets separates into five gates and four metrics by
what the spec calls them. The trailing sentence *"Gate: memory improves
target eval cases without increasing policy failures"* is a fifth gate,
not a closing remark.

```text
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.memory.contradiction       case         9
2   gate.memory.no_fabrication      corpus       9
3   gate.memory.form_injection      corpus       9
4   gate.memory.correction_durable  case         9
5   gate.memory.no_policy_regress   case         9
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

### Evaluation harness, ten gates

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
milestone.

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
#   id                              kind         M
--  ------------------------------  -----------  --
1   gate.sandbox.production_refu..  structural   1
2   gate.sandbox.no_runtime_in_w..  structural   6
3   gate.sandbox.spec_has_no_hos..  structural   6
4   gate.sandbox.no_credential_r..  case         6
5   gate.sandbox.network_denied     case         6
6   gate.sandbox.egress_allowlis..  case         6
7   gate.sandbox.limits_enforced    case         6
8   gate.sandbox.escape_denied      case         6
9   gate.sandbox.workspace_isola..  case         6
10  gate.sandbox.no_orphans         case         6
11  gate.sandbox.artifact_checksum  case         6
12  gate.sandbox.artifact_key_op..  structural   6
13  gate.sandbox.workspace_conta..  property     4
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
0                 12          12  the import boundary, the secret
                                  scanner, and ten checks over
                                  documents
1                 28          40  the vertical slice, both
                                  builtins, deterministic build
2                 12          52  persistence, fencing, resume
3                 11          63  five adapters, trajectories
4                 13          76  policy, approvals, corpora
5                 11          87  the API surface, the stream,
                                  cancellation keeps effects
6                 11          98  isolation, egress, artifacts
7                  6         104  budgeting and compaction
8                  0         104  --
9                 14         118  formation and retrieval
```

Two facts fall out of the table and both are worth stating rather than
leaving for someone to notice.

1.  **Milestone 8 adds no gates.** Every gate its work strengthens is
    already registered against an earlier milestone: the MCP work adds
    corpus members to gates 2 and 5 of the tool system. This is a
    finding, not a design. It says Milestone 8 is the one whose
    acceptance rests entirely on its own section's criteria in the
    engineering plan, and it is worth a look during that milestone's
    planning to decide whether that is right. Milestone 6 was the
    other until [sandbox-isolation.md](sandbox-isolation.md) was
    written; it now carries eleven, which is what a milestone that
    creates a new trust boundary should carry.
2.  **Forty of one hundred and eighteen gates are green before
    Milestone 2.** More than a third of the plan's stated invariants are
    checkable against the in-memory slice, and twelve of them against a
    repository with no agent in it at all. That is the number that
    makes the in-memory tier worth building as real adapters rather
    than as test doubles.

The cumulative column reaches one hundred and eighteen, which is every
registry entry, at Milestone 9. Milestones 10 and 11 add none: scheduling,
routing, and subagents are covered by gates registered against the
runtime loop and the policy engine, and the same question this document
raises about Milestone 8 applies to them.

## Build-sequence milestones

Five specs left their build sequences untagged. Four are single-
milestone documents where the tag is the section's own milestone; the
fifth is context-engine, whose step 1 moves to Milestone 1 with its
gate.

```text
spec                       steps  assignment
-------------------------  -----  ----------------------------
context-engine                 7  step 1 M1, steps 2-7 M7
event-log-and-persistence      9  all M2, export step M3
memory-formation               6  all M9
memory-retrieval               7  all M9
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
    item with no trailing `**M<digit>.**`. **M0.**
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
    of the twelve. **M0.**
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
9.  **Milestones 6 and 8 adding no gates is reported, not fixed.**
    The honest finding is that their acceptance rests on the
    engineering plan's own criteria, and inventing gates to fill a
    column would be worse than naming the shape. Milestone 6's zero
    was closed the way the decision implies it should be: by a
    specification that had gates to declare, not by the column.
    Milestone 8's remains open and waits on the same thing.
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

## Open questions for review

1.  **Whether cancellation should exist at Milestone 1 at all.** The
    runtime loop already recorded the M1/M4/M5 split as a question. The
    answer here — `SIGINT` and a lazy deadline — is the cheapest thing
    that makes the split real rather than notional. If the intent was
    that nothing cancels before Milestone 5, drop the handler and the
    deadline check and points 1 through 3 become unreachable until
    then. Reversal cost: low, one handler and one predicate.
2.  **Whether memory formation has five gates or seven.** The harness
    table said seven and this document says five, on the strength of
    the spec calling four of its eight bullets metrics. If the intent
    was that formation precision and rejection rate block the
    milestone, they are gates and need thresholds, which no document
    states. Reversal cost: low now, high after the harness is written
    against one reading.
3.  **Whether Milestone 8 should acquire gates of its own.** It does
    substantial work — skills and MCP — and adds no registered
    invariant. It may be right, since its risks are covered by gates
    registered earlier, and it may be an omission that only shows up
    when a skill does something no gate was watching. Milestone 6 was
    the other half of this question and it is answered: it has eleven,
    and the sentence about something in a sandbox going wrong with no
    gate watching turned out to be describing a real hole.
4.  **Whether the `optional` field is worth its precedent.** One gate
    uses it. The alternative is a live smoke test that is not a gate
    at all but a manually run script, which is honest about its status
    and loses the registry's record of it.
5.  **Whether the census belongs in a document at all** once it is
    generated. Keeping it here makes the shape of the plan visible to
    a reader; generating it makes it correct. The compromise taken —
    written here, asserted by gate 6 — costs one test and one edit
    whenever the numbers move.
