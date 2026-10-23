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
- The approval classification of the five tools and the service's refusal
  of an append in an untrusted turn.
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
    source_event_id: UUID | None          # always null in this slice; nulled by session deletion
    created_at: datetime
```

These models live in `domain/records.py` together with every value the store
port exchanges (`Window`, `Order`, `Bucket`, `SeriesSummary`, `ValueAt`,
`ComponentSummary`, `BucketStats`, `BucketRow`) and the service's results
(`AppendResult`, `QueryResult`, `DeleteResult`, `CatalogLine`,
`RecordsCatalog`). `ports/records.py` re-exports the port's value types
beside `RecordStore` and `SeriesExists`, so a caller of the port imports
from one place.

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
stored in UTC; an instant without an offset is refused
(`records.invalid_arguments`), never read as local time. It may not be later than the composition clock plus five
minutes (`records.observed_in_future`) and defaults to the clock when
omitted. `DAY` precision keeps the civil date in the offset the owner gave
and renders no time; the stored instant is that date at noon in that
offset, so a day-precision entry sorts inside its day and never crosses a
date line when rendered elsewhere. The future refusal applies to the instant
the owner gave, and the stored noon is capped at the clock: an entry for
today whose local noon is still ahead is stored at the clock, so it is never
in the future (but never before its civil date's local midnight), sits inside the default query window and renders as its date
in the catalog. Query window edges are read by `parse_instant`, which needs an
offset and has no future bound, so an `until` days ahead is accepted; an edge
without an offset is `records.invalid_arguments` with a detail naming the
edge.

**Cadence.** `DAILY`, `WEEKLY` and `MONTHLY` mean one, seven and thirty days.
A series with a cadence is *due* when it has no entry or when the clock is
at or past the last `observed_at` plus the interval; the due instant is that
sum. A series without a cadence is never due.

**Bounds.** A principal holds at most `MAX_SERIES_PER_PRINCIPAL` series (64,
`records.series_limit`) and a series at most `MAX_ENTRIES_PER_SERIES`
entries (100,000, `records.entry_limit`). The entry limit applies only to a
genuinely new row: the service looks the idempotency key up first, so a
replay of an entry the series already holds succeeds at the limit. A note is
at most `NOTE_MAX_CHARACTERS` (200) characters after trimming. The three are
module constants in `domain/records.py`, not configuration knobs. A query
page is 1 to 1,000 entries and a bucketed query at most 1,000 buckets.

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
name                   side_effect      risk    idempotency               scopes
---------------------  ---------------  ------  ------------------------  -------------
records.list           NONE             LOW     READ_ONLY                 records.read
records.query          NONE             LOW     READ_ONLY                 records.read
records.create_series  EXTERNAL_WRITE   MEDIUM  IDEMPOTENT                records.write
records.append         NONE             MEDIUM  CONDITIONALLY_IDEMPOTENT  records.write
records.delete         EXTERNAL_DELETE  MEDIUM  IDEMPOTENT                records.write

name                   timeout_s  max_output_bytes  allow_parallel
---------------------  ---------  ----------------  --------------
records.list           10         16384             yes
records.query          10         262144            yes
records.create_series  10         4096              no
records.append         10         4096              no
records.delete         10         4096              no
```

`records.list`, `records.query` and `records.append` are `NONE` on the
grounds `memory.remember` and `knowledge.ingest` already stand on: the write
lands in the platform's own governed store and leaves the host for nowhere.
`records.create_series` is `EXTERNAL_WRITE` and `records.delete`
`EXTERNAL_DELETE`. Their writes leave the host for nowhere either; the class
is what makes the deterministic matrix require the owner's approval for them
in every turn, with no profile row (see [Policy](#policy)). Two obligations
follow for `records.append`, because the executor sets the effect watermark only for
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
  bool}`. An entry delete on an existing series whose entry is already gone
  succeeds with zeros; a delete that names a series the principal does not
  have, whether for one entry or the whole series, is `records.series_unknown`,
  so a stranger's delete is indistinguishable from a missing one.

The window is half-open, `[since, until)`. `since` defaults to the earliest
entry and `until` to the clock plus the five-minute future skew, so an entry
stored at the edge of the skew is inside the default window. `order` defaults
to `desc` and `limit` to 100; entries order by `(observed_at, created_at,
id)`, ascending or reversed, so entries sharing an instant page the same way
on every call. Buckets align to the civil day, ISO week (Monday) or month in
UTC, which is the timezone the runtime metadata row renders the current date
in today; every records rendering uses UTC until an owner timezone exists. A
window that would produce more than 1,000 buckets is
`records.window_too_large`.
`mean` is the `Decimal` sum divided by the count, quantized half-even to six
places, so a window renders the same bytes on every call and on both
adapters.

### Reason codes

The closed vocabulary, each a structured refusal with no row written:

```text
records.series_unknown        append, query or delete named a series that does not exist
records.series_exists         create_series named an existing series with a different declaration
records.component_mismatch    values carry a key not declared, or lack a declared one
records.value_out_of_range    a value is not finite or exceeds 12 integer or 6 fraction digits
records.observed_in_future    observed_at is past the clock plus five minutes
records.series_limit          the principal already holds MAX_SERIES_PER_PRINCIPAL series
records.entry_limit           the series holds MAX_ENTRIES_PER_SERIES entries and the key is new
records.window_too_large      a bucketed query spans more than 1,000 buckets
records.secret_detected       a name, component name, unit or note matched the secret scan
records.name_rejected         a series name, component name or unit matched the injection scan
records.untrusted_turn        an append in a turn whose origin trust is EXTERNAL_UNTRUSTED
records.invalid_arguments     an argument the schema admits has an invalid value, such as
                              an observed_at without an offset
```

The first ten are the domain's refusals and the gate's census; the last two
are the service's trust refusal and the tool's fallback for a value the
schema cannot express. `records.secret_detected`, `records.name_rejected` and
`records.untrusted_turn` are `PERMISSION` failures, `records.series_unknown`
is `NOT_FOUND`, and the rest are `INVALID_ARGUMENTS`; none is retryable, and
each has a static message in the tool message table.

The tool pipeline's own refusals, scope denial among them, keep their
existing codes. `records.series_unknown` from `records.append` is the signal
that `records.create_series` must come first; the description says so.

### What the model reads

Each description is three or four sentences, stable, and says what the tool
cannot infer. `records.create_series` says that it needs the owner's approval
and that a series must exist before an append. `records.append` says that
values are the owner's words, that the series must already exist, and that in
a turn that read external content the tool refuses and the model asks the
owner to state the value in chat, which the refusal it receives there repeats.
`records.query` says the summary covers the window and that `bucket` replaces
entries with aggregates. `records.delete` says it asks the owner. All five say
that records are the owner's own measurements and are not memory.

## Policy

The slice changes no policy profile. Two mechanisms carry the owner's rules:
the deterministic matrix, through the classification above, and one check in
the records service.

### Approval by class

`records.create_series` is classified `EXTERNAL_WRITE` and `records.delete`
`EXTERNAL_DELETE`, and the deterministic matrix of
[policy-and-approvals.md](policy-and-approvals.md) requires approval for
both classes in every turn, so neither tool has a `tool_rules` row. A create
or a delete never executes before its approval resolves; an approved call
runs the tool exactly as an allowed one, once; a denied or expired approval
writes nothing. `human_confirms_arguments` is not set: the approval card
already carries the tool's arguments, so the owner sees the name, components
and units of a new series and the series or entry a delete removes.
`records.list`, `records.query` and `records.append` are `NONE` and the
matrix allows them. Hardline rules run first and are untouched.

### The untrusted-turn refusal

`records.append` stays `NONE`, and the trust overlay's side-effect exclusion
for `NONE` means the policy engine allows it in any turn. The records
service holds the rule instead. It refuses an append whose turn origin trust
is `EXTERNAL_UNTRUSTED` with `records.untrusted_turn`, a `PERMISSION` failure
that is not retryable, before it reads the store, so no row and no event is
written. A replay in such a turn is refused the same way, because the check
precedes the idempotency lookup. The test is the one the overlay applies to a
tool without a tool-name row, and deliberately not the engine's "may
authorize" set, which excludes `MEMORY`: recall labels an ordinary turn
`MEMORY`, and a rule on that set would refuse most mornings' appends. A turn
that read an email, a web page, a knowledge passage or any external tool
result is labelled `EXTERNAL_UNTRUSTED` by the executor and is refused;
`USER`, `MEMORY` and `INTERNAL_TOOL` origins append without a card. Internal
tool results, `records.list` among them, do not lower the label. The refusal
tells the model to ask the owner to state the value in chat, where the
owner's own next turn appends it.

### Why no profile row

The owner's decision of 2026-10-22 was an approval rather than a refusal for
an untrusted-turn append, through a new policy condition and three
`tool_rules` rows in the default profile
([ADR-0239](../adr/0239-owner-records-and-numeric-series.md), section 5). Any
edit to the default profile changes the shipped policy version, and
production's formation release evidence is bound to that version
(`tests/unit/test_release_evidence_bundle.py`): the edit would unbind the
evidence the running release was admitted on, which the owner has not
accepted. This slice therefore adds no condition and no row. The approval
path for untrusted appends, as the condition and its row, is the pending
alternative; it lands only when the owner accepts re-admitting the formation
release evidence, and it would replace the service check rather than sit
beside it.

## Scans and the sensitivity ceiling

Series names, component names, component units and notes pass the shared
secret scan and injection scan that governed memory writes pass; units are
scanned because the catalog renders them. A secret-shaped value in any
of them refuses the whole call with `records.secret_detected` and stores
nothing. An instruction-like series name, component name or unit is refused with
`records.name_rejected`, because a name needs no instructions and the catalog
renders names on every request. An instruction-like note is stored with
`note_withheld` set and is rendered as `[note withheld]` in every
model-facing result; the note itself leaves the store only through the
owner's own future surfaces. Refusal messages never quote the rejected text.

Every series carries a `Sensitivity`, set at creation and never asked about
again: `sensitive` by default, `restricted` when the owner says so, never
below `internal`. Model-facing reads and the catalog use the constant
`restricted` ceiling that `memory.search` applies for the owner's chat, so
the owner sees every series they own and no creation or append asks a
privacy question. The ceiling is a parameter of the two reads that list
series, `list_series` (behind `records.list`) and the catalog, and a series
above a lower ceiling is absent from both; in this slice every caller passes
`restricted`, and `records.query` and `records.append` take no ceiling, so a
surface with a lower ceiling needs that parameter threaded through before it
is admitted.

## Persistence

### Tables

The migration is `migrations/versions/f4b7d1c8a903_owner_records.py`.

`record_series`:

```text
tenant_id        text         not null
principal_id     text         not null
id               uuid         not null
name             text         not null   -- normalized
display_name     text         not null
components       jsonb        not null   -- [{"name": ..., "unit": ...}], 1–4
sensitivity      integer      not null   -- the Sensitivity ordinal, 0–3
cadence          text         null
revision         integer      not null
created_at       timestamptz  not null
updated_at       timestamptz  not null
primary key (tenant_id, principal_id, id)
unique (tenant_id, principal_id, name)
check (jsonb_typeof(components) = 'array'
       and jsonb_array_length(components) between 1 and 4)
check (sensitivity between 0 and 3)
```

`record_entries`:

```text
tenant_id           text         not null
principal_id        text         not null
id                  uuid         not null
series_id           uuid         not null
observed_at         timestamptz  not null
observed_precision  text         not null
values              jsonb        not null   -- {"systolic": "128.000000", ...}
note                text         null
note_withheld       boolean      not null
idempotency_key     text         not null
source_session_id   uuid         null
source_event_id     uuid         null
created_at          timestamptz  not null
primary key (tenant_id, principal_id, id)
constraint fk_record_entries_tenant_id_record_series
    foreign key (tenant_id, principal_id, series_id)
    references record_series (tenant_id, principal_id, id) on delete cascade
unique (tenant_id, principal_id, series_id, idempotency_key)
index (tenant_id, principal_id, series_id, observed_at)
index (source_session_id)                     -- the session-deletion cascade
check (jsonb_typeof(values) = 'object')
```

Both primary keys are composite and tenant-scoped, so the scoped foreign key
references the series' own key and a cross-owner link fails at write time.
`sensitivity` is stored as the ordinal of `Sensitivity` (`public` 0 to
`restricted` 3), so the ceiling read is an integer comparison; `cadence` is
the enum's text or null. Values are stored as the canonical six-place text
strings of each `Decimal` inside the JSONB object, never as JSON numbers, so
the round trip is exact on both adapters and a row's bytes are reproducible.
The one-value-per-declared-component invariant is enforced by the service
before the insert, by both adapters' `append`, and by the store contract
suite. Both tables carry forced tenant row-level security like every table
[people-and-relationships.md](people-and-relationships.md) adds. The
migration follows ADR-0031: one head, structure only, and a `downgrade()`
that refuses while either table holds a row.

### The store port

```python
class RecordStore(Protocol):
    async def create_series(self, series: RecordSeries) -> tuple[RecordSeries, bool]
    async def get_series(self, principal: Principal, name: str) -> RecordSeries | None
    async def count_series(self, principal: Principal) -> int
    async def list_series(self, principal: Principal, ceiling: Sensitivity) -> list[SeriesSummary]
    async def append(self, entry: RecordEntry) -> tuple[RecordEntry, bool]
    async def find_entry(self, principal: Principal, series_id: UUID,
                         idempotency_key: str) -> RecordEntry | None
    async def count_entries(self, principal: Principal, series_id: UUID) -> int
    async def query(self, principal: Principal, series_id: UUID, window: Window,
                    order: Order, limit: int) -> list[RecordEntry]
    async def summarize(self, principal: Principal, series_id: UUID,
                        window: Window) -> dict[str, ComponentSummary]
    async def bucket(self, principal: Principal, series_id: UUID, window: Window,
                     bucket: Bucket, zone: ZoneInfo) -> list[BucketRow]
    async def delete_entry(self, principal: Principal, series_id: UUID, entry_id: UUID) -> int
    async def delete_series(self, principal: Principal, series_id: UUID) -> tuple[int, bool]
    async def detach_session(self, session_id: UUID) -> int
    async def erase_principal(self, principal: Principal) -> int
```

The scope is the run's `Principal`, and every method filters on its
`tenant_id` and `principal_id`; a series of another principal is `None` or
zero, never an error that says it exists. `create_series` returns `(existing,
False)` for an equal declaration, raises `SeriesExists` for a different one
under the same name, and `(new, True)` otherwise. Declaration equality is
order-insensitive: two declarations are equal when they carry the same set of
component name and unit pairs, the same sensitivity and the same cadence, and
the stored series keeps the first declaration's component order. `append`
returns `(stored, True)` when the series already holds the entry's
idempotency key and inserts nothing; `find_entry` answers the same question
without writing, so the service applies the entry limit only to a genuinely
new row, and `count_series` and `count_entries` back the two limits. Queries
order by `(observed_at, created_at, id)`.

The PostgreSQL adapter writes rows with insert statements and computes
`summarize` and `bucket` with the same pure Python helpers the in-memory
adapter uses (`summarize_entries` and `bucket_entries` in
`adapters/persistence/records.py`), in `Decimal`, so the two adapters return
byte-identical summaries and buckets by construction rather than by two
implementations agreeing. They share one contract suite
(`tests/contract/test_record_store_contract.py`), and the contract suite is
where the invariant, the replay and the isolation are proven before any tool
exists.

### Erasure

Records are owner-authored, not derived, so they outlive their source.
Session deletion's enumerated cascade gains `record_entries`: it nulls
`source_session_id` and `source_event_id` for the session and keeps the
rows. In this slice the service sets `source_session_id` only and
`source_event_id` is always null; the cascade clears both so a later writer of
the event link needs no new erasure rule. The session-deletion repository applies it inside its own deletion
transaction, and the store's `detach_session` states the same contract for
the contract suite. `erase_principal` removes every series
and entry of the principal in one transaction. It is a store-level contract
only, mirroring the People store: no principal-erasure flow composes it in
this slice. Deletion through the tool is a hard delete in one transaction,
visible to no read afterwards; nothing re-forms from a deleted record because
nothing forms from records at all.

## Events

Four event types join Section 6.8's list, each with `payload_schema_version`
1 and appended to the run's session in the same unit of work as the write:

```text
records.series.created   series_id, name, display_name, components, sensitivity, cadence
records.entry.appended   series_id, entry_id, observed_at, observed_precision, values, note_withheld
records.entry.deleted    series_id, entry_id
records.series.deleted   series_id, entries_removed
```

Each is a `principal` actor event carrying the run's id. In
`records.series.created`, `name` is the normalized name, `components` is the
list of component names in declaration order, `sensitivity` and `cadence`
are the enum texts (`cadence` null when absent), and it is appended only
when the series is new, never for an equal re-declaration. In
`records.entry.appended`, `observed_at` is the stored UTC instant in ISO 8601
and `values` maps each component to its canonical six-place text; it is not
appended for a replay. `records.entry.deleted` is appended only when an entry
was removed, and `records.series.deleted` only when the series existed, with
the count of entries removed by its cascade.

The row is canonical and the event is provenance: `records.entry.appended`
carries the values so that a rebuild can replay them, as `memory.formed`
carries a statement; the two deletion events carry identifiers and counts
only. No event carries a note.

## Formation: `recorded_elsewhere`

A record is not a belief and never becomes one. Formation reads one more
thing: from the segment's extraction events it collects the
`records.entry.appended` events, and for each entry one frozenset of forms per
component, the canonical six-place text and the short form
(`recorded_value_sets` in `memory/recorded_values.py`). The pre-check lives
in the governed formation service's candidate loop, after the provenance,
portability and hazard checks: a candidate statement is ineligible when it
contains, as standalone numeric tokens, every component value of one entry
appended in the same segment, each component consuming its own token, so an
entry with two equal components needs the number twice. A standalone numeric
token is an optionally signed run of digits with an optional fraction that is
not preceded by a digit or a dot, and is not followed by a digit, nor by a dot
that is itself followed by a digit. "128 over 82" against a `{128, 82}` entry
is suppressed; "I ran 82 km" in the same segment is not; a one-component
weight entry suppresses only a statement carrying that weight.

Because the check sits in the governed consolidation loop's candidate path,
every candidate that loop evaluates passes through it, deterministic,
provider-extracted and distilled candidates alike, and the People data those
candidates carry, without a copy of its own.
Writes that do not pass through that loop are outside it: explicit owner
corrections, `memory.remember`, email and other source formation, explicit
People writes and reconsolidation. An explicit write stays the model's or the
owner's choice, steered by the tool descriptions. The suppression is counted with the run's rejected
candidates and named `recorded_elsewhere` in the formation diagnostic.

## The records catalog in context

The owner decided that the model should always know which series exist and
be able to say when one is due. The catalog is one platform-rendered item,
and the context engine's obligations for a new item type are met here.

**Content.** One line per series the ceiling admits, ordered by normalized
name, each starting with the series' display name:

```text
blood pressure (systolic mmHg, diastolic mmHg): 31 entries, last 2026-10-21 14:40 +00:00, due daily since 2026-10-22 14:40 +00:00
weight (kg): 1 entry, last 2026-10-19, logged ad hoc
sleep (hours): no entries, due daily since 2026-10-22 09:00 +00:00
```

Never a value. Times render in UTC, the zone the runtime metadata row renders
the current date in, until an owner timezone exists; a day-precision entry
renders its date only. A single entry renders `1 entry`. A series without a
cadence says `logged ad hoc`; one whose cadence has not elapsed says
`due weekly`, `due daily` or `due monthly` with no `since`; a series with a
cadence and no entries is due from the clock. When more than sixteen series
exist the sixteen earliest by name render and a last line says
`N more series; records.list has them`. A principal with no series renders no
bytes at all, so a session without records is byte-identical to one built
before the milestone.

**Region and class.** `ContextClass.RECORDS_CATALOG` maps to
`ContextRegion.BODY` in the region table, because the catalog changes
whenever an entry lands and may never touch the prefix; the item type carries
that class, and the builder's region assertion covers it. It enters the fixed
assembly order of [context-engine.md](context-engine.md) as row 13a, after
the in-turn recall and correction lines and before the runtime metadata.

**Trust and envelope.** The item is a `UserMessage` labelled
`trust=INTERNAL_TOOL` with `source="records.catalog"`, rendered inside the
ordinary envelope exactly as a `records.list` result would be. Its names were
approved by the owner on a card and passed both scans at creation, and
`INTERNAL_TOOL` content lowers neither the builder's request-level origin
stamp nor the executor's turn label: a fresh chat whose only added item is
the catalog keeps `USER` origin trust, and an append in it stores without a
card.

**Budget and yield.** The cap is the `records_catalog` class in
`context/plan.yaml`, `max_items: 16` and `max_tokens: 600`, an absolute cap
with no scaling by the window; `ContextBudget` gains
`records_catalog_tokens`, and composition passes `max_items` to the builder
as the series limit. A catalog over its own token cap drops whole. Under
pressure the catalog yields whole, never truncated, and first: the builder
has no knowledge-passage yield step, so `records_catalog` is the first entry
in the pressure event's yield steps and is dropped before in-turn recall
trims, because a shortened catalog misstates what exists and one tool call
recovers it.

**Reading the catalog.** The builder reads the catalog inside assembly
through an injected `RecordsCatalogSource` (`context/records_catalog.py`),
as it reads in-turn recall through its retriever, with the run's `Principal`,
the constant `restricted` ceiling and the class's series limit; the records
service is that source. Rendering is pure: the same catalog and zone produce
the same bytes, so two assemblies over the same store and clock are
byte-identical. The catalog is absent, and no store is read, when the flag is
off (no source is injected), when the run's principal lacks `records.read`,
or when the class is absent (zero caps); it is also absent when the store
holds no series, and a failed read logs `context_records_catalog_failed` and
assembles without it.

**The framing sentence.** Row 2 of the assembly order gains one fixed
sentence of platform text, cache-safe because it never varies:

> A records catalog, when present, lists the owner's numeric series with
> their last entries; it holds names and dates, never values. A series marked
> due may be mentioned once per session, never while the owner is mid-task,
> and the catalog is not a request to log.

The sentence changes the prefix bytes of every session, flag on or off, so it
rotated the builder version to `context-builder@13`. A plan from an earlier
builder fails its stored prefix-hash recheck and re-plans through the
ordinary logged epoch rotation, once.

**Stability.** The engine's fifty-turn prefix script creates a series and
appends an entry at turn 40 (`records_append_turn` in
`tests/eval_cases/28_fifty_turn_prefix.yaml`); `test_prefix_stability`
asserts that every request from turn 40 on carries the catalog in its body,
none before it does, and the session still yields exactly one
`prefix_sha256`.

**Reminders without a chat open** are not wired in this slice. A scheduled
run carries only its revision's requested scopes, and `schedule.create`
requests no records scope, so no scheduled run sees the catalog or can call
`records.query`, even though the schedule authority may hold `records.read`.
Wiring a schedule to request it is a later design note.

## Configuration and scopes

One environment flag and two shipped knobs:

```text
AGENT_RECORDS_ENABLED                         env   bool  default 0   registers the five tools and injects the catalog source
classes.records_catalog.max_items             plan  int   16          context/plan.yaml
classes.records_catalog.max_tokens            plan  int   600         context/plan.yaml
```

The two shipped knobs are in `SHIPPED_KNOB_PATHS`, which takes the ADR-0036
inventory to 207, and `tests/unit/test_config.py` pins both. The series,
entry and note bounds are module constants, not knobs (see
[Domain model](#domain-model)).

`records.read` and `records.write` join the closed scope vocabulary of
[policy-and-approvals.md](policy-and-approvals.md) and the enumeration in
[http-api-and-streaming.md](http-api-and-streaming.md). Holders in the first
slice: the owner's principal carries both through the API and worker units
(`AUTH_SCOPES` in `deploy/veetbot.env.example` and `.env.example`). The
schedule authority may hold `records.read`
(`deploy/veetbot-schedule.env.example`), never `records.write`, since a
schedule cannot know a value the owner has not just said; but scheduled
records access is not wired in this slice, because a scheduled run carries
only its revision's requested scopes and no schedule requests a records
scope. Wiring it is a later design note. The surface, notify and call env examples are
unchanged in this slice; granting the scopes to an owner-paired surface, so
that a reading typed into Telegram appends as one typed into Chat does, is an
owner env edit, and production env edits remain the owner's. A delegated
child run carries what delegation grants it from its parent; no rule
specific to records is added. A public caller or an unpaired sender gains no
owner scopes and therefore no records.

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
   the scans, the limits, the events, and the provenance detach in the
   session-deletion cascade. Gates 5 and 6.
4. **Tools.** The five tools with their schemas, descriptions and
   classification; registration under the flag; the `records` domain in the
   partition table; the two scopes in the vocabulary. Gate 8.
5. **Policy.** The `EXTERNAL_WRITE` and `EXTERNAL_DELETE` classification of
   create and delete, the service's `records.untrusted_turn` refusal, and the
   matrix tests; the default profile is unchanged. Gate 2.
6. **Query determinism and listing.** Fixture-driven byte-stability tests
   across adapters and calls. Gates 4 and 9.
7. **Formation.** The recorded value sets from the segment's append
   events; the pre-check in the governed service's candidate loop; the
   diagnostic counts `recorded_elsewhere`. Gate 7.
8. **Catalog.** `ContextClass.RECORDS_CATALOG`, the item, the renderer, the
   `records_catalog` class and budget field, the yield step, the injected
   catalog source, the framing sentence with the `context-builder@13`
   rotation, and the extended stability script. Gate 10.
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
   `EXTERNAL_UNTRUSTED` is refused with `records.untrusted_turn`, a
   non-retryable `PERMISSION` failure, and writes no entry and no event; an
   append in a turn whose origin trust is `USER`, `MEMORY` or `INTERNAL_TOOL`
   stores without a card; `records.create_series` and `records.delete`,
   classified `EXTERNAL_WRITE` and `EXTERNAL_DELETE`, evaluate to
   `REQUIRE_APPROVAL` under the deterministic matrix in any turn, so neither
   executes without a resolved approval, and an approved create executes
   once. **M34.**
   The registry check
   `tests/gates/test_records_m34.py::test_untrusted_turn_append_is_refused_and_writes_nothing`
   proves the refusal clause; the trusted-turn, approval and execute-once
   clauses are proven by
   `tests/gates/test_records_m34.py::test_memory_turn_append_is_allowed`,
   `tests/gates/test_records_m34.py::test_create_and_delete_require_approval_by_matrix`
   and `tests/gates/test_records_m34.py::test_approved_create_series_executes_once`,
   which run beside it.
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
   an entry appended in the same segment, on every extractor whose
   candidates pass the governed service's candidate loop, while a statement
   carrying only one value of a two-component entry still forms. **M34.**
8. **Registration.** Startup registration validation accepts all five tools
   with every `ToolSpec` field, `records` is a builtin domain, the two scopes
   are in the closed vocabulary, every records reason code has a static
   message, and with `AGENT_RECORDS_ENABLED` unset or `0` the tools are
   unregistered and the roster is unchanged, while `1` registers all five.
   **M34.**
9. **Listing stable.** `records.list` output and the catalog rendering for
   the same store are byte-identical across calls. **M34.**
10. **Catalog.** The item declares the body region; a principal with no
    series renders no bytes; the rendering carries names, components, counts,
    last times and due state and never a value; a series above the surface
    ceiling is absent; the extended fifty-turn script with a series created
    and entries appended mid-session yields one `prefix_sha256`; under
    synthetic pressure, or over its own token cap, the catalog drops whole,
    first, before recall trims; and a request whose only added item is the
    catalog keeps `USER` origin trust. **M34.**

Gate 10's registry check is
`tests/gates/test_records_m34.py::test_catalog_region_bytes_and_trust`; its
prefix clause is proven by `tests/gates/test_context_m7.py::test_prefix_stability`
and its yield clause by
`tests/gates/test_records_m34.py::test_catalog_yields_whole_under_pressure`,
which run beside it. Every gate's `check` in `evals/gates/records.yaml` is
its test.

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
- [policy-and-approvals.md](policy-and-approvals.md): the two scopes and a
  count reconciled with `src/agent_core/policy/scopes.py`; no condition and
  no row, because the slice changes no profile.
- [http-api-and-streaming.md](http-api-and-streaming.md): the two scopes in
  the enumeration.
- [memory-formation-and-consolidation.md](memory-formation-and-consolidation.md):
  the `recorded_elsewhere` pre-check at the filter stage.
- [context-engine.md](context-engine.md): row 13a, the cap-table line, the
  yield step, the budget field, the framing sentence, and the
  `context-builder@13` note.
- [event-log-and-persistence.md](event-log-and-persistence.md) and
  [runtime-loop.md](runtime-loop.md): the four event types, the persisted
  event-type count, and the cascade's new table.
- [bootstrap-and-composition.md](bootstrap-and-composition.md): the knob
  count and the `context/plan.yaml` census row.
- [milestone-map.md](milestone-map.md) and [readiness.md](readiness.md): the
  ten gates and the milestone's row.
- `AGENTS.md`, `mkdocs.yml`, the ADR-0036 knob inventory, the env
  examples, and the operator note for `AGENT_RECORDS_ENABLED` in
  `docs/deployment.md`.

## Decisions

- Values are `Decimal` end to end and are stored as canonical text, never as
  floats or JSON numbers, so summaries are reproducible.
- `side_effect: NONE` for the append with the two tool-side replay
  obligations, rather than a new side-effect class, because the write leaves
  the host for nowhere; create and delete take `EXTERNAL_WRITE` and
  `EXTERNAL_DELETE` so the existing matrix asks the owner for them.
- No policy profile change. The owner chose an approval for untrusted-turn
  appends through a profile row with a condition; a profile edit changes the
  shipped policy version and unbinds production's formation release
  evidence, which the owner has not accepted, so the slice refuses such an
  append in the service with `records.untrusted_turn`, and the profile row
  remains the pending alternative (ADR-0239, amended 2026-10-22).
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
