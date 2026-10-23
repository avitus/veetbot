---
title: Owner Records
status: design
canonical: true
---

# Owner records

Milestone 34 is the owner-authorized October 22, 2026 entry of roadmap item
B14, recorded in [ADR-0239](../adr/0239-owner-records-and-numeric-series.md).
The engineering plan states the requirement in its Milestone 34 section; this
document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md) and changes no requirement of any
other specification; where it adds to one, the addition is named in
[What changes in other documents](#what-changes-in-other-documents).

The owner asked whether the agent could keep health metrics such as blood
pressure over time. Nothing could: a belief keeps the latest value and
supersedes the rest
([memory-formation-and-consolidation.md](memory-formation-and-consolidation.md)),
the sandbox workspace is a cache
([sandbox-isolation.md](sandbox-isolation.md)), and a knowledge document is
read-only input. This milestone gives the owner's principal a bounded record
store: named numeric series the agent appends to, lists, queries and deletes
through five tools, with a catalog of the series in every request so the
model knows what exists and can say when a series is due.

## What this document is responsible for

- The domain model: a series, its one-to-four named numeric components, an
  entry, the normalization that makes a name unique, and every bound.
- The five tools in the `records` domain: closed schemas, result shapes, the
  closed reason-code vocabulary, the descriptions the model reads, and every
  `ToolSpec` field.
- The policy condition `turn_origin_trusted` and the three `tool_rules` rows.
- The scans, the sensitivity label and the ceiling reads apply.
- Persistence: two tables, their indexes and row-level security, the store
  port, both adapters, the append replay contract, and erasure.
- The four events and the session-deletion cascade.
- The formation eligibility rule `recorded_elsewhere`.
- The records catalog context item and every obligation the context engine
  places on a new item type.
- Configuration, scopes and who holds them.
- The build sequence and the ten hard gates.

Out of scope, by the owner's decisions in ADR-0239: SQL of any kind, HTTP
routes, a native screen, unit conversion, derived series, charts, and import
from files. A later amendment may add any of them; none is assumed here.

## Domain model

```python
class RecordComponent(BaseModel):
    name: str            # ^[a-z][a-z0-9_]{0,31}$, unique within the series
    unit: str | None     # 1–16 characters after trimming, or absent

class RecordSeries(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    name: str                             # normalized, unique per principal
    display_name: str                     # the owner's spelling, 1–64 chars
    components: tuple[RecordComponent, ...]  # 1–4, fixed after creation
    sensitivity: Sensitivity              # SENSITIVE by default; >= INTERNAL
    cadence: RecordCadence | None         # DAILY | WEEKLY | MONTHLY | None
    revision: int
    created_at: datetime
    updated_at: datetime

class RecordEntry(BaseModel):
    id: UUID
    series_id: UUID
    tenant_id: str
    principal_id: str
    observed_at: datetime                 # aware UTC
    observed_precision: RecordPrecision   # EXACT | DAY
    values: dict[str, Decimal]            # exactly the declared components
    note: str | None                      # <= 200 characters after trimming
    note_withheld: bool                   # instruction-like note, not rendered
    idempotency_key: str                  # the pipeline's key for the call
    source_session_id: UUID | None
    source_event_id: UUID | None          # nulled by session deletion
    created_at: datetime
```

**Normalization.** `name` is `display_name` after NFC normalization, Unicode
case folding, trimming, and collapsing internal whitespace to one space. Two
display names that normalize alike are the same series. A name is 1 to 64
characters after normalization and contains at least one letter or digit.

**Values.** Every value is a finite `Decimal` with at most twelve integer
digits and at most six fraction digits; the stored scale is six. A value
outside that range is `records.value_out_of_range`. The canonical text of a
value is its six-place quantized form (`128.000000`); its short form drops
trailing zeros and a trailing point (`128`, `82.4`). Both forms are what the
formation rule matches.

**Time.** `observed_at` arrives as an ISO 8601 instant with an offset and is
stored in UTC. It may not be later than the composition clock plus five
minutes (`records.observed_in_future`) and defaults to the clock when
omitted. `DAY` precision keeps the civil date in the offset the owner gave
and renders no time; the stored instant is that date at noon in that
offset, so a day-precision entry sorts inside its day and never crosses a
date line when rendered elsewhere.

**Cadence.** `DAILY`, `WEEKLY` and `MONTHLY` mean one, seven and thirty days.
A series with a cadence is *due* when it has no entry or when the clock is
at or past the last `observed_at` plus the interval; the due instant is that
sum. A series without a cadence is never due.

**Bounds.** A principal holds at most `RECORDS_MAX_SERIES` series (default
64, `records.series_limit`) and a series at most
`RECORDS_MAX_ENTRIES_PER_SERIES` entries (default 100,000,
`records.entry_limit`). A note is at most 200 characters after trimming. A
query page is 1 to 1,000 entries and a bucketed query at most 1,000 buckets.

## The five tools

| Tool | Bounded purpose |
| --- | --- |
| `records.list` | The principal's series: display name, components, units, entry count, first and last observed time, sensitivity, cadence and due state. No arguments. |
| `records.create_series` | Declare a series by name with its components, units, sensitivity and cadence. Requires approval, which is the owner's confirmation that the series should exist. |
| `records.append` | Add one entry to an existing series: values for every declared component, the observed time and precision, an optional note. |
| `records.query` | Entries of one series in a half-open time window, newest first by default, with a per-component summary over the whole window, or per-bucket aggregates instead of entries. |
| `records.delete` | Remove one entry, or a whole series with its entries. Requires approval in every turn. |

All five are `CAPABILITY`, `BUILTIN`, `in_process`, `INTERNAL_TOOL` output
trust, registered only with `AGENT_RECORDS_ENABLED=1`, and declared in the
`records` domain, which joins the partition table in
[tool-system.md](tool-system.md) as a builtin domain registered at build time
under its flag, as `people` is.

### Classification

```text
name                   side_effect  risk    idempotency               scopes
---------------------  -----------  ------  ------------------------  -------------
records.list           NONE         LOW     READ_ONLY                 records.read
records.query          NONE         LOW     READ_ONLY                 records.read
records.create_series  NONE         MEDIUM  IDEMPOTENT                records.write
records.append         NONE         MEDIUM  CONDITIONALLY_IDEMPOTENT  records.write
records.delete         NONE         MEDIUM  IDEMPOTENT                records.write

name                   timeout_s  max_output_bytes  allow_parallel
---------------------  ---------  ----------------  --------------
records.list           10         16384             yes
records.query          10         262144            yes
records.create_series  10         4096              no
records.append         10         4096              no
records.delete         10         4096              no
```

`side_effect` is `NONE` on the grounds `memory.remember` and
`knowledge.ingest` already stand on: the write lands in the platform's own
governed store and leaves the host for nowhere. Two obligations follow for
`records.append`, because the executor sets the effect watermark only for
non-`NONE` side effects and re-invokes a tool on replay exactly as on
re-execution: the tool awaits `context.mark_effect_sent()` before its insert,
and when invoked with an idempotency key the series already holds it returns
the stored entry with `replayed: true` and inserts nothing. `records.delete`
and `records.create_series` are `IDEMPOTENT`: deleting what is gone and
declaring what exists change nothing.

### Schemas

Every schema is closed (`additionalProperties: false`) and every bound is in
it, so a malformed call fails at validation and never reaches the service.

`records.create_series`:

```json
{
  "type": "object",
  "properties": {
    "series": {"type": "string", "minLength": 1, "maxLength": 64},
    "components": {
      "type": "array", "minItems": 1, "maxItems": 4,
      "items": {
        "type": "object",
        "properties": {
          "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
          "unit": {"type": "string", "minLength": 1, "maxLength": 16}
        },
        "required": ["name"], "additionalProperties": false
      }
    },
    "sensitivity": {"type": "string", "enum": ["sensitive", "restricted"]},
    "cadence": {"type": "string", "enum": ["daily", "weekly", "monthly"]}
  },
  "required": ["series", "components"],
  "additionalProperties": false
}
```

`records.append`:

```json
{
  "type": "object",
  "properties": {
    "series": {"type": "string", "minLength": 1, "maxLength": 64},
    "values": {
      "type": "object", "minProperties": 1, "maxProperties": 4,
      "propertyNames": {"pattern": "^[a-z][a-z0-9_]{0,31}$"},
      "additionalProperties": {"type": "number"}
    },
    "observed_at": {"type": "string", "format": "date-time"},
    "precision": {"type": "string", "enum": ["exact", "day"]},
    "note": {"type": "string", "minLength": 1, "maxLength": 200}
  },
  "required": ["series", "values"],
  "additionalProperties": false
}
```

Values arrive as JSON numbers; the tool parses the request body's numbers as
`Decimal` before validation so that `82.4` is stored as `82.400000` and never
as a binary float. The canonical argument form the pipeline hashes is the
parsed `Decimal` text, so two calls that differ only in `82.4` versus `82.40`
share one idempotency key.

`records.query`:

```json
{
  "type": "object",
  "properties": {
    "series": {"type": "string", "minLength": 1, "maxLength": 64},
    "since": {"type": "string", "format": "date-time"},
    "until": {"type": "string", "format": "date-time"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
    "order": {"type": "string", "enum": ["asc", "desc"]},
    "bucket": {"type": "string", "enum": ["day", "week", "month"]}
  },
  "required": ["series"],
  "additionalProperties": false
}
```

`records.delete`:

```json
{
  "type": "object",
  "properties": {
    "series": {"type": "string", "minLength": 1, "maxLength": 64},
    "entry_id": {"type": "string", "format": "uuid"}
  },
  "required": ["series"],
  "additionalProperties": false
}
```

`records.list` takes `{"type": "object", "properties": {},
"additionalProperties": false}`.

### Results

Every success returns one line of `content` the model can repeat to the owner
and a `structured` body the harness and the clients read.

- `records.list` → `{"series": [SeriesSummary...], "count": n}` where a
  summary carries `series_id`, `display_name`, `components`, `sensitivity`,
  `cadence`, `entry_count`, `first_observed_at`, `last_observed_at`, `due`
  and `due_since`. Ordered by normalized name.
- `records.create_series` → the summary of the created or already-existing
  series and `created: bool`.
- `records.append` → `{"entry_id", "series": display_name, "values",
  "observed_at", "precision", "entry_count", "replayed": bool,
  "note_withheld": bool}`.
- `records.query` without `bucket` → `{"series", "window": {"since",
  "until"}, "entries": [...], "summary": {component: {"count", "min", "max",
  "mean", "first": {"value", "observed_at"}, "last": {...}}}}`. The summary
  covers the whole window, not the page. With `bucket` → `{"series",
  "window", "bucket", "buckets": [{"start", "end", component: {"count",
  "min", "max", "mean"}}]}` and no entries. Empty buckets are omitted.
- `records.delete` → `{"series", "entries_removed": n, "series_removed":
  bool}`; a delete of what is already gone succeeds with zeros.

The window is half-open, `[since, until)`. `since` defaults to the earliest
entry and `until` to the clock. `order` defaults to `desc` and `limit` to
100. Buckets align to the civil day, ISO week (Monday) or month in the
timezone the runtime metadata row renders the current date in, and a window
that would produce more than 1,000 buckets is `records.window_too_large`.
`mean` is the `Decimal` sum divided by the count, quantized half-even to six
places, so a window renders the same bytes on every call and on both
adapters.

### Reason codes

The closed vocabulary, each a structured refusal with no row written:

```text
records.series_unknown        append, query or delete named a series that does not exist
records.series_exists         create_series named an existing series with different components
records.component_mismatch    values carry a key not declared, or lack a declared one
records.value_out_of_range    a value is not finite or exceeds 12 integer or 6 fraction digits
records.observed_in_future    observed_at is past the clock plus five minutes
records.series_limit          the principal already holds RECORDS_MAX_SERIES series
records.entry_limit           the series already holds RECORDS_MAX_ENTRIES_PER_SERIES entries
records.window_too_large      a bucketed query spans more than 1,000 buckets
records.secret_detected       a name, component name or note matched the secret scan
records.name_rejected         a series or component name matched the injection scan
```

The tool pipeline's own refusals, scope denial among them, keep their
existing codes. `records.series_unknown` from `records.append` is the signal
that `records.create_series` must come first; the description says so.

### What the model reads

Each description is three or four sentences, stable, and says what the tool
cannot infer. `records.create_series` says that it needs the owner's approval
and that a series must exist before an append. `records.append` says that
values are the owner's words, that the series must already exist, and that an
append in a turn that read external content asks the owner first.
`records.query` says the summary covers the window and that `bucket` replaces
entries with aggregates. `records.delete` says it asks the owner. All five say
that records are the owner's own measurements and are not memory.

## Policy

### The condition

`PolicyCondition.TURN_ORIGIN_TRUSTED` (`turn_origin_trusted`) holds when the
action's `origin_trust` is not `EXTERNAL_UNTRUSTED`. That is the same test
the deterministic layer's trust overlay applies to a tool without a
tool-name row, and it is deliberately not the engine's "may authorize" set,
which excludes `MEMORY`: recall labels an ordinary turn `MEMORY`, and a
condition on that set would ask for a tap on most mornings. A turn that read
an email, a web page, a knowledge passage or any external tool result is
labelled `EXTERNAL_UNTRUSTED` by the executor and fails the condition.
Internal tool results, `records.list` among them, do not lower the label.

### The rows

Three rows in the default profile, unconditional in the file because a
profile is a file and not a flag; with the tools unregistered the rows are
inert, since an unknown tool is denied anyway:

```yaml
records.append:
  condition: turn_origin_trusted
  decision: allow
  otherwise: require_approval
records.create_series:
  decision: require_approval
records.delete:
  decision: require_approval
```

`human_confirms_arguments` is set on none of them. The approval card already
carries the tool's arguments, so the owner sees the series, values and time
of an append from an untrusted turn, the name, components and units of a new
series, and the series or entry a delete removes. Nothing is written before
an approval resolves; an approved call runs the tool exactly as an allowed
one; a denied or expired approval writes nothing. The records service keeps
no trust check of its own: the profile is the one place the rule lives, its
version is the profile's hash, and hardline rules run first and are
untouched. The trust overlay's side-effect exclusion for `NONE` still holds
for these tools, which is why the row, not the overlay, carries the rule.

## Scans and the sensitivity ceiling

Series names, component names and notes pass the shared secret scan and
injection scan that governed memory writes pass. A secret-shaped value in any
of them refuses the whole call with `records.secret_detected` and stores
nothing. An instruction-like series or component name is refused with
`records.name_rejected`, because a name needs no instructions and the catalog
renders names on every request. An instruction-like note is stored with
`note_withheld` set and is rendered as `[note withheld]` in every
model-facing result; the note itself leaves the store only through the
owner's own future surfaces. Refusal messages never quote the rejected text.

Every series carries a `Sensitivity`, set at creation and never asked about
again: `sensitive` by default, `restricted` when the owner says so, never
below `internal`. Model-facing reads and the catalog are constructed with
the ceiling their surface's recall uses, which for the owner's chat is the
constant `restricted` that `memory.search` applies, so the owner sees every
series they own and no creation or append asks a privacy question. A series
above the ceiling of a more restricted surface is absent from `records.list`,
`records.query` and the catalog, and `records.append` to it is
`records.series_unknown`.

## Persistence

### Tables

`record_series`:

```text
id               uuid         primary key
tenant_id        text         not null
principal_id     text         not null
name             text         not null   -- normalized
display_name     text         not null
components       jsonb        not null   -- [{"name": ..., "unit": ...}], 1–4
sensitivity      text         not null
cadence          text         null
revision         integer      not null
created_at       timestamptz  not null
updated_at       timestamptz  not null
unique (tenant_id, principal_id, name)
unique (id, tenant_id, principal_id)          -- target of the scoped FK
check (jsonb_typeof(components) = 'array'
       and jsonb_array_length(components) between 1 and 4)
```

`record_entries`:

```text
id                  uuid         primary key
series_id           uuid         not null
tenant_id           text         not null
principal_id        text         not null
observed_at         timestamptz  not null
observed_precision  text         not null
values              jsonb        not null   -- {"systolic": "128.000000", ...}
note                text         null
note_withheld       boolean      not null default false
idempotency_key     text         not null
source_session_id   uuid         null
source_event_id     uuid         null
created_at          timestamptz  not null
foreign key (series_id, tenant_id, principal_id)
    references record_series (id, tenant_id, principal_id) on delete cascade
unique (series_id, idempotency_key)
index (series_id, observed_at desc)
check (jsonb_typeof(values) = 'object')
```

Values are stored as the canonical six-place text of each `Decimal`, never as
JSON numbers, so the round trip is exact on both adapters and a row's bytes
are reproducible. The one-value-per-declared-component invariant is enforced
by the service inside the append transaction and by the store contract suite;
the composite foreign key makes a cross-owner link fail at write time, and
both tables carry forced tenant row-level security like every table
[people-and-relationships.md](people-and-relationships.md) adds. The
migration follows ADR-0031: one head, structure only, `downgrade()` written.

### The store port

```python
class RecordStore(Protocol):
    async def create_series(self, series: RecordSeries) -> tuple[RecordSeries, bool]
    async def get_series(self, scope: RecordScope, name: str) -> RecordSeries | None
    async def list_series(self, scope: RecordScope, ceiling: Sensitivity) -> list[SeriesSummary]
    async def append(self, entry: RecordEntry) -> tuple[RecordEntry, bool]
    async def query(self, scope: RecordScope, series_id: UUID, window: Window,
                    order: Order, limit: int) -> list[RecordEntry]
    async def summarize(self, scope: RecordScope, series_id: UUID, window: Window) -> dict[str, ComponentSummary]
    async def bucket(self, scope: RecordScope, series_id: UUID, window: Window,
                     bucket: Bucket, zone: ZoneInfo) -> list[BucketRow]
    async def delete_entry(self, scope: RecordScope, series_id: UUID, entry_id: UUID) -> int
    async def delete_series(self, scope: RecordScope, series_id: UUID) -> tuple[int, bool]
    async def detach_session(self, session_id: UUID) -> int
    async def erase_principal(self, scope: RecordScope) -> int
```

`RecordScope` is `(tenant_id, principal_id)` and every method filters on it;
a series of another principal is `None` or zero, never an error that says it
exists. `create_series` returns `(existing, False)` for an equal declaration,
raises `SeriesExists` for a different one under the same name, and `(new,
True)` otherwise. `append` returns `(stored, True)` when the series already
holds the entry's idempotency key and inserts nothing. `summarize` and
`bucket` compute in `Decimal` on both adapters. The in-memory adapter and the
PostgreSQL adapter share one contract suite, and the contract suite is where
the invariant, the replay and the isolation are proven before any tool
exists.

### Erasure

Records are owner-authored, not derived, so they outlive their source.
Session deletion's enumerated cascade gains `record_entries`: it nulls
`source_session_id` and `source_event_id` for the session and keeps the rows.
`erase_principal` removes every series and entry of the scope in one
transaction and is the store-level contract that principal erasure composes,
mirroring the People store. Deletion through the tool is a hard delete in one
transaction, visible to no read afterwards; nothing re-forms from a deleted
record because nothing forms from records at all.

## Events

Four event types join Section 6.8's list, each with `payload_schema_version`
1 and appended to the run's session in the same unit of work as the write:

```text
records.series.created   series_id, name, display_name, components, sensitivity, cadence
records.entry.appended   series_id, entry_id, observed_at, observed_precision, values, note_withheld
records.entry.deleted    series_id, entry_id
records.series.deleted   series_id, entries_removed
```

The row is canonical and the event is provenance: `records.entry.appended`
carries the values so that a rebuild can replay them, as `memory.formed`
carries a statement; the two deletion events carry identifiers and counts
only. No event carries a note.

## Formation: `recorded_elsewhere`

A record is not a belief and never becomes one. Memory formation's segment
selection, which reads the owner's messages, additionally reads the
`records.entry.appended` events of the segment and collects each entry's
values in both canonical forms; that is the one change to what formation
reads. The shared deterministic eligibility gate gains a `recorded_values`
input: a candidate statement is ineligible when it contains, as standalone
numeric tokens, every component value of one entry appended in the same
segment. "128 over 82" against a `{128, 82}` entry is suppressed; "I ran 82
km" in the same segment is not; a one-component weight entry suppresses only
a statement carrying that weight. Because the rule lives in the shared gate,
every extractor that passes candidates through it, deterministic formation,
People formation, provider extraction, distillation and reconsolidation
alike, inherits it without its own copy. The suppression is counted with the
run's rejected candidates and named `recorded_elsewhere` in the formation
diagnostic. `memory.remember` is not refused for a measurement: an explicit
write stays the model's choice, steered by the tool descriptions.

## The records catalog in context

The owner decided that the model should always know which series exist and
be able to say when one is due. The catalog is one platform-rendered item,
and the context engine's obligations for a new item type are met here.

**Content.** One line per series the ceiling admits, ordered by normalized
name:

```text
blood pressure (systolic mmHg, diastolic mmHg): 31 entries, last 2026-10-21 07:40 -07:00, due daily since 2026-10-22 07:40 -07:00
weight (kg): 12 entries, last 2026-10-19, logged ad hoc
sleep (hours): no entries, due daily
```

Never a value. Times render in the timezone the runtime metadata row renders
the current date in; a day-precision entry renders its date only. A series
without a cadence says `logged ad hoc`; one whose cadence has not elapsed
says `due weekly`, `due daily` or `due monthly` with no `since`. When more
than sixteen series exist the sixteen earliest by name render and a last line
says `N more series; records.list has them`. A principal with no series
renders no bytes at all, so a session without records is byte-identical to
one built today.

**Region and class.** `ContextClass.RECORDS_CATALOG` maps to
`ContextRegion.BODY` in the region table, because the catalog changes
whenever an entry lands and may never touch the prefix; the item type carries
that class, and the builder's region assertion covers it. It enters the fixed
assembly order of [context-engine.md](context-engine.md) as row 13a, after
the in-turn recall and correction lines and before the runtime metadata.

**Trust and envelope.** The item is labelled `INTERNAL_TOOL` and rendered
inside the ordinary envelope with `source="records.catalog"`, exactly as a
`records.list` result would be. Its names were approved by the owner on a
card and refused both scans at creation, and `INTERNAL_TOOL` content lowers
neither the builder's request-level origin stamp nor the executor's turn
label: a fresh chat whose only added item is the catalog keeps `USER` origin
trust, and an append in it stores without a card.

**Budget and yield.** An absolute class cap of 16 series and 600 tokens, with
no scaling by the window; `ContextBudget` gains `records_catalog_tokens`.
Under pressure the catalog yields whole, never truncated, after knowledge
passages drop and before in-turn recall trims, because a shortened catalog
misstates what exists and one tool call recovers it.

**Purity.** `build()` reads no store. The catalog is fetched in the same
per-request preparation that computes in-turn recall, with the principal's
scope and the surface's ceiling, and handed to `build()` as an input; the
determinism gate holds because two builds on the same inputs are
byte-identical. The catalog is absent when the flag is off, when the run's
principal lacks `records.read`, or when the store holds no series.

**The framing sentence.** Row 2 of the assembly order gains one fixed
sentence of platform text, cache-safe because it never varies:

> A records catalog, when present, lists the owner's numeric series with
> their last entries; it holds names and dates, never values. A series marked
> due may be mentioned once per session, never while the owner is mid-task,
> and the catalog is not a request to log.

**Stability.** The engine's fifty-turn prefix script is extended, as a
records gate rather than an amendment to the engine's, with a series created
and entries appended mid-session; it must still yield exactly one
`prefix_sha256`.

**Reminders without a chat open** are an ordinary schedule the owner creates
in plain words. Scheduled runs hold `records.read`, see the catalog and can
call `records.query`; no new mechanism is needed.

## Configuration and scopes

Three knobs, all in the ADR-0036 inventory and `tests/unit/test_config.py`:

```text
AGENT_RECORDS_ENABLED            bool  default 0   registers the five tools and the catalog
RECORDS_MAX_SERIES               int   default 64
RECORDS_MAX_ENTRIES_PER_SERIES   int   default 100000
```

`records.read` and `records.write` join the closed scope vocabulary of
[policy-and-approvals.md](policy-and-approvals.md) and the enumeration in
[http-api-and-streaming.md](http-api-and-streaming.md). Holders in the first
slice: the owner's principal carries both through the API and worker units
(`AUTH_SCOPES` in `deploy/veetbot.env.example`) and through every
owner-paired surface, because a reading typed into Telegram is the owner's
words as much as one typed into Chat; scheduled runs and delegated child
runs carry `records.read` only, since neither can know a value the owner has
not just said. The surface, schedule, notify and call env examples name
these; production env edits remain the owner's. A public caller or an
unpaired sender gains no owner scopes and therefore no records.

## Build sequence

Each step begins with its red test, and the gate a step turns green is named.

1. **Domain.** `domain/records.py`: the models, normalization, value and
   time validation, cadence and due. Unit tests over normalization pairs,
   value bounds, future instants and day precision.
2. **Store.** `ports/records.py`, the in-memory adapter, the PostgreSQL
   adapter, the migration, and the shared contract suite that proves scope
   isolation, the replay contract, the component invariant, `Decimal`
   summaries and erasure on both adapters. Gates 1, 3 and 6.
3. **Service.** `application/records.py`: create, append, query, delete,
   the scans, the events, and `detach_session` in the session-deletion
   cascade. Gates 5 and 6.
4. **Tools.** The five tools with their schemas, descriptions and
   classification; registration under the flag; the `records` domain in the
   partition table; the two scopes in the vocabulary. Gate 8.
5. **Policy.** `PolicyCondition.TURN_ORIGIN_TRUSTED`, its evaluator, the
   three rows in `default.yaml`, and the policy determinism tests. Gate 2.
6. **Query determinism and listing.** Fixture-driven byte-stability tests
   across adapters and calls. Gates 4 and 9.
7. **Formation.** Segment selection reads the append events; the shared
   eligibility gate takes `recorded_values`; the diagnostic counts
   `recorded_elsewhere`. Gate 7.
8. **Catalog.** `ContextClass.RECORDS_CATALOG`, the item, the renderer, the
   budget field, the yield step, the preparation hook, the framing sentence,
   and the extended stability script. Gate 10.
9. **Documents.** The additions listed under
   [What changes in other documents](#what-changes-in-other-documents), the
   env examples, and each gate's `check` repointed from the pending check to
   its test.

## Hard gates

1. **Scope isolation.** Another principal's series is `None` or zero to every
   store method and `records.series_unknown` to every tool, on both adapters,
   with forced row-level security and the scoped foreign key refusing a
   cross-owner link at write time. **M34.**
2. **Trust gate.** A `records.append` in a turn whose origin trust is
   `EXTERNAL_UNTRUSTED` evaluates to `REQUIRE_APPROVAL` and nothing is written
   before the approval resolves; an approved call stores the entry and a
   denied or expired one stores nothing; an append in a turn that holds a
   `memory.search` result is allowed without a card; `turn_origin_trusted`
   holds for `USER`, `MEMORY` and `INTERNAL_TOOL` origins and fails for
   `EXTERNAL_UNTRUSTED`; a create or delete never executes without a resolved
   approval, in any turn. **M34.**
3. **Append replay.** A `records.append` re-executed after a simulated crash
   with the effect watermark set stores one entry, and the re-invoked tool
   returns the stored entry with `replayed` set for its idempotency key, on
   both adapters. **M34.**
4. **Query determinism.** The window summary and each bucketed aggregate for
   a fixed fixture are byte-identical across calls and across adapters, the
   window is exact on its half-open bounds, and a window past 1,000 buckets
   is refused. **M34.**
5. **Scans.** A secret-shaped name, component name or note refuses the call
   and stores nothing; an instruction-like name is refused; an
   instruction-like note is stored withheld and absent from every
   model-facing result. **M34.**
6. **Erasure.** Deleting a series removes its entries from every read in one
   transaction and emits an event without values; deleting the source session
   keeps the entry and nulls its provenance; `erase_principal` removes
   everything, on both adapters. **M34.**
7. **No belief per value.** A consolidation run over a session containing
   appends forms no belief whose statement carries every component value of
   an entry appended in the same segment, on every extractor that shares the
   eligibility gate, while a statement carrying only one value of a
   two-component entry still forms. **M34.**
8. **Registration.** Startup registration validation accepts all five tools
   with every `ToolSpec` field, the two scopes are in the closed vocabulary,
   the three `tool_rules` rows are in the default profile, and with
   `AGENT_RECORDS_ENABLED=0` the tools are unregistered, the rows are inert
   and the roster is unchanged. **M34.**
9. **Listing stable.** `records.list` output and the catalog rendering for
   the same store are byte-identical across calls. **M34.**
10. **Catalog.** The item declares the body region; a principal with no
    series renders no bytes; the rendering carries names, components, counts,
    last times and due state and never a value; a series above the surface
    ceiling is absent; the extended fifty-turn script with a series created
    and entries appended mid-session yields one `prefix_sha256`; under
    synthetic pressure the catalog drops whole after knowledge passages and
    before recall trims; and a request whose only added item is the catalog
    keeps `USER` origin trust. **M34.**

## Tracked metrics

- Appends per principal per week.
- Append approvals requested, approved and denied.
- Catalog tokens per request, and due-series mentions per session.
- Query latency at the 95th percentile.
- Refusals by reason code.
- Owner deletions per month.

## What changes in other documents

Each addition lands with the step that implements it; none changes an
existing requirement.

- [builtin-tools.md](builtin-tools.md): the census of tools declared by other
  specifications gains the five, and a Milestone 34 section carries the
  classification above.
- [tool-system.md](tool-system.md): the `records` domain in the partition
  table.
- [policy-and-approvals.md](policy-and-approvals.md): the two scopes, a
  count reconciled with `src/agent_core/policy/scopes.py`, the
  `turn_origin_trusted` condition, and the three rows.
- [http-api-and-streaming.md](http-api-and-streaming.md): the two scopes in
  the enumeration.
- [memory-formation-and-consolidation.md](memory-formation-and-consolidation.md):
  the `recorded_elsewhere` eligibility rule.
- [context-engine.md](context-engine.md): row 13a, the cap-table line, the
  yield step, the budget field, and the framing sentence.
- [event-log-and-persistence.md](event-log-and-persistence.md): the four
  event types and the cascade's new table.
- [milestone-map.md](milestone-map.md) and [readiness.md](readiness.md): the
  ten gates and the milestone's row.
- `AGENTS.md`, `mkdocs.yml`, the ADR-0036 knob inventory, and the env
  examples.

## Decisions

- Values are `Decimal` end to end and are stored as canonical text, never as
  floats or JSON numbers, so summaries are reproducible.
- `side_effect: NONE` with the two tool-side replay obligations, rather than
  a new side-effect class, because the write leaves the host for nowhere.
- The untrusted-turn approval is a profile row with a condition, not a
  service check, so the rule has one home and one version.
- The catalog is `INTERNAL_TOOL`, not `MEMORY`, so its presence never turns
  a fresh chat's append into an approval.
- Series creation is an approval, because an approval card is enforceable
  where a model-asked question is not.
- Records never decay, never expire and never form beliefs; the formation
  rule is exact token matching over every component value of one entry.

## Open questions

- Whether a `restricted` series should also be withheld from scheduled runs
  by default. Today the schedule's ceiling decides, as it does for recall.
- Whether the catalog should name the unit of a one-component series inline
  (`weight (kg)`) or after the count; the renderer above does the former and
  a change is a renderer change, not a contract change.
