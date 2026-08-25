---
title: Memory Read API & Browser
status: design
canonical: true
---

# Memory read API and browser

This document specifies Milestone 17. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md) and it reuses rather than replaces
the http-api-and-streaming, memory-formation, memory-retrieval, and
memory-evaluation designs.
[ADR-0070](../adr/0070-milestone-17-memory-read-api-and-browser.md) records the
architectural decisions and the authorization.

The belief store is the one durable thing about a person that this platform
holds and never shows them. The `memory` area carries forty-nine registered
gates: twenty-nine say what memory must never do and Milestone 16's twenty say
how well it works, and not one of them shows anybody a belief. The only surface
that answers *what do you actually believe about me* is a terminal on the host:
`agent memory list`, `get`, `formations`, `diagnose`, and `trace`
(memory-formation-and-consolidation.md:460-470). That surface is right for an
operator and useless to the person the beliefs are about, who reaches the
platform through the native client.

The retrieval design already drew the line this milestone builds on. A recall
trace explains one answer; browsing everything the agent believes is *a
different surface* (memory-retrieval-and-ranking.md:656-657), and that surface
has never existed anywhere but the command line. Milestone 17 builds it: two
GET routes, one exact scope, one default-off flag, and a browser in the Apple
client.

Milestone 17 is authorized as a parallel workstream alongside Milestones 13
through 15 and alongside Milestone 16. Its gates may become green
independently, but the verified gate ceiling advances only in numerical order.

## Scope

Milestone 17 delivers a read surface and the client that consumes it.

- **Browse.** A keyset-paginated list of the calling principal's beliefs,
  newest first by store position, with a page of complete belief views rather
  than of recall-shaped summaries.
- **Search.** A text query over subject and statement, answered identically by
  both store adapters because both call the same lexical helpers.
- **Filter.** Status, belief type, subject, and source session, each composable
  with the others and with the text query.
- **Detail.** One belief by identifier, returning the same view a list row
  carries.

Four things are out of scope, and each is named because a reader who does not
find it here should find the reason here.

1. **Any write.** No edit, no retraction, no deletion, no confirmation of a
   flagged conflict, no re-derivation. Corrections stay on the `agent memory`
   CLI, which routes them through the governed formation service rather than
   around it. ADR-0070 decision 3 fixes this boundary, and hard gate 6 enforces
   it structurally rather than by convention.
2. **Recall-trace viewing.** There is a precondition and it is a defect:
   `PostgresTraceStore.for_turn` in
   `src/agent_core/adapters/persistence/memory_repositories.py` selects traces
   by turn identifier alone, with no tenant or principal predicate, so it is
   not safe behind a route at any ceiling. That predicate must be added, and
   the trace store's contract suite must observe it, before a trace route is
   designed. Beyond the defect, a trace view owes the minimum-of-two-ceilings
   rule (memory-retrieval-and-ranking.md:661-667), which is a second ceiling
   mechanism in a milestone whose whole ceiling story is one parameter.
3. **Consolidation and formation audit routes.** `formations` and `diagnose`
   expose model selection, watermarks, and attempt audits — operator-tier
   provenance whose exposure question is a different question from a belief's.
   They are a candidate for a later round.
4. **Knowledge documents.** Passages and their ingestion are a separate subject
   with a separate design and a separate governance story
   ([knowledge-documents.md](knowledge-documents.md)); a memory browser that
   quietly also browsed the knowledge corpus would be two surfaces sharing one
   review.

Milestone 16's residue list excludes "an HTTP memory surface"
(memory-evaluation-and-lifecycle.md:66-73). This milestone takes the read half
of that exclusion and no more. The semantic arm, the external provider, the
persona surface, the entity graph, and belief merge remain excluded exactly as
ADR-0069 left them.

## The routes

Two routes, both GET, both requiring the exact scope `memory.read`, both
mounted only when `AGENT_MEMORY_API_ENABLED` is set. They use the same
authentication middleware, principal-first application signatures, request-id
header, error envelope, and cross-principal not-found rule as every route in
[http-api-and-streaming.md](http-api-and-streaming.md); that document carries a
stub subsection pointing here, and this document owns the schemas. Both success
responses carry `Cache-Control: private, no-store`, because a belief body is
principal-scoped and sensitivity-bearing and no shared or on-disk cache may
retain it — the rule the artifact content route already applies to the other
route that returns user content.

```text
GET /v1/memories                 memory.read   Page[MemoryView]
GET /v1/memories/{memory_id}     memory.read   MemoryView
```

### `GET /v1/memories`

```text
ceiling      REQUIRED  public | internal | sensitive | restricted
limit        default 50, clamped at 200
cursor       opaque base64url keyset token
status       repeatable; MemoryStatus values
                       default: the live set, active + provisional
belief_type  repeatable; BeliefType values
subject      lowercased (exact, case-insensitive)
session_id   the source session's identifier
text         any-term search over subject and statement
```

`ceiling` has no default. A request that omits it is a validation error, not a
request served at the permissive end, because a default ceiling is precisely
the failure mode where a caller added next year is trusted by omission. The
rule is ADR-0045 decision 11's, restated for a route rather than for a snapshot
caller, and hard gate 1 is what keeps it from being softened into a default the
first time a client forgets the parameter.

The default `status` set is the live one — `active` and `provisional` — because
a browser that opened onto superseded and retired rows would be showing history
rather than belief. History is one parameter away: a caller that wants it names
the statuses it wants. `candidate` is a legal value and returns nothing in
practice, since a candidate is not a stored belief.

`subject` is lowercased on both sides of the comparison, which matches the
store's SQL `lower()` semantics exactly; `casefold()` would diverge from it on
non-ASCII subjects and break parity between the two adapters.

`text` is split into terms by `lexical_query_terms` in
`src/agent_core/domain/memory.py`, which both store adapters call, and matched
with any-term semantics: a belief matches when it overlaps one term or more.
Term derivation is therefore shared; matching is not, and cannot be. PostgreSQL
tests each term with `plainto_tsquery('simple', term)` against
`to_tsvector('simple', subject || ' ' || statement)`, and the in-memory tier
tests it with `lexical_text_matches`, whose `lexical_tokens` reproduces that
lexeme split and whose all-lexemes-of-one-term rule reproduces
`plainto_tsquery`'s conjunction. One half holds by construction and the other
is a deliberate emulation, which is exactly why the cross-adapter tests below
have to check it rather than assume it — assuming it is the mistake Milestone
16 already had to repair once. Text too short to yield a term is matched whole
rather than matching everything.

Repeated parameters intersect across kinds and union within a kind: two
`status` values mean either status, and a `status` together with a
`belief_type` means both conditions. That is the reading a query string makes
natural and the only one a client will guess right.

The response is the shared `Page` envelope, `items` and `next_cursor`, the same
shape `GET /v1/sessions` and `GET /v1/approvals` return.

### `GET /v1/memories/{memory_id}`

`ceiling` is required here too, and for the same reason. A belief that is above
the supplied ceiling, that belongs to another principal or another tenant, or
that does not exist at all is uniformly `not_found` with status 404. This
extends the existing cross-tenant-404 rule
(http-api-and-streaming.md:310-328) to the ceiling, because the alternative
distinguishes *exists but is too sensitive for you* from *does not exist*, and
that distinction is an oracle over the subject line of every restricted belief.
Transparency must not become a disclosure path
(memory-retrieval-and-ranking.md:661-667).

### Errors

The closed error-code vocabulary is unchanged
(http-api-and-streaming.md:111). These routes raise exactly four of its
members and add no code of their own.

```text
400  malformed_request     missing or unknown ceiling, unknown status or
                           belief type, malformed or undecodable cursor,
                           limit that is not a positive integer
401  authentication_error  no credential, or a credential that does not
                           resolve
403  authorization_error   a principal without memory.read
404  not_found             a belief above the ceiling, in another
                           principal's store, or absent
```

A `limit` above 200 is clamped rather than rejected, which is the pagination
rule the API already applies everywhere; only a `limit` that is not a positive
integer is refused, as `malformed_request`.

### Pagination

The four pagination rules stated in
[http-api-and-streaming.md](http-api-and-streaming.md) — keyset never offset,
opaque base64url, `limit` defaulting to 50 and capping at 200, `next_cursor`
null on the last page (http-api-and-streaming.md:1503-1520) — apply unchanged.
This surface fixes their two free parameters:

```text
sort      store_position DESC, id ASC
keyset    (store_position < p) OR (store_position = p AND id > i)
cursor    base64url over compact JSON: the last row's (store_position, id)
```

`store_position` is drawn from one store-wide sequence, so it is unique across
beliefs and increases with write order within any one principal's; `id` is
there because a total key needs a tiebreaker even for a value that should never
tie, and a keyset predicate over a key that is not total is the version of this
that quietly drops a row. The sequence is cluster-wide and its counter is not
transactional, so positions are gapped and a lower position can commit after a
higher one; the walk is therefore over committed rows and its guarantee is the
one the gate states — no belief that existed throughout the walk is skipped or
repeated, which is the property an offset paginator over a growing table cannot
give. Reading a cursor twice returns the same page unless the store was written
to in between. A cursor taken from one filter and replayed under another is not
detected, on the same reasoning the API already gives: the client that
constructs one is the client that took the cursor apart.

### Mounting

The router exists only when `AGENT_MEMORY_API_ENABLED` is set, default off,
mirroring `AGENT_SCHEDULE_API_ENABLED` and `AGENT_NOTIFICATION_API_ENABLED`.
Absent the flag the routes are not registered and do not appear in the OpenAPI
document, while `memory.read` stays in the closed scope vocabulary so that
configuration validation still recognizes a principal granted it. A read
surface over everything the platform believes is a thing an operator turns on
deliberately.

## The `MemoryView` projection

`MemoryView` is a frozen model that forbids unknown fields, derived from
`MemoryRecord` by an explicit allow-list rather than by exclusion, because an
exclusion list is the version of this that leaks the next field somebody adds
to the record.

```text
exposed   id, subject, statement, belief_type, status, polarity, scope,
          portability, authority, sensitivity, confidence,
          corroboration_count, flagged_for_review, conflicts_with,
          superseded_by, source_session_id, source_event_ids,
          formation_run_id, consolidation_policy_version, origin_scopes,
          valid_from, valid_to, expires_at, last_reinforced_at, created_at,
          updated_at

withheld  tenant_id, principal_id      the API never returns tenant identity
          utility                      retriever-internal ranking state
          store_position               cursor internals
```

These four fields are withheld outright and settled. Tenant identity is never
a field of a response anywhere in this API; `utility` is a number the ranker
moves and a number a reader would misread as a judgment about the belief; and
`store_position` is the cursor's internals, which stay inside the cursor for
the same reason the cursor is opaque.

`formation_run_id`, `consolidation_policy_version`, and `origin_scopes` were
initially withheld here as a recommendation flagged for owner sign-off,
reasoning that they are the operator-tier vocabulary the `formations` audit
surface owns and that exposing them would answer a question this milestone
deliberately deferred. The owner decided otherwise on 2026-08-23: the trio is
exposed (docs/status/questions-for-review.md, Milestone 17 section). They are
genuine provenance a user could reasonably want in a belief's detail view, and
the `formations` and `diagnose` audit routes' own exposure question — model
selection, watermarks, and attempt audits — remains open on its own terms
rather than tied to this one.

Hard gate 9 asserts the exposure list exactly in both directions: every named
field serializes, and no withheld field can appear however the view is
constructed.

## The ceiling is supplied, never inferred

The caller states the sensitivity ceiling of the surface it is rendering onto,
on every request, and the server filters strictly: a belief is returned only
when `SENSITIVITY_ORDER[sensitivity] <= SENSITIVITY_ORDER[ceiling]`. The server
never derives a ceiling from the principal's scopes, from its roles, from the
authentication mode, or from anything about the transport. ADR-0045 decision 11
made this the rule for snapshot callers and gave the reason: inferring
sensitivity from write scopes is a mapping that is wrong the first time a
surface exists that the mapping's author did not imagine.

The native Apple client declares `restricted`. The owner considered the
conservative alternative — a lower ceiling for the graphical surface — and
explicitly rejected it, accepting full-parity viewing instead, because a
browser that silently omits rows teaches its user that the platform forgot
something it did not forget, and an invisible omission is worse than no
browser. The device is the owner's, the token is device-local and held in the
keychain, and the surface is private in the same sense the Milestone 9 runtime
surface is. The accepted consequence is stated plainly in ADR-0070 decision 5:
a stolen, unlocked device with a live token can read restricted beliefs.

Declaring `restricted` is not the same as defaulting to it. The client sends
the parameter on every request like any other caller, and a future shared or
projected surface sends a lower one without a line of server code changing.

## The Apple client

The client gains a browsing surface over the two routes and holds no memory
state of its own, in the shape
[notifications-and-devices.md](notifications-and-devices.md) established for
the device surface.

- **Entry point.** A sidebar toolbar item presents the memory browser as a
  sheet on both compact and regular layouts in round one — the plan the owner
  approved for this milestone. A detail-column presentation on regular layouts
  remains a candidate future refinement, not a round-one commitment. The
  browser is a peer of the conversation list rather than a mode inside a
  conversation, because a belief outlives the session that formed it.
- **Models.** `MemoryView` is mirrored as a `Codable` value in the client's
  wire models beside `SessionView` and `Notification`, with `Page<MemoryView>`
  reusing the existing page envelope. Unknown fields decode without failing, so
  a newer server does not break an older client.
- **View model.** Search input is debounced before it becomes a request; the
  list pages by cursor as it scrolls, and it stops when `next_cursor` is null
  rather than when a page comes back short. Two guards matter and both have
  been the bug in every list of this shape: a repeated-cursor guard, so a
  server that returns the cursor it was given cannot spin the client, and a
  stale-response guard, so a slow page for an abandoned query cannot overwrite
  the results of the query the user is now typing. Changing a filter or the
  query text discards the cursor and restarts from the first page.
- **Degradation.** A server that does not mount the router returns not-found
  for `/v1/memories`, which the client feature-detects the way it already
  detects a server that needs upgrading, and presents as *this server does not
  support memory browsing yet* rather than as an error. An older server keeps
  working with the browser entry point absent.
- **Rendering.** A row shows the statement as its primary text, subject and
  belief type as secondary text, a text-labeled sensitivity badge, and a
  status tag when the belief's status is not `active`; a belief that is
  flagged for review or that carries `conflicts_with` links renders that state
  inline, because the whole point of Milestone 16 committing conflicts flagged
  rather than resolving them silently was that somebody would eventually see
  them. Authority, alongside the belief's remaining classification, provenance,
  and lifecycle fields, appears in the detail view a row opens onto.
- **Accessibility identifiers.** The sidebar entry point (`sidebar.memory`),
  the browser's root container (`memory.browser`), a row (`memory.row.<uuid>`),
  and the detail view (`memory.detail`) carry stable accessibility
  identifiers. The search field carries none: `.searchable` hoists it into the
  navigation bar chrome, which is not a descendant of any view the client
  could tag, so the user-interface fixture reaches it the way it reaches any
  search bar, through `app.searchFields`, rather than through an identifier.
  The filter controls carry accessibility labels rather than identifiers.
- **Verification.** Native tests are this surface's verification, on the hosted
  Apple package and simulator lanes. No Python gate observes Swift, per ADR-0049
  decision 9, and none of the ten gates below is satisfied by client code.

## Store and lifecycle interplay

Browse reads a store that Milestone 16's sweeps are actively moving, and the
two designs meet at three points.

A page is point-in-time. The decay sweep retires idle provisional beliefs and
the conflict path flags and links contradictions, so a belief on page one can
be retired before page three is fetched. The keyset predicate is what makes
that safe: a retired belief drops out of a later page under the default status
filter without shifting the rows around it, where an offset paginator would
have shifted every subsequent row by one.

The browser is where the lifecycle becomes visible. `flagged_for_review`,
`conflicts_with`, `superseded_by`, `valid_to`, and `last_reinforced_at` are all
in the exposure list precisely because they are the fields that say what the
lifecycle did, and until now nothing outside the CLI could read them.

Lexical parity between the two adapters rests on one shared half and one
emulated half: both call `lexical_query_terms` to derive the query's terms, and
each then matches with its own engine — `plainto_tsquery('simple', …)` against
a `to_tsvector('simple', …)` in PostgreSQL, `lexical_text_matches` emulating
that lexeme split and its conjunction in memory. Only the first half holds by
construction, so the second is asserted rather than assumed. The same is true
of the subject predicate, where SQL `lower()` and Python `lower()` are two
implementations that have to be shown to agree.

The requirement is that both stores browse identically. The mechanism that
asserts it is the one the store contract suite already uses for recall, and it
is two suites rather than one parametrized run. The shared browse contract
suite in `tests/contract/test_memory_store_contract.py` covers order, the
keyset boundary including its identifier tiebreak, every filter, and the text
query against the in-memory adapter.
`tests/integration/test_memory_postgres_m9.py` runs the same
`MemoryBrowseQuery` values against a live PostgreSQL store and compares its
answer to the in-memory adapter's over the identical corpus, adding the keyset
walk and the principal-isolation and status-override predicates. Neither suite
is optional: the first fixes the behavior, the second proves the PostgreSQL
adapter answers alike. Hard gate 8 asserts that mechanism is in place.

## Build sequence

1. `MemoryBrowseQuery`, the cursor codec, and the `browse` port method on
   `MemoryStore`, with the shared contract suite written first and both
   adapters failing it. **M17.**
2. Both adapters implement `browse`: the in-memory tier and the PostgreSQL
   keyset query, sharing the lexical helpers and the sort. **M17.**
3. `PublicMemoryService`, principal-first, applying the ceiling filter and the
   not-found rule above the store. **M17.**
4. The two routes, the `memory.read` scope, the `AGENT_MEMORY_API_ENABLED`
   flag, and the OpenAPI assertion. **M17.**
5. The documentation and vocabulary flip that ships with the code: the scope
   enumeration in the API design, the scope-count prose in the policy design,
   and the executable vocabulary in one change. **M17.**
6. The Apple client's models and API-client methods, with their unit tests.
   **M17.**
7. The Apple browser view model and views, with the debounce, both guards, the
   degradation path, and the user-interface fixture. **M17.**
8. Full lanes: the Python suite, the PostgreSQL lanes, the Apple package and
   simulator lanes, hosted CI, and the review loop on one final head. **M17.**

Step 5 is called out because it is the step that is easy to defer and must not
be: the scope vocabulary is executable, so its documentation and its
enumeration ship together or the corpus disagrees with the code.

## Hard gates

1. **A request without a ceiling is refused.** A list or detail request that
   omits `ceiling` is a validation error naming the parameter; the server
   returns no belief and applies no default. A ceiling outside the enumeration
   is the same error. Registered as
   `gate.memory.read_api_ceiling_required`, case. **M17.**
2. **Nothing above the ceiling is returned or distinguishable.** Over generated
   belief sensitivities and request ceilings, no item of any list page has a
   sensitivity above the requested ceiling, and a detail read of a belief above
   it is byte-identical to a detail read of an identifier that does not exist.
   Registered as `gate.memory.read_api_ceiling_filter`, property. **M17.**
3. **A principal sees only its own beliefs.** A list request returns nothing
   belonging to another principal or another tenant, and a detail request for
   such a belief is `not_found` with status 404 rather than
   `authorization_error`. Registered as
   `gate.memory.read_api_principal_isolation`, case. **M17.**
4. **Keyset paging neither skips nor repeats.** Walking every page while
   beliefs are written and retired between requests yields each live belief at
   most once and every belief that existed throughout exactly once; a malformed
   cursor is a validation error; `next_cursor` is null on the last page and only
   there; and re-reading a cursor against an unchanged store returns an
   identical page. Registered as `gate.memory.read_api_pagination`, case.
   **M17.**
5. **Every filter selects the documented set.** Status, belief type, subject,
   source session, and text each select exactly the set the design declares and
   compose with one another; the default status set is the live one; and the
   text filter's results equal those of the shared lexical helpers applied
   directly. Registered as `gate.memory.read_api_filters`, case. **M17.**
6. **The router is read-only.** Every route mounted under `/v1/memories`
   declares the GET method and exactly the scope `memory.read`; a route with
   any other method or any other scope fails the build. Registered as
   `gate.memory.read_api_read_only`, structural. **M17.**
7. **The flag is a real switch.** With `AGENT_MEMORY_API_ENABLED` unset, no
   `/v1/memories` route is registered and none appears in the OpenAPI document,
   while `memory.read` remains a recognized scope for configuration validation.
   Registered as `gate.memory.read_api_flag_absent`, case. **M17.**
8. **Both stores browse identically.** The shared browse contract suite covers
   order, the keyset boundary, every filter, and the text query against the
   in-memory adapter, and the PostgreSQL parity suite answers the same
   `MemoryBrowseQuery` values from a live store and compares the two adapters
   over one corpus; both adapters derive their query terms through the same
   shared helper and lowercase their subject comparison alike. Registered as
   `gate.memory.browse_contract_parity`, structural. **M17.**
9. **The projection is exactly the exposure list.** A serialized `MemoryView`
   carries every field the exposure list names and no other key, and no
   construction path — list, detail, or error envelope — can emit a withheld
   field. Registered as `gate.memory.read_api_view_projection`, structural.
   **M17.**
10. **Every error is a member of the closed vocabulary.** Across missing and
    malformed parameters, absent and insufficient credentials, unknown
    identifiers, and above-ceiling reads, every response either succeeds or
    carries one of `malformed_request`, `authentication_error`,
    `authorization_error`, or `not_found` with its documented status.
    Registered as `gate.memory.read_api_error_vocabulary`, case. **M17.**

## Tracked metrics

Track:

- list and detail request counts, by outcome and by declared ceiling;
- page sizes served and pages walked per list session;
- beliefs filtered out by the ceiling, as a count and never as identifiers;
- text-query rate and the share of queries that yield no term;
- cursor rejections, by malformed and by out-of-range;
- client-side degradation events, where a server without the flag was met.

Metrics carry no belief statement, no subject, and no identifier.

## Open questions

Whether `formation_run_id`, `consolidation_policy_version`, and
`origin_scopes` should be exposed was the one item in this section the
implementing change could not land without an answer to. It is answered: the
owner decided on 2026-08-23 to expose the trio rather than withhold it
(docs/status/questions-for-review.md, Milestone 17 section). The three
questions below remain open.

1. Whether a later round should add ordering by `last_reinforced_at` or by
   confidence. Store position is the only order the keyset predicate is proven
   for, and a second order means a second cursor shape.
2. Whether the detail route should carry the belief's supersession chain rather
   than one `superseded_by` identifier. A chain is what a reader usually wants
   and is also an unbounded walk over records the ceiling has to filter twice.
3. When the trace-store predicate is fixed, whether trace viewing arrives as a
   third route here or as its own milestone with the consolidation audits.
