---
title: Context Engine
status: design
canonical: true
---

# The context engine

This document specifies how a run's model request is **assembled**: which content
goes on which side of the prompt cache boundary, in what order, under what token
budget, with what trust labels, and what yields when the budget binds. It expands
[Section 11](engineering-plan.md) of the engineering plan, sits under Milestone 7,
and is recorded as [ADR-0020](../adr/0020-context-engine.md).

Scope: **assembly**. What to remember is specified in
[memory formation](memory-formation-and-consolidation.md); what to recall is
specified in [memory retrieval](memory-retrieval-and-ranking.md). This document
owns the container both of those write into, and the guarantees that container
makes to everything else.

## Why assembly is its own problem

Section 11 currently describes the context builder as a list of seven things to
concatenate. That understates it. The builder is the component that every other
subsystem hands its output to, and it is the only one that can violate all of their
guarantees at once:

- **It is the cache boundary.** Section 10.1 fixes a prompt-stability invariant —
  a byte-stable prefix built once per session — and names the context builder as
  the thing that enforces it. Nothing else can. A single volatile byte in the
  prefix converts the cheapest request in the system into the most expensive one,
  silently, for the rest of the session.
- **It is the trust boundary in practice.** Section 11.2 assigns every item a
  `TrustLevel`, but a label only means something at the moment content is rendered
  into a prompt. Compaction, summarization, and truncation all pass through the
  builder, and each is an opportunity to strip a label off content that still
  carries its original risk.
- **It is where scarcity is resolved.** Memory, history, tool definitions, tool
  results, and working state all want the same tokens. Every other subsystem sizes
  its own appetite; only the builder decides who actually eats.

The failure mode is not "the prompt was wrong." It is that all three failures above
are **invisible in the output**. A cache-thrashing session returns correct answers
at ten times the cost. A laundered label returns a correct-looking answer derived
from attacker-controlled text. A budget overrun returns a provider 400 twenty
minutes into a long run. This spec optimizes for **assembly that fails loudly at
build time** rather than expensively at run time.

## What the context engine must respect

These are fixed by earlier decisions and are not re-litigated here.

- **Prompt-stability invariant (Section 10.1).** The cacheable prefix — platform
  policy, agent instructions, tool definitions — is built once per session and kept
  byte-stable. Volatile content goes in the user turn.
- **`build()` is a port (Section 7).** `ContextBuilder.build(run, checkpoint, agent,
  principal) -> ModelRequest` is the existing signature and this spec does not
  change it. Everything below is a statement about what that function must do.
- **The session owns history, the run owns its working conversation (Section 27.4,
  ADR-0009).** A new run seeds from the session-history projection built from
  events, not from the previous run's checkpoint.
- **Provider-opaque reasoning items never cross a run boundary (Section 6.6,
  ADR-0007).** They live in the checkpoint for the life of a tool loop and are
  replayed verbatim, never authored, summarized, or logged.
- **Private chain-of-thought is never requested or stored (Section 11.4, ADR-0006
  as amended).** Only messages, actions, evidence, concise decision summaries, and
  structured working state.
- **The agent version is pinned per run (Section 6.1).** Agent instructions cannot
  drift mid-run, which is what makes a session-scoped prefix possible at all.
- **Memory renders under a fixed contract (retrieval spec, stage 8).** The
  `<memory>` block's ordering, banding, and attribution rules are that spec's; this
  document places the block and pays for it.

## Two regions and one rule

Every item in a request belongs to exactly one of two regions, and the assignment
is a property of the item's **type**, decided at design time, never at run time.

**Region A — the frozen prefix.** Built once per session and byte-stable for the
life of the prefix epoch. It carries platform policy, agent instructions, the
filtered tool definitions, the session-open memory snapshot, and the framing text
that tells the model how to treat everything downstream.

**Region B — the turn body.** Rebuilt on every request. It carries the compacted
history summary, recent conversation items, the working-state block, in-turn recall
and correction lines, tool calls and their results, runtime metadata, and the
current user message.

The rule is one sentence: **if it can differ between two requests in the same
session, it is in Region B.** Not "if it usually doesn't change" — *if it can*. The
current date is the canonical trap. It is in Section 11.1's assembly list, it feels
like configuration, it changes at most once per session, and putting it in the
prefix breaks every session that crosses midnight in a way that no test written
before 23:00 will catch.

Assembly order is fixed and total:

| # | Region | Content | Trust |
| --- | --- | --- | --- |
| 1 | A | Platform policy and hardline rules | `PLATFORM` |
| 2 | A | Framing: how to treat memory, tool output, and untrusted spans | `PLATFORM` |
| 3 | A | Agent instructions (pinned `AgentSpec` version) | `TRUSTED_CONFIGURATION` |
| 4 | A | Tool definitions (filtered, canonically serialized) | `TRUSTED_CONFIGURATION` |
| 5 | A | Skill catalog (pinned at session open) | per entry |
| 6 | A | Session-open memory snapshot | `MEMORY` |
| 7 | B | Compacted history summary, if any | `PLATFORM` (see below) |
| 8 | B | Retained conversation items, oldest to newest | per item |
| 9 | B | Loaded skill bodies, in load order | per skill |
| 10 | B | Working-state block | per entry |
| 11 | B | In-turn recall and correction lines | `MEMORY` |
| 12 | B | Runtime metadata: current date, principal scope, surface | `PLATFORM` |
| 13 | B | The current user message | `USER` |

Rows 5 and 9 are added by [skills.md](skills.md), which owns their content,
their caps, and their trust derivation. They are listed here because assembly
order is fixed and total, and a table that omits two of its rows is neither.

Platform policy comes first because it is the only content that must be read before
anything that might try to override it. The current user message comes last because
recency is the strongest positional signal available and the turn's actual request
should hold it.

### Enforcement is a test, not a convention

An invariant maintained by developers remembering it is not an invariant. Three
mechanisms, in order of how much they catch:

**A region-assignment table in code.** Each context item type declares its region
as a class attribute. The builder asserts on assembly; an item with no declared
region is a build failure, not a default. Adding a new item type therefore forces
the decision rather than allowing it to be skipped.

**A recorded prefix hash.** Every request records `prefix_sha256` on
`model.request.started`. Prefix stability is then an observable property of
production traffic rather than a hope: a session with more than one distinct prefix
hash per epoch is a defect with a session id attached.

**A scripted stability test in CI.** Drive a fifty-turn session against the fake
provider (Section 10.3) with the clock advanced across a day boundary, tools
partially revoked mid-session, memory written and corrected mid-session, and a
forced compaction. Assert exactly one distinct `prefix_sha256`. This is a hard gate
on the milestone.

### Authorization changes do not rewrite the prefix

Section 11.1 says filter tools by agent configuration, principal authorization,
policy profile, and runtime environment. Three of those are session-stable. The
fourth is not: a scope can be revoked while a session is open, which would change
the tool list, which would rewrite the prefix.

Resolve it by separating the *advertisement* of a tool from the *authorization* to
call it. **The tool set is resolved once at session open and pinned in the context
plan.** A tool whose authorization is later revoked stays in the prefix and is
denied at call time by the policy engine (Section 9), which is where the security
boundary actually lives — a tool definition in a prompt grants nothing. The denial
is structured and the model is told plainly that the capability is no longer
available, which is information it can act on.

This is deliberately the safe direction to be wrong in. The failure it permits is a
wasted model call that ends in a clean denial. The failure it prevents is a
prefix rewrite, and a prefix rewrite on every permission change is both a cost
regression and a covert channel — the shape of the tool list would otherwise leak
authorization changes into the cache timing of an unrelated session.

### Prefix epochs

Some changes genuinely cannot be absorbed. Routing the session to a different model
or provider invalidates the prefix by construction: tokenizers differ, tool
serialization differs, and Section 10.1 notes that changing tool definitions or
thinking parameters invalidates downstream cache anyway. An operator revoking a
capability for a security reason may need it gone from the prompt immediately, not
merely denied at call time.

These start a **new prefix epoch**: the plan is rebuilt, `epoch` increments,
`context.epoch.rotated` is emitted with a reason, and the full prefix is re-cached
at known cost. Epochs are explicit, logged, and counted. **Epochs per session is a
tracked metric and its target is 1.0**; a deployment averaging materially more than
one has a configuration problem, and because the counter exists, it has one
visibly.

## The budget allocator

Section 11.3 specifies `ContextBudget` as seven integers and requires an allocator
rather than string concatenation. It does not say where the integers come from or
what happens when they do not fit. Both are decided here.

### Only history scales with the window

Total capacity is `min(model.max_context_tokens, policy_cap)`. The output reserve
comes off the top as a **subtraction, not a share** — a request that fits but
leaves no room to answer is a failed request that costs full price. What remains is
divided among classes, and the division follows one principle:

**Every class except history is capped absolutely, and only history scales with the
context window.**

This generalizes the argument the retrieval spec makes about the memory snapshot.
Prefix content is read on every request of the session, so its cost is attention
paid repeatedly, and attention degrades with the absolute number of items competing
for it — not with the fraction of the window they occupy. A larger window is not a
reason for a longer policy document, more tools, or a bigger snapshot; those get
worse as they grow, and the window has nothing to do with it. History is the one
class whose value genuinely increases with size, because more history is more of
the actual conversation. So history takes the remainder and everything else takes a
ceiling.

| Region | Class | Cap | Scales | Yields |
| --- | --- | --- | --- | --- |
| A | Platform policy | 2,000 | No | Never — fails at plan time |
| A | Agent instructions | 4,000 | No | Never — fails at plan time |
| A | Tool definitions | 30 tools / 6,000 tokens | No | Only at an epoch boundary |
| A | Skill catalog | 20 skills / 1,500 tokens | No | Only at an epoch boundary |
| A | Memory snapshot | 40 items / 1,500 tokens | No | Only at an epoch boundary |
| B | Skill bodies | 2 loaded / 6,000 tokens | No | Never — the load fails instead |
| B | Working state | 1,000 | No | Never |
| B | Knowledge passages | 3 passages / 3,000 tokens | No | First |
| B | In-turn recall | 2,000 | No | Second |
| B | Tool results | 25% of body | Partly | Third |
| B | History | remainder, floor 8,000 | Yes | Fourth |
| B | Current user message | uncapped | — | Never |
| — | Output reserve | 8,192 or the model's default | No | Never — subtracted first |

Tool definitions carry an **item cap as the primary limit**, exactly as the snapshot
does and for exactly the same reason: selection accuracy degrades with the number of
candidates, not with their token weight. Thirty tools is already generous; a
deployment that needs more needs tool filtering or skills (Section 30.4, where only
skill metadata enters ordinary context), not a bigger allowance. The skill catalog
carries an item cap for the same reason and is capped at twenty;
[skills.md](skills.md) argues that number and the 6,000-token body class beside
it, which never yields because a third `skill.load` fails instead.

Knowledge passages are a separate class from in-turn recall rather than a share of
it, and [knowledge-documents.md](knowledge-documents.md) argues both the split and
the number. A passage is a verbatim quotation the model may cite, so it is three
times the weight of a belief and cannot be trimmed by sentence; the class is capped
at three passages because a document that answers a question usually answers it in
one or two, and it yields before recall because a corpus is re-queryable by an
explicit `knowledge.search` while the beliefs in a snapshot are not.

The prefix classes sum to a hard ceiling of 15,000 tokens. If a plan exceeds it,
**the session fails to open with a structured error naming the offending class**.
It does not silently truncate the agent's instructions. A truncated system prompt
is an agent that behaves subtly wrong forever, which is far worse than a session
that refuses to start with a clear reason.

### What history selection retains

The yield order below says which class gives up tokens first. It does not say
which conversation items were in the request to begin with, and that is the
decision that determines whether two runs with the same input produce the same
prompt. Selection happens at two moments, on two different inputs, and they are
two different functions.

**At run seed, the input is a log prefix, not a projection.** The session-history
projection is a live read model that advances from its watermark on a timer;
asking it for "the session's history" returns whatever has been applied at the
moment the question is asked. `seed_checkpoint` is called twice — once when the
application service creates the run, and again when `CheckpointRepository.latest`
returns `None` because the Milestone 2 dispensability gate deleted the run's
checkpoints — and the two calls can be hours and a deploy apart. Reading the
projection as it stands would make the second seed a different conversation from
the first, which is precisely the failure that gate exists to catch and would
instead be causing.

So the seed reads the projection **cut at a fixed sequence**: the session sequence
of the `user.message.created` event the run answers, recorded on the run row when
the run is created.

```text
# additive column on the runs table
runs.seed_event_sequence  BIGINT NULL   -- session sequence of the seeding
                                        -- message; history is every item
                                        -- strictly below it
```

The column is written in the transaction that appends the seeding event and
inserts the run row — the transaction that already allocates the sequence, so it
costs nothing. It is nullable for the child runs of Section 27.6, which seed from
a parent's concise instruction rather than from session history and therefore
select over an empty input, which is the same function rather than a branch in
the caller.

Projections are already required to be deterministic over a log prefix. Pinning
the prefix is what turns that property into a guarantee about seeding, and it
needs no gate of its own: the dispensability gate is the test, and it only tests
anything because the cut is fixed.

**At assembly, the retained set is a suffix, never a subset.** History is selected
as a contiguous tail of the ordered item list — one cut index, everything at or
after it in, everything before it out. Not a relevance ranking over past turns,
and not "the important ones". Three reasons, in order of how much they cost to
get wrong.

A non-contiguous selection produces a conversation with holes, and a model reading
a hole does not see a hole. It sees a conversation in which the thing that filled
the gap never happened, and reasons confidently from that. The absence is
invisible in the transcript and expensive in the answer.

Contiguity also makes tool-pair atomicity a property of the cut rather than a
pass that runs after it. With a suffix there is exactly one boundary that can
split a pair, so the repair is one adjustment to one index. With a ranked subset
any pair can split, and the repair changes the token total, which can require a
second repair.

And a relevance-ranked history would be a second retrieval system — a second
ranker, a second set of tuning parameters, a second set of failure modes — beside
the one this corpus already has. In-turn recall exists precisely to pull back the
older thing that matters. **History is recency; recall is relevance.** Collapsing
them makes both untestable, because a missing turn is then either a selection
defect or a ranking miss and no test can tell which.

**The cut is the largest suffix that fits.** Four rules compute it, and each is
total:

1. Scan backward from the newest item, accumulating estimated tokens, and stop at
   the first item whose inclusion would exceed `budget.history_tokens`. The floor
   is applied by the allocator before the predicate runs, so the predicate reads
   one number and never re-derives it.
2. The cut never falls earlier than `replaced_through_sequence`. Items the
   summary already covers are represented at position 7; admitting them again
   states the same turns twice in two voices, and the paraphrase and the original
   will disagree about emphasis.
3. If the cut splits a tool pair, it moves **later**, past the orphaned result, so
   the pair is excluded as a unit. Never earlier: admitting the call would add
   tokens to a set already at its limit, and the pair is atomic in both
   directions.
4. The never-yield items are not subject to the cut at all. The current user
   message, the working-state block, the correction lines, and the pending pairs
   of an active loop are assembled first and their cost is subtracted before the
   scan begins. If they alone exceed the class, the request fails — the same rule
   the prefix follows, for the same reason.

```python
def select_history(
    items: Sequence[ConversationItem],   # ordered, oldest first
    summary_floor: int,                  # replaced_through_sequence, or 0
    history_tokens: int,                 # budget.history_tokens, floor applied
    estimator: TokenEstimator,
    model_id: str,
) -> int: ...                            # cut index; items[cut:] are retained
```

**`summary_floor` is an event sequence; the return is an item index.** The two
are different units and the function never compares them to each other. Each
`ConversationItem` carries the sequence of the event it was built from, and the
floor is applied by comparing *that* to `replaced_through_sequence`: an item at
or below the floor has been replaced by the summary and is not a candidate. The
index the function returns is then a position in `items`, derived after the
floor has been applied, never a sequence number in disguise.

**It returns an index, not a list.** An index can only describe a suffix, so the
contiguity rule is carried by the return type instead of by a test somebody has
to remember to write.

**The estimator is pure.** `TokenEstimator.estimate` is permitted to be
approximate and is now also required to be a pure function of its arguments — no
clock, no sampling, and no cache that can change the number it returns rather
than the time it takes to return it. An approximate estimator puts the cut in a
slightly different place than an exact one would, which is a tuning question. A
non-deterministic estimator puts the cut in a different place on two calls with
the same input, which is the failure this whole subsection exists to prevent.

### Yield order under pressure

When the assembled body will not fit, the builder yields in a fixed order, taking
the cheapest and most recoverable loss first:

1. **Knowledge passages drop, lowest-ranked first.** They are dropped whole and
   never truncated, because a passage shortened to fit is a misquotation of a
   document the model is about to cite. They go first because the corpus is still
   there: an explicit `knowledge.search` re-reaches it in one tool call.
2. **In-turn recall trims to its floor.** It is the marginal addition, it was
   selected against a relevance floor that can simply be raised, and it is the most
   recoverable thing in the request — the agent can call `memory.search` explicitly
   if it turns out to need it.
3. **Tool results truncate to pointers, oldest first.** The full result is already
   in the event log and, above the inline threshold, in the artifact store. What
   remains is a typed pointer with the byte count and reference, so the model can
   see that content exists and ask for it rather than concluding it never existed.
4. **History compacts.** Deliberately last: it is the only step that costs a model
   call on the critical path, and the only one that loses information the run
   cannot cheaply re-fetch.

Four things never yield, and the builder fails rather than dropping them: the
current user message, the working-state block, the correction lines that override
the frozen snapshot, and the pending tool-call/result pairs of an active loop.

**Tool call and result items are atomic budget units.** A `ToolCallItem` without its
`ToolResultItem` is not a smaller request, it is a malformed one — both providers
reject the sequence — and the failure surfaces as a provider 400 that reads like a
transport bug. The allocator treats the pair as one indivisible item and a
validator rejects any assembled request containing an orphan.

### Pressure is resolved by a write, never inline

The single most important property of the builder is that **`build()` is a pure
function of its inputs**. Called twice on the same checkpoint, it returns
byte-identical bytes. That is what makes retries safe, makes the stability test
possible, and makes a request reproducible from the log.

Compaction is a model call, and model calls are not pure. So compaction is **not
something `build()` does**. The loop measures pressure before the call; if the body
will not fit, it invokes the compactor, which **writes a new checkpoint** carrying
the summary, and then calls `build()` again on that checkpoint. Section 6.9 already
lists context compaction as a checkpoint trigger; this is the reason it is one.

The separation is what keeps assembly testable. A builder that summarizes inline is
a builder whose output cannot be predicted, cannot be diffed, and cannot be asserted
byte-stable — and prefix stability is exactly an assertion about bytes.

### The token estimator

Section 11.3 permits an approximate estimator behind a replaceable interface. Four
constraints on it:

**It is conservative by construction.** Over-estimating wastes headroom.
Under-estimating produces a provider context-length error mid-run, which on a long
async run means losing real work. The estimator rounds up, and the allocator holds
a further safety margin (5% by default) on top.

**It never covers the prefix.** The prefix is measured **exactly, once, at plan
time** by rendering it and counting, and the count is stored in the plan. Only the
elastic body is estimated per request. This confines estimator error to the part of
the request that has slack, and it means prefix-size failures are caught at session
open rather than on turn forty.

**It is per-model.** Tokenizers differ, and the interface takes a model id. A shared
character-ratio heuristic is an acceptable first implementation; a wrong one applied
uniformly across providers is not.

**It reconciles against reality.** Every `model.response.completed` carries actual
usage. The estimator maintains a per-model correction factor from the observed
ratio, which converges quickly and, more usefully, makes estimator drift a visible
number rather than a mystery. The factor resets when the model changes.

## Compaction

Section 11.4 gives the first compactor a correct retention list — current goal,
explicit constraints, unresolved questions, tool results needed downstream, source
event ids — and says not to build anything sophisticated before the basic loop
works. Agreed. What it does not say is how compaction coexists with a prefix that
must never be rewritten, and that is where naive implementations break the invariant
without noticing.

### Compaction only ever touches Region B

The prefix is not compactable content. It contains no history. A compactor that
"summarizes the conversation so far" and rebuilds a system message from the result
has just rewritten the prefix, and the resulting cache miss will be attributed to
whatever change shipped that week rather than to the compactor.

Compaction replaces the **oldest end of the body** with a summary item that sits at
position 7 in the assembly order. Section 10.1 places a rolling cache breakpoint
over the last few non-system messages; compaction invalidates that rolling window
and nothing above it. The cost of a compaction is therefore bounded and known in
advance: re-cache the history window, keep the prefix.

### Untrusted content is elided, never paraphrased

This is the load-bearing rule and it is a security rule, not a quality one.

Summarization launders trust labels. A conversation containing a tool result marked
`EXTERNAL_UNTRUSTED` — a fetched web page, an inbound email, a third-party API
response — gets compacted into a paragraph of fluent narration, and that paragraph
is not labeled anything. It reads as the platform's own account of what happened.
Every downstream defense in Section 11.2 depends on the label, and the label is now
gone. Worse, the injection that was inert while quoted as data is now embedded in
text the model has been told to treat as a trustworthy record of the session.

So: **the summarizer runs on trusted content only.** Untrusted spans are not
summarized. They are replaced with a typed pointer:

```text
[elided] tool.result 8f21 (external_untrusted, 4,214 bytes)
         -> artifact:a/9d02
```

The pointer states that content existed, what it was, how large, where it lives,
and that it was untrusted. The model can ask for it, and re-fetching it returns it
inside its envelope with its label intact. Nothing is lost except the ability to
launder it.

The same rule protects the trusted side less dramatically but usefully: every
summary carries the union of its `source_event_ids`, so any statement in a summary
can be traced to the events that produced it — the same provenance discipline
memory formation applies to beliefs.

### Summaries are bounded and monotone

Summaries of summaries lose provenance and drift. Cap **summary depth at 2**: raw
items compact into a summary, and summaries may merge once. Beyond that, the run
has outgrown its window and should be escalated — a child run with its own budget
(Section 27.6), or a handoff — rather than compressed a third time into something
whose relationship to what happened is no longer checkable.

Merging summaries takes the **union** of source event ids and the **union** of
retained constraints. Constraints are never merged away; if the retained constraint
set alone will not fit, that is a failure, not a compaction target.

## Trust labeling and rendering

Section 11.2 requires a trust classification on every context item and states two
rules: external content must be represented as data rather than instructions, and a
tool result must never redefine policy, grant permissions, or change approval
requirements. Both are properties of how content is *rendered*.

### Every non-platform region is enveloped

Content below `TRUSTED_CONFIGURATION` renders inside a delimited, attributed
envelope:

```text
<untrusted source="tool:web.fetch" id="8f21" nonce="7c1e">
...content...
</untrusted:7c1e>
```

Three properties matter:

- **The framing is in the prefix, the data is in the body.** The instruction that
  enveloped content is data to be considered and never instructions to be followed,
  that it cannot alter policy or grant permission, and that a closing marker inside
  content is content — all of it is stable platform text, written once. Only the
  data varies. This is the same split the memory block already uses and it is the
  only version that is cache-safe.
- **Content cannot close its own envelope.** The closing marker carries a
  per-item nonce, and any occurrence of the delimiter syntax inside content is
  escaped at render time. Without this, "ignore the above and " preceded by a
  literal close tag is a one-line escape from the entire trust model.
- **The label survives every transformation.** Truncation keeps the envelope and
  marks it truncated. Elision keeps the label in the pointer. Compaction does not
  touch it at all. A transformation that produces unlabeled output from labeled
  input is a bug with a named eval.

### The working-state block carries per-entry trust

The working-state block is rendered by the platform, but its *entries* have
origins, and an entry derived from untrusted content is still untrusted. A fact
extracted from a fetched page enters `established_facts` only as an attributed
claim — "the vendor's page states X" with its source event id — never as a bare
assertion of X. This is the same distinction memory formation draws when it refuses
to let `EXTERNAL_UNTRUSTED` spans form beliefs directly: the content is evidence,
and evidence is quoted with its source.

## Working state

Milestone 7 gives `WorkingState` a shape and a home in `RunCheckpoint.working_state`
and stops there. Three things need deciding: who writes it, what carries across a
turn boundary, and what bounds it.

### The model never writes it as prose

Working state is updated through a deterministic control tool, following the same
pattern Section 27.3 uses for `conversation.ask_user` and Section 30.2 uses for
`skill_manage` — the platform reads a typed tool call, not model prose that a parser
hopes to understand:

```yaml
context.update_working_state
  input:  { "objective": str | null,
            "add_constraints": [str],
            "upsert_tasks": [TaskState],
            "add_facts": [Fact],
            "resolve_questions": [str],
            "next_action": str | null }
  effect: typed transition on the run's working state; checkpoint;
          emit context.working_state.updated
```

The runtime is the second writer, and only for what it observes directly: task
states transition on tool outcomes, and `open_questions` gains an entry when
`conversation.ask_user` fires and loses it when the answer arrives. No parsing of
assistant text, ever. Inferring structured state from prose is nondeterministic and
is a prompt-injection surface — the same reasoning that put clarifying questions
behind a control tool.

The tool is read-only outside the run, never requires approval, and **cannot remove
a constraint the user set**. An agent that can quietly drop its own constraints does
not have constraints.

### Carry rules across the turn boundary

A run is a turn (ADR-0009), so working state is per-run — but an objective set on
turn three is obviously still the objective on turn four, and `next_action` from
turn three is obviously stale. Each field carries a rule, applied when the new run
seeds from the session-history projection:

| Field | Carry | Rule |
| --- | --- | --- |
| `objective` | Session | Carries until a user message states a different one; superseded, not silently replaced |
| `constraints` | Session | Append-only within a session; removable only by the user or an explicit tool call |
| `tasks` | Session | Open tasks carry; completed tasks drop from context and stay in the log |
| `established_facts` | Session | Carry with provenance; also a formation input (see below) |
| `open_questions` | Session | Carry until resolved by an answer, never by the model deciding to forget |
| `next_action` | Run | Always reset — it is a within-turn scratchpad |

Carry is computed from the log, not copied from the previous checkpoint, so it
holds the same property Section 27.4 established for conversation: the session
event log is authoritative and the checkpoint is not the system of record.

### Working state is bounded

An unbounded working state is history wearing a schema. Caps: 20 constraints, 30
open tasks, 40 established facts, 20 open questions, and a 1,000-token block
ceiling. Eviction takes completed and stale entries first, by age.

**Constraints never evict.** At the cap, `add_constraints` fails with a structured
error the agent can surface, rather than silently dropping the oldest. Constraints
are precisely the content whose loss the user notices and attributes to the agent
not listening.

### Established facts feed formation

`established_facts` entries are, definitionally, things the session concluded and
carried — which is what the memory write path is looking for. Facts surviving to
the end of a session with trusted provenance are offered to consolidation as
candidates at the session-boundary trigger (formation spec, stage 1). They are
candidates, not beliefs: they enter the ordinary formation pipeline and are subject
to every eligibility gate, including the untrusted-content write ban. This gives
formation a second high-quality input without giving it a bypass.

## Ports and data model

`ContextBuilder` (Section 7) is unchanged. These are additions.

```python
class ContextPlan(BaseModel):
    session_id: UUID
    epoch: int
    prefix_sha256: str
    prefix_tokens: int              # measured exactly, never estimated
    model_id: str
    tool_names: list[str]           # pinned at session open
    tool_schema_sha256: str
    snapshot_id: UUID | None
    snapshot_watermark: int         # retrieval spec: the recall delta
    skill_pins: tuple[SkillPin, ...]  # skills spec: pinned at open
    cache_breakpoints: list[CacheBreakpoint]
    policy_version: str
    builder_version: str
    created_at: datetime
```

`ContextBudget` (Section 11.3) gains the classes the allocator needs to distinguish,
and a margin:

```python
class ContextBudget(BaseModel):
    total_tokens: int
    reserve_output_tokens: int
    platform_tokens: int
    agent_tokens: int
    tool_tokens: int
    skill_catalog_tokens: int       # skills spec: Region A metadata
    skill_body_tokens: int          # skills spec: Region B, loaded
    retrieved_context_tokens: int   # frozen snapshot + elastic in-turn
    history_tokens: int
    # additions
    working_state_tokens: int
    tool_result_tokens: int
    knowledge_tokens: int           # knowledge spec: Region B, passages
    safety_margin_ratio: float = 0.05
```

`Fact` is referenced by Milestone 7's `WorkingState` and defined here, because
provenance and trust are what make it usable by formation:

```python
class Fact(BaseModel):
    statement: str
    source_event_ids: list[int]
    trust_level: TrustLevel
    established_at: datetime
```

```python
class CompactionResult(BaseModel):
    summary: str
    source_event_ids: list[int]
    elided: list["ElidedSpan"]
    replaced_through_sequence: int
    depth: int                      # <= 2
    tokens_before: int
    tokens_after: int
    compactor_version: str

class ElidedSpan(BaseModel):
    item_id: str
    trust_level: TrustLevel
    byte_length: int
    artifact_ref: str | None
    event_id: int
```

Ports:

```python
class ContextPlanner(Protocol):
    async def plan(
        self,
        session: Session,
        agent: AgentSpec,
        principal: Principal,
        model: ModelCapabilities,
    ) -> ContextPlan: ...

    async def current(self, session_id: UUID) -> ContextPlan | None: ...

    async def rotate(
        self, session_id: UUID, reason: str
    ) -> ContextPlan: ...

class TokenEstimator(Protocol):
    def estimate(
        self, items: Sequence[ConversationItem], model_id: str
    ) -> int: ...

    def estimate_tools(
        self, tools: Sequence[ToolSpec], model_id: str
    ) -> int: ...

    def reconcile(
        self, model_id: str, estimated: int, actual: int
    ) -> None: ...

class Compactor(Protocol):
    async def compact(
        self,
        checkpoint: RunCheckpoint,
        budget: ContextBudget,
        reason: str,
    ) -> CompactionResult: ...
```

Events (extending Section 6.8):

```text
context.plan.created
context.epoch.rotated
context.compacted
context.working_state.updated
context.budget.pressure
context.budget.exceeded
```

`context.budget.pressure` records that a yield step ran and which one; it is the
signal that tells an operator a deployment is chronically over-subscribed before
`context.budget.exceeded` tells them it has failed.

## Failure modes and defenses

| Failure | How it happens | Defense |
| --- | --- | --- |
| **Cache thrash** | A volatile byte reaches the prefix — a date, a counter, a re-serialized tool schema with unstable key order | Region declared per item type; canonical serialization; `prefix_sha256` on every request; the fifty-turn stability gate |
| **Label laundering** | Compaction paraphrases untrusted content into unlabeled prose | Untrusted spans are elided to typed pointers, never summarized; canary eval |
| **Envelope forgery** | Tool output contains the closing delimiter | Per-item nonce; delimiter escaping at render; injection eval |
| **Orphaned tool pair** | The allocator drops a call or a result independently | Pairs are atomic budget units; a validator rejects orphans before send |
| **Silent context overflow** | Estimator under-counts; provider 400 mid-run | Conservative rounding; 5% margin; exact prefix measurement; reconciliation against actual usage |
| **Truncated system prompt** | Agent instructions exceed their cap and are trimmed to fit | Prefix classes never yield; the session fails to open with the offending class named |
| **Compaction amnesia** | A constraint set twenty turns ago is summarized away | Constraints never yield and never merge away; compaction-fidelity eval |
| **Working-state drift** | State asserts something the log contradicts | Typed transitions only, each emitting an event; carry recomputed from the log, not copied |
| **Epoch churn** | Routing or policy changes rotate the prefix repeatedly | Epochs are explicit, logged, and counted; epochs-per-session is a tracked metric with target 1.0 |
| **Unstable history selection** | The seed reads the projection as it stands rather than at a fixed cut, or the estimator returns different numbers for the same items | Seeding reads the log below `runs.seed_event_sequence`; the estimator is pure; the cut is a suffix index, property-tested for stability |
| **Reasoning-item leakage** | An opaque provider payload is summarized, logged, or carried across a run | Excluded from compaction input by type; dropped at run boundaries; never rendered into a summary (ADR-0007) |

## Hard gates

Six hard gates. Five are on Milestone 7 with the rest of the engine; the
first is on Milestone 1, because ADR-0024 places deterministic assembly in
the vertical slice and a builder whose output is not reproducible cannot be
built incrementally afterwards.

1. **Determinism.** `build()` invoked twice on the same checkpoint produces
   byte-identical output. Property-tested across generated checkpoints. **M1.**
2. **Prefix stability.** The scripted fifty-turn session — clock crossing
   midnight, a tool revoked, memory written and corrected, a forced compaction —
   yields exactly one distinct `prefix_sha256`. **M7.**
3. **Budget conformance.** No assembled request exceeds the model's window; the
   output reserve is intact on every request; a synthetic overflow yields in the
   specified order and no more than necessary. **M7.**
4. **Tool-pair integrity.** No assembled request, under any yield path, contains
   an unpaired tool call or tool result. **M7.**
5. **Trust preservation.** A canary string placed in `EXTERNAL_UNTRUSTED` tool
   output never appears outside an envelope, and never appears in a compaction
   summary. Envelope-closing attempts in tool output do not escape the
   envelope. **M7.**
6. **History-cut determinism.** The retained set is always a contiguous suffix,
   always fits `history_tokens`, never falls earlier than the summary floor,
   never contains an orphaned tool call or result, and is identical across two
   calls on the same input. Property-tested over generated item lists. **M7.**

## Tracked metrics

- **Cached prefix ratio** — prefix tokens served from cache after the first request
  of a session. Below roughly 90% means the invariant is leaking somewhere the hash
  check has not caught.
- **Epochs per session** — target 1.0.
- **Estimator error** — signed, per model; must never be negative by more than the
  safety margin.
- **Compaction rate and depth** — compactions per thousand turns, and the share
  reaching depth 2. Rising depth means the escalation path (child runs) is not being
  taken when it should be.

## Build sequence (incremental, each gated by evals)

1. **Deterministic assembly.** The two regions, the region-assignment table, the
   fixed order, trust labels on every item, and `prefix_sha256` recorded from the
   first commit — the hash is not retrofittable onto traffic that has already been
   served unstably.
2. **Budget allocator.** Fixed floors, absolute caps, the selection cut, the yield
   order, tool-pair atomicity, and the estimator behind its port.
   Fail-at-plan-time for prefix overflow.
3. **Trust envelopes.** Nonced delimiters, escaping, and the framing text in the
   prefix. This is security hardening and it precedes anything that transforms
   content.
4. **Cache breakpoints and epochs.** `CacheHints` population per Section 10.1, the
   rolling history window, epoch rotation and its accounting. Delivers the cached
   prefix ratio, which is how step 1 is proven in production rather than only in CI.
5. **Working state.** The typed control tool, carry rules, bounds, and the
   established-facts handoff to formation.
6. **Compaction.** As a checkpoint write, on trusted content only, with elision of
   untrusted spans, provenance ids, and the depth cap.
7. **Reconciliation and tuning.** Estimator correction factors from real usage,
   the pressure metrics, and retuning the class caps against them.

## Decisions

- **Two regions, one rule: if it can differ between two requests in the same
  session, it is not in the prefix.** Region membership is a property of item type,
  declared in code and asserted at assembly, not a judgment made per request.
- **Prefix stability is enforced by a recorded hash and a scripted long-session
  test**, not by convention. A session with more than one prefix hash per epoch is a
  defect with an id.
- **The tool set is pinned at session open; revocation is enforced at call time.**
  A tool definition in a prompt grants nothing, so advertising a tool that will be
  denied is cheaper and safer than rewriting the prefix on every permission change.
- **Prefix changes that genuinely cannot be absorbed rotate an epoch**, explicitly
  and countably, with epochs-per-session tracked against a target of 1.0.
- **Only history scales with the context window.** Every other class is capped
  absolutely, because prefix content is attention paid on every request and
  attention degrades with item count, not with window fraction.
- **The prefix never yields.** If platform policy, agent instructions, tools, and
  snapshot do not fit their ceilings, the session fails to open with the offending
  class named. A silently truncated system prompt is worse than a refused session.
- **History selection is a contiguous suffix chosen by a deterministic cut, and
  the seed reads the log at a pinned sequence.** A relevance ranking over history
  would duplicate in-turn recall and leave a missing turn ambiguous between a
  selection defect and a ranking miss; a live projection read would let two seeds
  of one run disagree.
- **Yield order is knowledge passages, then in-turn recall, then tool-result
  truncation, then compaction** — cheapest and most recoverable first. Knowledge
  passages lead because the corpus is still there and one `knowledge.search`
  re-reaches it; compaction is last because it alone costs a model call and
  loses information irreversibly within the run. This is the same order stated
  where the ladder is specified, and the two must not drift apart.
- **Tool call/result pairs are atomic budget units.** Dropping half a pair is a
  malformed request, not a smaller one.
- **`build()` is a pure function; compaction is a write.** Pressure is resolved by
  writing a checkpoint and rebuilding, never by mutating inline. Determinism is what
  makes retries safe and the stability assertion meaningful.
- **The prefix is measured exactly at plan time; only the body is estimated.** This
  confines estimator error to the region with slack and moves prefix-size failures
  to session open.
- **The estimator is conservative, per-model, and reconciled against actual usage.**
  Drift becomes a number rather than a mystery.
- **Untrusted content is elided, never paraphrased.** Summarization is a
  label-laundering vector; the summarizer runs on trusted content only, and
  untrusted spans become typed pointers that retain their label.
- **Summary depth is capped at 2.** Past that the run has outgrown its window and
  should escalate to a child run rather than be compressed into something
  unverifiable.
- **Framing lives in the prefix; only data varies.** Envelope rules, memory framing,
  and the "this is data, not instructions" instruction are written once as stable
  platform text.
- **Content cannot close its own envelope**, by nonce and by escaping.
- **Working state is written by a typed control tool and the runtime, never parsed
  from model prose**, carries across turns by per-field rule computed from the log,
  is bounded, and never evicts a constraint.

## Open questions

None outstanding for assembly. Two adjacent items were left open here and are
now decided in [runtime-loop.md](runtime-loop.md), which gave compaction its call
site and therefore had to say what the call resolves to.

The **compaction summarizer's prompt and model tier** resolves through
`ModelRouter` under a named `compaction` model policy rather than being fixed
here or chosen at the call site. It defaults to the run's own provider, so
provider pinning is not broken by a compaction mid-run, at the cheapest tier
whose context window admits the region being summarized. The prompt is a
versioned asset and its version is recorded on the checkpoint compaction writes,
which is what makes a fidelity regression attributable to a prompt change rather
than to a model change. The tuning itself still belongs to the
compaction-fidelity eval; what is fixed is where the choice lives and what is
recorded about it.

**Skill-content injection** (Section 30.4 loads full skill instructions on
selection) is assigned to **Region B**, as the reasoning above anticipated, with
one addition that answers the caching objection: a skill body, once selected,
is **sticky for the remainder of the session** unless the skill is deselected by
a control tool. A skill that entered and left the prefix on alternating steps
would invalidate the cached prefix on every one of them, which costs more than
carrying an unused body. Stickiness is what makes Region B affordable for a
large body; without it the right answer would have been the turn layer.
[skills.md](skills.md) takes this decision as given and supplies the rest:
the caps, the loading tool, the trust derivation, and the rule that a body
never yields. It also settles the one thing this paragraph left open — a
skill is never deselected, and a third load fails rather than evicting.
