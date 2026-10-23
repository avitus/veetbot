# ADR-0239: Owner records — named numeric series the agent appends to and queries over time

- Status: Accepted — the owner authorized the roadmap entry and its
  implementation on 2026-10-22 with "Push to dev and then I authorize the
  roadmap entry. Begin implementation"; entered as Milestone 34
- Date: 2026-10-22 (drafted and authorized the same day)
- Related: ADR-0018, ADR-0019, ADR-0021, ADR-0026, ADR-0029, ADR-0031,
  ADR-0033, ADR-0100, ADR-0117, ADR-0122, ADR-0126; Sections 8, 9.2, 15,
  18.4, 21 (roadmap), 22 and 24 of the engineering plan
- Detailed design: `docs/plan/owner-records.md`, to be written when the item is
  authorized; the tool classification lands in `docs/plan/builtin-tools.md`
- Owner request: on 2026-10-21 the owner asked whether Veetbot "has write
  access to a SQL-style database", for example to "keep track of health
  metrics, such as blood pressure, over time", and on 2026-10-22 asked for this
  proposal.

## Context

Nothing the model can call today keeps a value series. The platform's own
PostgreSQL holds events, beliefs, artifacts and People rows, and the model
never queries it. The built-in roster (`docs/plan/builtin-tools.md:82`) and
the tools other specifications declare (`docs/plan/builtin-tools.md:124`)
contain no table, record or SQL tool. Each existing store fails the use case
for a reason that is a design rule, not an accident:

- **Beliefs keep the latest value, not a history.** `memory.remember` stores
  one statement under a subject. A newer owner statement on the same subject
  supersedes the older one by authority then recency
  (`docs/plan/memory-formation-and-consolidation.md:212`); the superseded
  belief is retained bi-temporally but excluded from retrieval. Three weeks of
  readings become one live belief and two hidden ones. `memory.search` takes an
  `as_of` instant, so the model could probe past dates one at a time, but
  retrieval is ranked recall with no aggregate, no window and no ordering
  guarantee. Recording a reading as a belief also burdens formation with
  conflict resolution and review flags that exist for facts, not measurements.
- **The sandbox forgets.** `sandbox.run_command` runs Python 3.12 in gVisor,
  so SQLite is available, but the workspace is a cache discarded when the run
  finishes or pauses (`docs/plan/sandbox-isolation.md:444`). Anything that
  must survive is an artifact, and no tool brings an artifact back into a
  later run's workspace. A database file cannot round-trip, and roadmap item
  B6 explicitly keeps "arbitrary session-history/artifact retrieval" off the
  plan.
- **Knowledge documents are read-only input.** `knowledge.ingest` admits an
  artifact as a searchable document under `USER` origin trust
  (`docs/plan/knowledge-documents.md:515`); nothing appends to one.
- **People memory has dated records, scoped to people.** Interactions and
  relationship intervals are revisioned and erasable
  (`docs/plan/people-and-relationships.md:161`), which is the governance shape
  this proposal borrows, but the subject of a People row is a person, and its
  exclusions (`docs/plan/people-and-relationships.md:147`) keep it that way.

The roadmap beyond Milestone 15 (`docs/plan/engineering-plan.md:4355`) has no
item for owner-authored structured records. Section 24 requires deferred work
to become a roadmap entry before it enters the plan, and the milestone map's
rule for a zero-gate milestone requires a specification with gates before any
code. This ADR proposes the entry and fixes the decisions the specification
would expand, so that the owner can accept, amend or decline the shape before
anyone writes it.

## Decision (proposed)

### 1. A records domain, not a database

The agent gets a bounded, owner-scoped record store exposed as five tools in a
new `records` domain, not SQL access of any kind. A series is a named list of
timestamped numeric entries; an entry carries one to four named numeric
components so that a blood-pressure reading is one entry (`systolic`,
`diastolic`, optionally `pulse`) rather than two series the model must keep in
step. The model declares, appends, lists, queries and deletes; it never names a table,
writes a predicate or sees a row id it did not create. Every read and write is
tenant- and principal-scoped, as every memory write already is
(`docs/plan/memory-formation-and-consolidation.md:516`).

```text
tool                   purpose
---------------------  -----------------------------------------------------
records.list           the principal's series: name, components, units,
                       entry count, first and last observed time, sensitivity
records.create_series  declare a series: name, components, units; the owner
                       approves it before it exists
records.append         add one entry to an existing series
records.query          entries in a time window, newest first, with a
                       server-computed summary per component, or bucketed
                       aggregates
records.delete         remove one entry or a whole series
```

The domain joins the partition table in `docs/plan/tool-system.md` as a
builtin domain, the way `delegate` and `people` did.

### 2. The data model

```python
class RecordComponent(BaseModel):
    name: str                     # [a-z][a-z0-9_]{0,31}, unique in the series
    unit: str | None              # at most 16 characters, e.g. "mmHg", "kg"

class RecordSeries(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    name: str                     # normalized: NFC, casefold, single spaces
    display_name: str             # the owner's spelling, 1–64 characters
    components: list[RecordComponent]   # 1–4, fixed after creation
    sensitivity: Sensitivity      # default SENSITIVE; never below INTERNAL
    cadence: str | None           # "daily" | "weekly" | "monthly" | None
    revision: int
    created_at: datetime
    updated_at: datetime

class RecordEntry(BaseModel):
    id: UUID
    series_id: UUID
    tenant_id: str
    principal_id: str
    observed_at: datetime         # aware; the measurement's time, not now
    observed_precision: str       # "exact" | "day"
    values: dict[str, Decimal]    # one per declared component, no extras
    note: str | None              # at most 200 characters, scanned
    idempotency_key: str          # the pipeline's key; unique per series
    source_session_id: UUID | None
    source_event_id: UUID | None  # nulled by session deletion, entry kept
    created_at: datetime
```

Values are `NUMERIC(18,6)`, finite, with at most twelve integer digits.
`observed_at` may not be later than the composition clock plus five minutes
and defaults to the clock when the model omits it; `day` precision records the
owner's date without inventing a time. A principal holds at most 64 series and
a series at most 100,000 entries; both caps are configuration with those
defaults. Physical tables are `record_series` and `record_entries`; the
specification may store components and values in a narrow child table or a
constrained JSONB column, but it may not hide the one-value-per-declared-
component invariant in an unconstrained payload
(`docs/plan/people-and-relationships.md:161`). Both tables get forced
tenant row-level security and scoped composite foreign keys so a cross-owner
link fails at write time, a unique index on `(principal_id, name)`, a unique
index on `(series_id, idempotency_key)`, and an index on
`(series_id, observed_at)`. The migration follows ADR-0031
(`docs/plan/event-log-and-persistence.md:906`): one head, structure separate
from data, downgrade written.

### 3. The tool contracts

Closed JSON schemas with `additionalProperties: false`, every bound visible to
the model, after the pattern the People tools set
(`docs/plan/builtin-tools.md:1877`).

`records.create_series`:

```json
{
  "series": "blood pressure",
  "components": [
    {"name": "systolic", "unit": "mmHg"},
    {"name": "diastolic", "unit": "mmHg"}
  ],
  "sensitivity": "sensitive",
  "cadence": "daily"
}
```

`sensitivity` is optional and is `sensitive` or `restricted`. `cadence` is
optional and is `daily`, `weekly` or `monthly`; it declares how often the
owner means to log, which is what lets the catalog (section 9) say a series
is due without the model guessing, and a series without one is never called
overdue. The call requires approval (section 5), which is the owner's
explicit confirmation that the series should exist. Declaring a name that exists with the same
components returns the existing series unchanged; with different components
it is refused with `records.series_exists`. The result is the series as
`records.list` shows it.

`records.append`:

```json
{
  "series": "blood pressure",
  "values": {"systolic": 128, "diastolic": 82},
  "observed_at": "2026-10-22T07:40:00-07:00",
  "precision": "exact",
  "note": "after coffee"
}
```

An append to a series that does not exist is refused with
`records.series_unknown`, so no entry lands in a series the owner has not
confirmed. An entry whose keys do not match the declared components is
refused with `records.component_mismatch`. The result is the entry id, the
series' display name, the stored values and the series' entry count; the
text the model sees is one line, so a reply can confirm what was recorded.

`records.query`:

```json
{
  "series": "blood pressure",
  "since": "2026-09-22T00:00:00-07:00",
  "until": "2026-10-22T23:59:59-07:00",
  "limit": 100,
  "order": "desc",
  "bucket": null
}
```

`limit` is 1 to 1,000, default 100. The result carries the page of entries
and, for the whole window rather than the page, a per-component summary of
`count`, `min`, `max`, `mean`, `first` and `last` with their observed times.
With `bucket` set to `day`, `week` or `month`, the result carries per-bucket
`count`, `min`, `max` and `mean` instead of entries, at most 1,000 buckets.
Means are `Decimal` rounded half-even to the component's stored scale, so the
same window renders the same bytes on every call. `records.list` takes no
arguments. `records.delete` takes `series` and an optional `entry_id`.

### 4. Classification

Every `ToolSpec` field, in the table form `docs/plan/builtin-tools.md:269`
uses:

```text
name                   side_effect  risk    idempotency               scopes
---------------------  -----------  ------  ------------------------  -------------
records.list           NONE         LOW     READ_ONLY                 records.read
records.query          NONE         LOW     READ_ONLY                 records.read
records.create_series  NONE         MEDIUM  IDEMPOTENT                records.write
records.append         NONE         MEDIUM  CONDITIONALLY_IDEMPOTENT  records.write
records.delete         NONE         MEDIUM  IDEMPOTENT                records.write

name                   output_trust   timeout_s  max_output_bytes  allow_parallel
---------------------  -------------  ---------  ----------------  --------------
records.list           INTERNAL_TOOL  10         16384             yes
records.query          INTERNAL_TOOL  10         262144            yes
records.create_series  INTERNAL_TOOL  10         4096              no
records.append         INTERNAL_TOOL  10         4096              no
records.delete         INTERNAL_TOOL  10         4096              no
```

All five are `CAPABILITY`, `BUILTIN`, `in_process`. `records.create_series`
is `IDEMPOTENT` because the same declaration twice yields the same series and
a different one under an existing name is refused. `records.append` is
`CONDITIONALLY_IDEMPOTENT` because two readings with the same values on the
same day are two entries, not one: the pipeline's idempotency key
(`docs/plan/tool-system.md:617`) is stored on the entry, so a re-executed call
after a crash finds the recorded entry rather than storing a second one.
`side_effect` is `NONE` on the same grounds as `memory.remember` and
`knowledge.ingest`: the write lands in the platform's own governed store and
leaves the host for nowhere. That combination carries two obligations the
tool must meet itself, because the executor sets the effect watermark only
for non-`NONE` side effects and re-invokes a tool on replay exactly as on
re-execution: `records.append` calls `mark_effect_sent` before its insert,
as `skill.manage` and `schedule.create` do, and when invoked with an
idempotency key it has already stored it returns that entry unchanged. The
recovery table's replay row (`docs/plan/tool-system.md:673`) holds only with
both. `records.delete` is `IDEMPOTENT` because deleting what is already gone
changes nothing.

`records.read` and `records.write` join the closed scope vocabulary
(`docs/plan/policy-and-approvals.md:268`) and the HTTP enumeration beside
`memory.read` and `memory.write` (`docs/plan/http-api-and-streaming.md:363`).
The vocabulary in code holds forty-four strings
(`src/agent_core/policy/scopes.py`); the specification's own counts are
already stale against it, and the specification update that adds these two
reconciles them to forty-six.

### 5. Trust and policy

Values are numbers, and numbers are never raised to `USER` argument trust
(`docs/plan/tool-system.md:566`), so argument trust cannot tell an owner's
reading from one a fetched page suggested. The gate is therefore
`origin_trust`, the signal that says whether untrusted content was in the
room, and the owner's answer of 2026-10-22 is that an untrusted turn gets an
approval rather than a refusal. The deterministic layer's trust overlay
cannot supply it: the overlay escalates an allowed action to an approval on
untrusted input only for side effects outside `NONE`, `WORKSPACE_READ` and
`NETWORK_READ`, which is why `memory.remember` never prompts, and a
`side_effect` other than `NONE` would misdescribe a write that leaves the
host for nowhere. The mechanism is instead a `tool_rules` row with a
condition (`docs/plan/policy-and-approvals.md:643`), the shape ADR-0168 gave
`browser.act`:

```yaml
records.append:
  condition: turn_origin_trusted
  decision: allow
  otherwise: require_approval
```

`turn_origin_trusted` is a new `PolicyCondition` that holds when the action's
`origin_trust` is not `EXTERNAL_UNTRUSTED`, the same test the overlay applies
to a tool without a row. It is deliberately not the engine's "may authorize"
set, which excludes `MEMORY`: recall is on by default and labels an ordinary
turn `MEMORY`, so a condition on that set would ask for a tap on most
mornings, while a turn that read an email, a web page, a knowledge passage or
any external tool result is labelled `EXTERNAL_UNTRUSTED` by the executor and
gets the approval. The approval card carries the arguments, so "log the
reading from my doctor's email" shows the owner the series, values and time
and stores the entry on their tap; a denied or expired approval stores
nothing. Internal tool results such as `records.list` do not lower the label.
The records service keeps no trust check of its own: the profile is the one
place the rule lives, and its version is the profile's hash.

`records.create_series` and `records.delete` require approval in every turn,
through unconditional rows with `decision: require_approval`. The first is
the owner's explicit confirmation that a series should exist, with its name,
components and units on the card; the second shows which series or entry
goes before it goes. No trust refusal follows a tap, because the human
decision is the gate. `human_confirms_arguments` is set on none of the three
rows: that flag exists for a tool whose own surface confirms the arguments,
which these have not. The rows are written unconditionally, like the
`device.sms.send` row: a profile is a file, not a flag, and with the tools
unregistered the rows are inert because an unknown tool is denied anyway.
Hardline rules run first and are untouched.

### 6. Scans and sensitivity

Series names, component names and notes pass the shared secret scan and
injection scan that governed memory writes pass
(`docs/plan/memory-read-api-and-browser.md:247`). A credential-shaped value in
any of them refuses the whole call with `records.secret_detected` and stores
nothing. An instruction-like note is stored with
`contains_instruction_like_text` set and is withheld from every model-facing
result, which renders `[note withheld]` in its place; the owner still sees it
over HTTP. Refusal messages never quote the rejected text.

Every series carries a `Sensitivity`, set once at creation and never asked
about: the default is `sensitive`, the owner may say "treat this as
restricted" and the model passes `restricted`, and nothing goes below
`internal`. The owner asked for a sensible default without friction for the
sake of privacy, and this is what the label costs and buys. In the owner's
own chat it costs nothing: model-facing reads apply the same constant
`restricted` ceiling that `memory.search` applies, so the model sees every
series the principal owns, and no creation or append ever asks a privacy
question. What it buys is a predicate the platform already applies
elsewhere. Recall filters beliefs by the surface's sensitivity ceiling
(`docs/plan/memory-retrieval-and-ranking.md:61`), People context is
constructed with a `sensitive` ceiling, and email-mode recall with
`internal`; the records tools are constructed with the ceiling their
surface's recall uses, so a health series labelled `sensitive` is invisible
wherever the owner's beliefs of that label already are, without a migration
later. In the first slice that is belt and braces, because only the owner's
principal holds `records.read` at all (section 10) and a public caller or an
unpaired sender gains no owner scopes.

### 7. Persistence, events and erasure

The row is canonical; the event is provenance. `records.append` appends
`records.entry.appended` to the run's session in the same unit of work as the
insert, carrying the series id, entry id, component names, values and
observed time, so that a rebuild can replay it. `records.series.created`,
`records.entry.deleted` and `records.series.deleted` carry ids and counts
only. All four join Section 6.8's list with `payload_schema_version`.

Records are owner-authored, not derived, so they outlive their source.
Session deletion's enumerated cascade gains the two records tables and nulls
`source_event_id` while keeping the entry. Principal erasure is a store-level
contract, `erase_principal`, mirroring the People store's, since the platform
has no application-level principal deletion to join. Deletion through the
tool is a hard delete in one transaction, visible to no read afterwards, with
an event that carries no values. Nothing re-forms from a deleted record because nothing forms from
records at all (next section), so no tombstone is needed. Records never decay
and never expire; the lifecycle rules for beliefs do not apply to them.

### 8. Relationship to memory

A record is not a belief and never becomes one. Memory formation adds one
eligibility rule at its filter stage
(`docs/plan/memory-formation-and-consolidation.md:191`), and the rule is
exact so that it cannot drift into suppressing facts. Segment selection,
which today reads only the owner's messages, additionally reads the
`records.entry.appended` events of the segment and collects their values;
that is the one change to what formation reads. A candidate statement is
ineligible when it contains, as numeric tokens, every component value of one
entry appended in the same segment: "128 over 82" against a `{128, 82}`
entry is suppressed, "I ran 82 km" in the same segment is not, and a
one-component weight entry suppresses only a statement carrying that weight.
The rule lives in the shared deterministic eligibility gate, so every
extractor that passes candidates through it, deterministic formation, People
formation, provider extraction, distillation and reconsolidation alike,
inherits it without its own copy. The suppression is counted with the run's
rejected candidates and named `recorded_elsewhere` in the formation
diagnostic; the specification places the vocabulary. The owner's sentence
"my blood pressure was 128 over 82 this morning" therefore produces one entry
and no belief, and the supersede churn described in Context cannot start.
`memory.remember` is not refused for a measurement: an explicit write stays
the model's choice, steered by the two tools' descriptions. The existence of
a series remains ordinary conversational evidence: "the owner tracks blood
pressure" may form as a fact under the usual rules, and that is the belief
that lets recall suggest the tool.

Records are not memory for the reconsolidation, lifecycle, recall-evaluation
or People machinery either; none of ADR-0169, ADR-0178 or ADR-0100 gains a
new input. A record may cite nothing and nothing cites a record.

### 9. The records catalog in context

The owner decided on 2026-10-22 that the model should always know which
series exist and be able to nudge, so the catalog is in the first slice, and
the context engine's rules for a new item type are met one by one rather
than left to the specification.

**What it is.** One platform-rendered item listing the principal's series:
display name, components with units, entry count, last observed time, and,
for a series with a cadence, whether it is due and since when. Never a value.
Lines are ordered by normalized name, so the same store renders the same
bytes. A principal with no series renders no bytes at all, the way an empty
persona row and an empty deferred-tool index do, so a session without records
is byte-identical to today. A series above the surface's sensitivity ceiling
is omitted exactly as it is from a read.

**Region.** B, by the one rule (`docs/plan/context-engine.md:74`): the
catalog changes whenever an entry lands, so it can differ between two
requests in the same session and may never touch the prefix. The item type
declares its region as a class attribute, which the builder asserts on
assembly (`docs/plan/context-engine.md:148`). It enters the fixed assembly
order as a new row between the in-turn recall lines and the runtime metadata
(`docs/plan/context-engine.md:100`), listed there because an order that
omits a row is not total.

**Trust and envelope.** The item is labelled `INTERNAL_TOOL` and rendered
inside the ordinary envelope (`docs/plan/context-engine.md:765`), as a
`records.list` result would be: the platform renders it from the governed
store, its names were approved by the owner on a card and refused both scans
at creation, and `INTERNAL_TOOL` content lowers neither the builder's
request-level origin stamp nor the executor's turn label. A fresh chat whose
only added item is the catalog therefore keeps `USER` origin trust, and an
append in it stores without a card.

**Budget.** An absolute class cap of 16 series and 600 tokens, no scaling
with the window, as every non-history class is capped
(`docs/plan/context-engine.md:330`); a seventeenth series is not rendered,
the catalog says how many were omitted, and `records.list` reaches them. The
budget gains a `records_catalog_tokens` field. Under pressure the catalog
yields whole, never truncated, after knowledge passages drop and before
in-turn recall trims (`docs/plan/context-engine.md:612`), because a shortened
catalog misstates what exists and one tool call recovers it.

**Purity.** `build()` reads no store. The catalog is fetched in the same
per-request preparation that computes in-turn recall and is handed to
`build()` as an input, so the determinism gate holds: two builds on the same
inputs are byte-identical.

**Stability.** The engine's fifty-turn prefix script
(`docs/plan/context-engine.md:1062`) is extended by the records
specification with a series created and entries appended mid-session, and
must still yield exactly one `prefix_sha256`; the extension is registered as
a records gate rather than an amendment to the engine's.

**The nudge.** The catalog is what lets the model say "you have not logged
blood pressure since Friday" in conversation, and the `cadence` on the
series is what keeps that from being a guess: only a series whose cadence
has elapsed is rendered as due. A fixed sentence in the platform framing
(row 2) tells the model what the catalog is, that a due series may be
mentioned once per session and not while the owner is mid-task, and that the
catalog is not a request to log. That sentence is stable platform text, so
it is cache-safe. An unattended reminder, one that reaches the owner without
a chat open, is an ordinary schedule the owner creates in plain words;
scheduled runs hold `records.read` (section 10) and see the catalog, so no
new mechanism is needed.

### 10. Activation and surfaces

`AGENT_RECORDS_ENABLED`, default `0`, registers the five tools, following
`AGENT_PEOPLE_ENABLED`; the `tool_rules` rows are in the profile regardless.
Pinned tool catalogs are unchanged; an unenabled deployment's roster is
unchanged. Scopes are granted per deployment unit through `AUTH_SCOPES` and
per constructed principal, so the first slice names its holders: the owner's
principal carries `records.read` and `records.write` through the API and
worker units and through every owner-paired surface (Telegram, WhatsApp, SMS
and the device channel), because a reading typed into Telegram is the owner's
words as much as one typed into Chat; scheduled runs and delegated child runs
carry `records.read` only, since neither can know a value the owner has not
just said. The `AUTH_SCOPES` lines in `deploy/veetbot.env.example` and the
surface, schedule, notify and call env examples name these, and the
production env edits remain the owner's. There is no HTTP route and no
native view: the owner decided on 2026-10-22 that reading through the chat
client is sufficient, so the owner reads records through the agent and
exports them by asking for a file, which `records.query`,
`workspace.write_text` and `artifact.export` already compose within their
limits (a year of daily entries is under 50 KiB). A read route or a native
screen, if ever wanted, enters by its own amendment and is not part of the
entry condition. There is no later slice: the catalog of section 9 ships
with the tools.

## Alternatives considered

1. **A general SQL tool.** Model-authored SQL against an owner database. The
   platform cannot classify it: one tool would be read-only and destructive
   at once, hardline rules cannot parse SQL safely, the schema would be
   whatever the model last decided, and erasure would be ungoverned. Rejected.
2. **Beliefs with `as_of` probing.** Already possible, already wrong for the
   reason given in Context: supersession, ranked recall, no aggregates, and a
   review queue full of measurements. Rejected.
3. **SQLite in the sandbox, round-tripped through artifacts.** Needs an
   artifact-import tool that roadmap item B6 excludes, breaks the
   workspace-is-a-cache rule, and leaves two runs racing over one file with
   no erasure path. Rejected.
4. **Knowledge documents.** Ingestion is a one-way admission of a finished
   document; appending is not a document operation. Rejected.
5. **An external MCP server (hosted spreadsheet or database).** Sends health
   data off the host and inherits operator-classified scopes the platform
   cannot vouch for. Possible later as an export target; not a store.

## Consequences

- Five tools, two scopes, two tables, four events, one configuration knob,
  one eligibility rule in formation, three `tool_rules` rows, one new
  `PolicyCondition`, and one context item type with its assembly row, budget
  class, yield step and framing sentence.
- The agent can answer "what has my blood pressure done this month" with a
  deterministic summary rather than a recollection.
- `AGENTS.md` is at its size cap (`scripts/check_docs.py` enforces it), so
  the routing-table row for `owner-records.md` must be paid for by trimming
  another line.
- The configuration knob census moves: the ADR-0036 inventory and
  `tests/unit/test_config.py` gain the knob, and an operator page in the
  shape of `docs/people-operations.md` documents it.

## Scope admission and entry condition

This ADR added roadmap item B14 to the engineering plan as a proposal. The
owner authorized it on 2026-10-22, and the item entered as Milestone 34 with
[owner-records.md](../plan/owner-records.md) as its specification and its ten
gates registered against the pending check until each test lands, as
Milestone 32's were. `docs/status/project-state.yaml` carries the milestone
from that date. Nothing under `src/`, `tests/`, `evals/` or `migrations/`
changed before the specification was checked in.

## Verification: gates and metrics to register

Hard gates the specification registers, each a test on both the in-memory
and PostgreSQL adapters where a store is involved:

1. **Scope isolation.** Another principal's series is `not_found` to every
   tool and route, with forced RLS and scoped foreign keys refusing a
   cross-owner link at write time.
2. **Trust gate.** A `records.append` in a turn whose origin trust is
   `EXTERNAL_UNTRUSTED` evaluates to `REQUIRE_APPROVAL` and nothing is
   written before the approval resolves; an approved call stores the entry
   and a denied or expired one stores nothing; an append in a turn that holds
   a `memory.search` result is allowed without a card; `turn_origin_trusted`
   holds for `USER`, `MEMORY` and `INTERNAL_TOOL` origins and fails for
   `EXTERNAL_UNTRUSTED`; a create or delete never executes without a resolved
   approval, in any turn.
3. **Append replay.** A `records.append` re-executed after a simulated crash
   with the effect watermark set stores one entry, and the re-invoked tool
   returns the recorded entry for its idempotency key.
4. **Query determinism.** The window summary and each bucketed aggregate for
   a fixed fixture are byte-stable across calls and across adapters; `since`
   and `until` are exact on the half-open interval.
5. **Scans.** A credential-shaped note or name refuses the call and stores
   nothing; an instruction-like note is stored flagged and withheld from
   model-facing results.
6. **Erasure.** Deleting a series removes its entries from every read in one
   transaction and emits an event without values; deleting the source session
   keeps the entry and nulls its provenance; the store-level
   `erase_principal` contract removes everything, on both adapters.
7. **No belief per value.** A consolidation run over a session containing
   appends forms no belief whose statement carries every component value of
   an entry appended in the same segment, on every extractor that shares the
   eligibility gate, while a statement carrying only one value of a
   two-component entry still forms.
8. **Registration.** Startup registration validation accepts all five tools
   with every `ToolSpec` field, the two scopes are in the closed vocabulary,
   the three `tool_rules` rows are in the default profile, and with
   `AGENT_RECORDS_ENABLED=0` the tools are unregistered, the rows are inert
   and the roster is unchanged.
9. **Listing stable.** `records.list` output for the same store is identical
   across runs, so the prefix hash the context engine records cannot vary
   because of it.
10. **Catalog.** The item declares Region B; a principal with no series
    renders no bytes; the same store renders the same bytes; the rendering
    carries names, components, counts, last times and due state and never a
    value; a series above the surface ceiling is absent; the extended
    fifty-turn script with a series created and entries appended mid-session
    yields one `prefix_sha256`; under synthetic pressure the catalog drops
    whole after knowledge passages and before recall trims; and a request
    whose only added item is the catalog keeps `USER` origin trust.

Tracked metrics: appends per principal per week; append approvals
requested, approved and denied; catalog tokens per request; due-series
mentions per session; query latency at the 95th percentile; refusals by
reason code; owner deletions per month.

## Documentation obligations on authorization

- `docs/plan/owner-records.md`: the specification, with the schemas above
  completed, the physical schema, the migration, the eligibility rule's exact
  placement, and the gates.
- `docs/plan/builtin-tools.md`: the tool census (`docs/plan/builtin-tools.md:124`)
  and a Milestone section carrying the classification above.
- `docs/plan/tool-system.md`: the `records` domain in the partition table.
- `docs/plan/policy-and-approvals.md` and `docs/plan/http-api-and-streaming.md`:
  the two scopes with a count reconciled with the code, the
  `turn_origin_trusted` condition, and the three `tool_rules` rows.
- `docs/plan/memory-formation-and-consolidation.md`: the `recorded_elsewhere`
  eligibility rule.
- `docs/plan/context-engine.md`: the catalog's assembly row, its line in the
  cap table, its yield step, the `records_catalog_tokens` budget field, the
  framing sentence, and the extended stability script.
- `docs/plan/milestone-map.md` and `docs/plan/readiness.md`: the gates and
  the milestone's row.
- `AGENTS.md` routing table, `mkdocs.yml`, the ADR-0036 knob inventory, and
  an operator page for the flag.

## Owner decisions, 2026-10-22

The owner answered the draft's questions:

1. Untrusted turns: "Offer an approval." Section 5 is rewritten on it.
2. Automatic surfacing: after reading the trade-off, "Put the catalog
   context item in the first slice so the model always knows which series
   exist and can nudge me." Section 9 is rewritten on it, with the
   context-engine obligations named.
3. Sensitivity: "Start with a sensible default but do not add too much
   friction for the sake of privacy." Section 6 applies it: `sensitive` by
   default, set once, never asked about.
4. Series creation: "Explicit confirmation for a new series."
   `records.create_series` under an approval is that confirmation.
5. Native screen: "not needed yet. reading through the chat client is
   sufficient." Section 10 drops the route and the screen.

## Calls made in this draft, for the owner to check

A reviewer read the draft as a builder would and asked what it would have to
guess. These are the answers, each a call made here rather than something
the owner said:

- `MEMORY` origin trust is accepted for appends without a card, because
  recall labels most turns `MEMORY`; only `EXTERNAL_UNTRUSTED` asks.
- The untrusted-turn approval is a `tool_rules` row with a new
  `turn_origin_trusted` condition, because the trust overlay cannot escalate
  a `NONE` side effect and a non-`NONE` class would misdescribe the write.
- Explicit series creation is an approval on `records.create_series`,
  because an approval card is enforceable where a model-asked question is
  not.
- Deletion is approval-gated and not trust-gated, so an approval is never
  followed by a refusal.
- The catalog is labelled `INTERNAL_TOOL`, not `MEMORY`, so its presence does
  not turn a fresh chat's append into an approval.
- `cadence` is optional and defaults to none, so a nudge rests on an
  expectation the owner declared rather than on the model's guess, and a
  series logged ad hoc is never called overdue.
- The `tool_rules` rows are unconditional in the profile file; the flag
  registers tools only.
- Model-facing reads use the constant `restricted` ceiling `memory.search`
  uses; the sensitivity label serves the HTTP and native surfaces.
- Scope holders: the owner through every paired surface writes; schedules and
  delegations read.
- The formation rule matches every component value of one entry as numeric
  tokens, through the shared eligibility gate, with segment selection reading
  the append events.
- Principal erasure is a store-level contract mirroring People's.

## Acceptance status

Accepted on 2026-10-22. The specification is checked in with its ten gates
registered; implementation proceeds under Milestone 34 in the build sequence
the specification states, and no gate is reported green by authorization.
