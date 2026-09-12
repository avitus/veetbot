---
title: Client Modes and Email Experience
status: design
canonical: true
---

# Client modes and the email experience

The owner approved this design and authorized implementation as **Milestone 26**
on 2026-09-11, including USD 20 per UTC day and USD 200 per rolling thirty days
for aggregate automatic email work. This canonical mechanism expands the
engineering plan's Milestone 26 under [ADR-0092](../adr/0092-client-modes-and-email-experience.md).
It is an independent parallel workstream; the verified ceiling remains Milestone
12. Gate registration alone is not implementation evidence.

## 1. Product decision

Email mode is a focused interface to the same Veetbot. Chat and Email share the
owner's identity, agent, persona, memories, learned preferences, policy, and
execution infrastructure. A mode selects the interface and relevant task
context; it does not select a separate assistant or memory namespace.

The owner's confirmed requirements are:

| Area | Requirement |
| --- | --- |
| Clients | Launch together on iPhone, iPad, and Mac. No terminal-client feature work. |
| Accounts | Use the existing personal and work Gmail integrations. |
| Retrieval | Start when Email mode opens and refresh automatically while it is active. |
| Attention | Keep the priority list short and optimize for a very high signal-to-noise ratio. |
| Drafts | Prepare a response when relevant; edit and send inside Veetbot; explicitly approve every send. |
| Learning | Use historical received and Sent email as extensively as practical, with no fixed age cutoff. |
| Memory | Share useful learning and memories across both accounts and Chat. |
| Important relationships | Regular reply partners, close collaborators, portfolio founders/CEOs, founders/CEOs of prospective investments, fellow portfolio board members, and venture investors. |

The approved defaults below govern implementation. They are not claims that
existing behavior already satisfies this design; changing a requirement needs
explicit owner approval rather than an implementation-driven relaxation.

## 2. The experience

### Mode navigation

Add a persistent Chat / Email selector. On iPhone, Email uses a list-to-thread
navigation flow. Mac and regular-width iPad use a list and detail layout, with
the draft visible alongside or below its source conversation. Preserve the
existing iOS 15 and macOS 12 minimum versions and adaptive layouts.

Switching modes preserves the active Chat run, unsent chat text, selected mail,
scroll position, and unsent email edits. Memory, Persona, Schedules, and Settings
remain global. Adding a future mode requires a small mode descriptor and a
presentation module; this feature does not build a dynamic plugin system.

### Priority inbox

The initial view combines both accounts and normally displays **up to five
threads**. It displays fewer when fewer meet the relevance threshold. Account
badges and an account filter make the source clear without dividing the
assistant's knowledge.

Each row shows the correspondent, subject, a concise statement of what needs
attention, and a state such as Needs reply, Draft ready, or For your attention.
An expandable explanation gives the strongest supported reasons for inclusion.
Numeric ranking scores and internal model details stay out of the main flow.

The list is a selection, not a quota. There is no obligation to fill five slots.
If more threads clear the threshold, a More important action expands the view
to ten, then provides pagination over the remaining qualifying threads. Find
mail and Review other mail make false negatives discoverable without cluttering
the default list. New qualifying mail is announced in place rather than moving
the selected row beneath the owner.

Viewing a thread does not automatically mark it read in Gmail. Dismiss from
priority affects Veetbot's attention state for that thread revision; it neither
archives mail nor silently teaches that the person is unimportant. New material
in a dismissed thread is assessed again. After a confirmed reply, the thread
leaves Needs reply unless another unanswered request remains.

Freshness is shown per account. A failed or unfinished scan says so. The UI
must not equate an incomplete scan or model failure with an empty priority inbox.

### Thread, feedback, and reply

Thread detail contains the original messages, a grounded summary, any request
or deadline, the importance explanation, and an editable draft when one is
appropriate. Render sanitized text initially; external images, tracking pixels,
and active HTML do not load. Show attachment metadata and the fact that an
attachment was not read. A draft that depends on an unread attachment requests
the missing information rather than claiming to have reviewed it.

Feedback offers Important and Less important, with an optional target: this
thread, this person, or this kind of content. Needs reply / No reply needed is a
separate judgment. Natural-language feedback such as “Her board updates matter,
but not the newsletter” reaches the same learning service from Email or Chat.
Show the interpreted change, its scope, and Undo. Ask a focused question only
when a natural-language instruction cannot be assigned a safe, clear scope.

Discuss in Chat opens the ordinary session associated with that thread. It
carries the selected thread and draft references into the conversation, uses
the same memories, and returns to the same Email state. Merely browsing Email
does not replace the owner's previously selected Chat conversation.

Existing content-free approval notifications and pending-action recovery use
the server-owned thread/session association to open the correct Email draft,
including after a cold launch. Resolving an approval on another device updates
both modes without discarding unsaved input. No mail content is added to push.

## 3. Retrieval and historical learning

### Active use, not a mailbox monitor

Refresh immediately on entry to Email and on foreground return while Email is
selected; refresh every **60 seconds** while visible, with manual refresh too.
Clients stop requesting refreshes when Email is hidden or backgrounded. The
server coalesces overlapping requests from multiple devices, with one active
refresh per principal/account and a single historical-learning slice per
principal. Repeated requests cannot build an unbounded backlog.

An empty inbox does not restart its initial loading indicator on each projection
poll. A failed refresh remains visible across successful reads of cached
projections until a later refresh succeeds; it is not reported as an empty
successful priority result.

Each foreground request may admit a bounded slice. The server does not schedule
the next slice itself: it requires a fresh active client request. On leaving
Email, stop admitting new slices; an admitted slice may finish its bounded work
and persist its checkpoint. Reopening resumes. This adds no recurring schedule,
Gmail push subscription, notification digest, or general device-presence router.
An explicitly requested email operation in Chat remains available through the
ordinary agent independently of the Email refresh timer.

### Reliable synchronization

Extend the first-party read MCP contract with the metadata and change retrieval
needed for durable synchronization. Existing search remains bounded to 25
results per page. Preserve provider message identity, thread identity, history
cursor, labels, sent/received direction, dates, and reply headers in a versioned
normalized contract. The client never calls Gmail directly.

`sync_changes.max_results` bounds normalized changes, not Google's history
records, which can each contain many changes. Overflow uses an opaque local
cursor bound to account, starting history ID, original provider page and page
size, flattened offset, and page digest. A continuation re-fetches that bounded
provider page; drift or expiry requests resynchronization without advancing the
cursor or silently dropping changes. Ordinary provider continuations remain
unchanged when no local overflow remains. Local wrappers are bounded to 64 KiB
and contain no mail content; provider tokens remain bounded to 4,096 characters.

The backend owns independent account cursors. Persist a cursor only after all
corresponding projection updates commit; process duplicate or reordered change
observations idempotently. A bounded lookahead coalesces each message's final
change across history pages before reading live projections, so a later
deletion can cancel an earlier addition even when that message is no longer
retrievable. Retain at most 1,000 message change records; exceeding that ceiling
starts the existing bounded inbox resynchronization while preserving cached
mail and independent historical learning. Handle new mail, replies sent elsewhere, label
changes, removed messages, throttling, revoked credentials, and partial account
failure. An expired history cursor triggers a bounded resynchronization. Gmail
explicitly requires a full resync when its retained history no longer covers
the client's cursor. [Google synchronization contract](https://developers.google.com/workspace/gmail/api/guides/sync)

Verified mailbox identity is distinct from a manifest default or capability
name. A changed default/capability binding creates a fresh operational session
for new work while retaining the immutable binding on old history. It must not
reinterpret existing source receipts or send through another Google identity.

Refresh the current inbox first, including older messages still in the inbox.
The first display may be partial while pagination completes; report that state
and keep useful results visible. Invalidate assessments when the source or a
relevant feedback/profile/model revision changes. Rerank affected existing mail
immediately after learning changes, even if Gmail has not changed. A poll with
unchanged sources and already-current assessment versions does no new model
work; unfinished historical analysis can still resume. Style changes affect
new or explicitly regenerated drafts, never overwrite edited drafts. Extractor
upgrades use governed replay with deduplication, not recurring re-extraction.

### Historical reach

Process both Inbox/archive and Sent history: recent 90 days first, then the
preceding year, then progressively older windows to the accessible mailbox's
beginning. These are processing priorities, not age exclusions. Spam and Trash
are excluded from automatic learning initially. Include a clear coverage view
showing processed date ranges, discovered/processed counts, exclusions, and
whether further history remains.

Use cheap structured metadata first to discover interaction patterns and select
useful source messages. Read relevant current threads and diverse historical
Sent examples next. Maintain forward progress through older history so a busy
inbox cannot permanently starve it. Metadata-only coverage is not reported as
semantic understanding of all mail. Within available budgets, continue body
analysis over remaining eligible history rather than silently ending after a
small sample.

Approved admission bounds are 100 thread summaries per account per slice,
10 full-thread reads per account per slice, and a 120-second execution deadline.
These are upper bounds: inherited tool, output-byte, context, token, and cost
limits may stop work sooner. Checkpoint between pages. Oversized threads need
explicit continuation or a partial-content marker; truncation must never be
mistaken for complete conversation evidence.

Large threads use a rolling retained window. At 100 messages or 512 KiB, finish
bounded analysis of the retained passages before advancing to the next source
window. A window may include one additional bounded MCP page, but does not grow
without bound. Completed passages can leave the cache while original message,
body-offset and source-event receipts survive. Analysis walks 8,192-character
passages; a rolled window remains explicitly partial and cannot authorize a
draft that requires complete thread context. These thresholds bound one working
window, not lifetime historical coverage.

Interactive Chat and reading/editing email take priority over historical work.
Every run retains its existing finite limits. Add explicit aggregate automatic
email admission, using the existing reservation/accounting pattern rather than
assuming scheduled-run cost ceilings already cover foreground work. Approved
defaults are **USD 20 per UTC day and USD 200 per rolling thirty days**, across
both accounts/devices and all automatic classification, history, style,
drafting, and formation work. Reserve each slice's maximum cost transactionally
before admission, settle actual usage afterward, and retain unresolved
reservations through crash recovery. Admission queries select unsettled tasks
and, for budget calculation, settled tasks created within the rolling thirty-day
window before pagination. Older settled task records remain available for audit
and idempotent command replay. Dependent extraction calls cannot escape
the originating reservation. A finite reservation is not renewed by retry.

Meter every model stage through the existing gateway. Lower applicable runtime
or principal limits prevail. Show a pause reason when a ceiling is reached;
cached browsing, editing, and feedback remain available. Measure mailbox volume,
throughput and cost before changing these approved limits or any existing spend
ceiling. The numbers are reviewable configuration defaults, not permission to
spend outside the approved implementation and evaluation scope. “Maximum learning” means broad, resumable evidence use,
not an unbounded parallel import or automatic budget increase.

## 4. Importance learning

### Evidence and relationships

Maintain a shared, versioned owner preference model. Distinguish relationship
importance, content relevance, urgency, and whether a reply is needed. Evidence
records include the source account/message, time, source type, confidence,
profile revision, and any owner correction.

| Priority signal | Supporting evidence | Limitation |
| --- | --- | --- |
| Regular reply partner | Distinct substantive threads the owner replies to, reply rate, reciprocity, recency, sustained history | Count messages once; exclude automated replies and quoted copies. |
| Close collaborator | Repeated substantive project work, shared context, explicit feedback | High send volume alone is insufficient. |
| Portfolio founder/CEO | Owner statements, supported shared memories, company and role evidence from correspondence | A title or domain alone does not establish portfolio membership. |
| Prospective-investment founder/CEO | Supported active investment discussions and owner engagement | A cold pitch does not establish likely investment. |
| Fellow portfolio board member | Evidence of the shared board and company relationship | An unrelated board title is insufficient. |
| Venture investor | Attributed role/firm evidence and relevant interactions | Role alone does not make every newsletter important. |
| Important content | Active projects/deals, material changes, decisions, commitments, direct requests, owner feedback | A sender's “urgent” claim is evidence to examine, not an instruction. |

Use existing shared memory and historical mail to discover these relationships.
Allow corrections in either mode. Associate addresses with the same person only
on reliable alias evidence or owner confirmation; matching display names are
insufficient. A small correspondent/organization evidence index supports this
feature without introducing the deferred general-purpose temporal entity graph.

### Selection and adaptation

A bounded semantic assessment extracts supported topics, relationship evidence,
actionability, deadlines, and reply need from changed mail. A deterministic,
versioned selection layer combines those features with owner rules and learned
preferences, applies the calibrated threshold, and orders qualifying threads.
Record the factors and input/profile revisions so decisions can be explained
and reproduced. Version model prompts and feature schemas too.

Explicit owner preferences dominate inferred patterns. A thread-only judgment
immediately changes that thread and contributes limited evidence to broader
preferences; it does not become an unconditional sender rule. Person/topic
feedback deliberately changes that target. Contradictory feedback, undo, and
reset rebuild the derived profile from its surviving evidence rather than
applying an irreversible hidden adjustment.

Important people do not make every message important. Infrequent board contacts
remain eligible through relationship evidence. New relevant senders can qualify
through content and shared project/deal context even with no reply history.
Conversely, bulk messages from known investors can stay below the threshold.

Do not train on the system's own rankings or summaries. Opening, dismissing,
ignoring, or archiving mail is not by itself a negative preference label.
Historical reply behavior is a useful inferred signal; explicit feedback has
higher authority. Sending a Veetbot-generated response must not feed back as
independent proof that Veetbot selected the right thread.

This is application personalization from evidence and feedback. No model
fine-tuning, reinforcement learning, new embedding infrastructure, or changes
to the deferred general memory-policy learning workstream are included.

## 5. Drafting in the owner's style

### Shared style with contextual variation

Build one versioned owner writing profile, shared with Chat, with supported
variation for recipient relationship and purpose: for example, an investment
introduction, a board decision, a close colleague, or a personal reply. Account
is useful context, not a separate persona. Learn brevity, greetings, sign-offs,
formality, paragraph structure, directness, and characteristic phrasing.

Historical Sent messages supply examples after removing quoted correspondence,
forwarded chains, signatures, templates, automated notices, and boilerplate.
Sample across recipients and time so one campaign cannot dominate. Historical
Sent mail is communication evidence, not proof of personal authorship or trusted
configuration. Mark ambiguous/delegated authorship and reduce its influence.

Use owner edits and explicit “write like this” feedback as stronger evidence.
Separate factual changes from stylistic changes. Record generated-draft lineage;
an unchanged Veetbot draft that is later sent does not become an independent
style example. A deliberate “use this as an example” action can endorse it.
The system must not gradually learn to imitate its own unreviewed output.

Persist a normalized digest in the thread-scoped `draft_lineage` record so
ordinary MIME whitespace changes and sent-body expiry cannot turn Veetbot's own
output into an independent historical example. The digest remains until source
exclusion. Owner-authored edit deltas and explicitly endorsed examples outrank
inferred historical examples in the bounded shared profile. The explicit
`POST /v1/email/drafts/{id}/style-example` command requires the current revision
and account authority, retains an excerpt of at most 2,000 characters, marks
authorship as owner-endorsed but not independent, and preserves a learning pause.

Style observations remain data supplied as bounded task context. They never
auto-edit the trusted global persona. Existing persona promotion continues to
require owner affirmation.

### When a draft is prepared

Automatically prepare drafts for up to the **three highest-priority reply-worthy
threads** after the fresh assessment. Also allow Draft reply on any selected
thread. Determine whether a reply is still needed using the complete available
thread, including replies made in Gmail or another client. Abstain when the
request is unclear, an attachment is essential, or a consequential decision is
missing. A draft can request the missing decision; it must not invent one.

Use relevant shared memory, thread facts, the learned style profile, and a small
set of appropriate examples. Broad memory sharing permits relevant context; it
does not authorize inserting unrelated private facts into outgoing email.
Never invent availability, investment intent, promises, amounts, or completed
actions merely to make a draft sound natural.

Keep drafts inside Veetbot's server-owned store. Autosave there does not change
Gmail and does not require a Gmail-write approval. Gmail draft synchronization
is not required for this release. The existing Gmail draft tool remains
available through its ordinary approval path when explicitly requested.

### Editing and sending

Each draft is bound to a mailbox identity, thread, reply target, source revision,
style/profile revision, and immutable draft revision. One current proposal is
active per reply target. Repeated refreshes do not create duplicate drafts or
replace owner edits. Refinement and regeneration create a new revision while
preserving the owner's previous version.

Autosave uses optimistic concurrency. A second device can continue from the
latest saved revision; competing edits produce an explicit conflict preserving
both versions. Unsaved local edits remain visible on a network error. New
incoming mail marks the draft stale and requires review of the changed source
before sending.

The send flow is:

1. Review & Send freezes the exact current revision and presents From account,
   To/Cc/Bcc where present, subject, and full body.
2. Final Send explicitly resolves the ordinary approval for that exact action.
3. Perform a fresh governed Gmail thread read, then recheck account authority,
   policy, draft/source revision, and action content. A failed freshness read
   defers sending and preserves the draft. An observed new message, external
   reply, edit, or recipient/account change invalidates the previous approval.
   Atomically claim the frozen local action/draft revision for dispatch so a
   concurrent local edit cannot slip between validation and the send claim.
4. Dispatch through the existing account-specific send MCP tool. Persist the
   confirmed result and update the thread and draft state across devices.

Sending by value preserves the existing approval guarantee; an approval cannot
point to a remotely mutable Gmail draft id. Cross-device double taps reuse one
logical action identity. A failure proven before dispatch can retry through the
governed lifecycle. Once dispatch may have occurred, display Send status unknown,
retain the record, and require reconciliation; do not automatically resend or
claim exactly-once delivery from Gmail.

The freshness guarantee covers source changes observed before dispatch. Gmail
does not provide an atomic check-thread-and-send transaction; another party can
send after the final read. Document this residual race and never imply that a
local revision lock prevents concurrent changes in Gmail.

Correct reply handling is part of this work. Preserve Reply-To, Message-ID,
References, and In-Reply-To; calculate reply/reply-all recipients, remove the
owner's verified addresses, and visibly confirm the outbound account. Maintain
the matching subject and thread id. Gmail requires reply headers as well as the
thread id for proper threading. [Google threading requirements](https://developers.google.com/workspace/gmail/api/guides/threads)
Initially support only verified account identities/aliases. Do not infer a
sender alias or switch the sending account from message body text.

## 6. Memories from email interactions

The existing communication adapter creates bounded, attributed thread excerpts.
The desired feature additionally needs semantic memories: relationships,
portfolio/deal context, projects, decisions, commitments, changed deadlines, and
useful preferences. This is an explicit, evaluated extension to
[ADR-0090](../adr/0090-attributed-communication-memory-formation.md), not a claim
that excerpt storage already provides it.

Add a separately versioned communication extractor that admits validated
first-party email evidence through the existing formation, conflict, lifecycle,
and retrieval machinery. Require exact message/source-span grounding and keep
author, account, thread, message identity, and event provenance. Distinguish
“Alex says the vote moved to Friday” from an owner-confirmed fact or completed
commitment. Store separate supported facts when a thread contains several;
avoid a raw email dump disguised as one memory.

Correspondent and historical Sent evidence remain attributed, inferred,
sensitivity-governed, and unable to supersede an owner assertion. Current
tentative communication facts retain the existing thirty-day evidence horizon.
Historical import measures validity from the original evidence date, not from
import time. Preserve dated historical records for deliberate historical/as-of
recall through the existing memory model: an old board appointment can answer
who served then without becoming a claim that the person serves now. Extend
the import/source contract and tests to preserve historical validity even when
the current-state belief is already expired. Historical records persist until
their source or owning records are erased; they do not occupy the current-fact
snapshot. No new temporal entity graph is required.

Distinguish profile lifetimes too. Owner-authored standing rules persist until
corrected or removed. Historical interaction aggregates remain dated history;
their recency contribution decays with a 180-day half-life. An inferred
role/affiliation older than 180 days, or active investment discussion older than
90 days, becomes historical-only evidence unless refreshed by independent
evidence. Older evidence can suggest relevance but cannot label a role or deal
current. Historical style examples remain eligible with context/date weighting.
Owner confirmation uses the existing governed promotion/corroboration path.
Re-reading and self-citation never renew evidence. Chat corrections take
precedence through backfill, retry, and policy upgrades.

Sharing also requires an explicit portability decision. Preserve the current
adapter's `LOCAL` behavior. The new evaluated semantic email policy may emit
`CONTEXTUAL` records within the same owner so relevant evidence can participate
in project-scoped Chat as well as Email; it may not emit universally portable
instructions or change tenant/principal/sensitivity boundaries. This deliberately
widens ADR-0090's relevance restriction for the new policy and must be gated on
cross-project relevance and attribution evidence. Relevance determines what
enters a draft. Account tags describe origin and sending authority, not separate
minds.

Memory inspection links back to the source thread when still available. Existing
memory browsing and Chat correction remain the initial owner surfaces. Email
learning controls can remove sender/topic/style evidence and selected imported
sources through a narrow governed email-source lifecycle; they do not add
arbitrary writes under the existing GET-only memory API. A source exclusion or
deletion must also remove its derived influence and prevent automatic recreation
from that same source. Independently supported owner memories remain intact.

## 7. Architecture and persisted state

### Reuse and new components

| Component | Plan |
| --- | --- |
| Apple shell | Small app coordinator and stable Chat/Email descriptors; independent presentation state and shared connection/authentication. |
| Email application service | New typed inbox, refresh, feedback, learning, and draft operations in the modular monolith. |
| Mail access | Existing isolated account-specific Gmail MCP servers; add bounded normalized read/change/reply metadata contracts. |
| Execution | Existing durable queue, run/session/event records, tool policy, credential broker, gateway, budgets, and approvals. |
| Personalization | Shared owner feedback ledger, relationship/content projections, and versioned style profile. |
| Memory | Existing service with a separately versioned and evaluated semantic email source/extractor extension. |
| Client APIs | Typed projections and commands; clients never parse chat transcripts into mailbox state or invoke MCP directly. |

Keep all Gmail network/OAuth code in `gmail_mcp`; preserve two-way import
isolation with `agent_core`. Add provider-neutral application values and an
MCP-backed capability adapter where necessary. This widens ADR-0071's original
tool-only application scope on the record; it does not add a second mail
provider or duplicate the Gmail credential stack.

Synchronization is deterministic orchestration, not a model deciding which
pages exist. Finite application-generated tasks use the existing worker and
normal tool invocation/policy pipeline. Document their typed task intent and
audit ownership before implementation. Do not execute Gmail directly from API
handlers or invent principal-authored prompts to smuggle imported mail into
trusted memory. Mail tool results retain their existing untrusted labels.

Use ordinary operational sessions for bounded ingestion tasks, with server-owned
metadata distinguishing them from user conversations. The default conversation
index must not fill with polling sessions. User interaction or draft generation
for a thread lazily creates/reuses its ordinary conversation session; Discuss
in Chat selects it. This introduces task associations and an index filter, not
a new session trust class or separate agent loop.

Release operational refresh MCP transports in the admitting process before
dispatch, then release the worker's transports when the run becomes terminal,
including failures awaiting cost reconciliation. Discard the matching ephemeral
catalog cache so worker execution/recovery performs discovery normally. Durable
sessions, pinned catalogs, events, and source receipts retain their existing
lifecycle; draft and Chat session transports are unaffected.

Assessment and draft response schemas require all declared properties, including
nested semantic facts, for strict structured output. Nullable values and empty
lists express absence explicitly. Domain defaults and local evidence validation
remain unchanged, and token estimation uses the actual transmitted schema.

The conversation index excludes the server-owned operational marker within each
repository query before cursor limits apply. Feedback evidence queries are
scoped to the current run before fetching events, retaining the same principal
and owner-authorship checks.

### State ownership

| Record | Required content and invariants |
| --- | --- |
| Account reference | Stable principal-scoped mailbox identity, display label, verified provider identity, capability binding, connection health. Never a credential. |
| Sync/import state | Per-account cursors, processed coverage, version, checkpoints, bounded active-job identity, failure/freshness. |
| Thread/message projection | Account-qualified provider ids, normalized headers, source revision, labels, direction, dates, bounded content, completeness, source event links. |
| Assessment | Importance factors, reply need, concise evidence-grounded reason, source/profile/model versions, confidence, attention state. |
| Feedback ledger | Authenticated author, target/scope, judgment, explanation, idempotency key, correction/undo relationship. |
| Personalization | Rebuildable correspondent/topic evidence and shared style profile with source lineage and revisions. |
| Draft | Account/thread/reply target, immutable revisions, owner edits, provenance, send/approval identity, terminal or uncertain outcome. |
| Source lifecycle | Exclusions, erasure/suppression markers, supporting-memory links, retention state. |

Apply tenant/principal predicates, forced PostgreSQL RLS where used by existing
repositories, and account-qualified identities throughout. Identical thread ids
in different accounts must not collide. Changing the manifest default account
must not reinterpret historical default-tool evidence. Resolve and persist the
actual stable account at event ingestion. Reauthorization to a different Google
identity creates a new mailbox identity rather than attaching old learned state.

Gmail is authoritative for external messages and send results. Veetbot is
authoritative for its own attention state, feedback, draft revisions, profiles,
and memory. Device-local state is presentation and unsaved input, not a second
mailbox or memory source of truth. Initial delivery requires online access for
authoritative changes; durable offline mailbox caching/edit queues are deferred.

### HTTP contract

Finalize schemas and exposure lists before the affected implementation. The
authorized scope includes these route families, with exact names subject only to
normal contract naming review:

| Route family | Operation |
| --- | --- |
| `/v1/email/accounts` | Read configured account availability, verified display identity, freshness and learning coverage. |
| `/v1/email/threads` and thread detail | Cursor-paginated priority/all-mail views, account filters, safe search, normalized thread and assessment. |
| `/v1/email/refresh` and operation status | Coalesced foreground refresh/history admission with stable idempotency. |
| `/v1/email/feedback` | Create, inspect, correct, undo scoped owner feedback and attention judgments. |
| `/v1/email/learning` | Inspect profile/coverage, pause/resume learning, reset derived preferences or exclude a source. |
| `/v1/email/threads/{id}/drafts` and draft detail | Start/refine a governed generation run; read/save revisions; discard internal proposals. |
| `/v1/email/drafts/{id}/send-proposal` | Freeze an exact outbound action and return the existing run/approval references. |
| Existing approval routes | Resolve every send once; no alternate direct-send HTTP route. |

The exact platform scopes are `email.read` and `email.write` for application
state, plus the existing run/approval scopes when those operations are invoked.
These never replace account-specific MCP use scopes. A client request cannot
mint account authority; the service checks current principal/account scope at
admission and before tool dispatch. Source erasure/reset is an explicit
authenticated owner command, never a learned preference or autonomous action.
Draft-generation and send-proposal commands also require `email.read` because
their responses contain private thread or draft content; check that authority
before admitting the durable task. Learning pause/reset return their command
result under `email.write` without requiring a subsequent read permission.

Use optimistic revisions for writes and idempotency for commands. Document
validation, authorization, unavailable/partial state, conflicts, cancellation,
and uncertain results using the existing error vocabulary where possible. SSE
continues to carry ordinary run progress; email projections refresh by version
after completion. No custom streaming transport is required.

## 8. Privacy, retention, and controls

The owner's approval authorizes shared learning as a product requirement. Before activation, update the existing
data-use disclosure to describe historical analysis, derived preferences,
semantic memory, and the hosted-model processing that already serves Veetbot.
Keep personalization distinct from generalized model training.

Never place raw mail, addresses, subject lines, draft bodies, or learned personal
details in operational logs, metrics, notifications, or checked-in evaluation
fixtures. Use private owner-reviewed evaluation data only through the governed
local/live evaluation workflow and existing export controls. Shared memory
remains scoped to this owner, not other principals or tenants.

Normalized API, tool and run failures log only their error class and opaque
request/run/tool identity. Omit exception text and tracebacks at these
boundaries because validation and provider errors can embed private mail,
model input, or credentials. Every Email HTTP response, including an unhandled
server failure, carries `Cache-Control: private, no-store`.

Use a 30-day last-access cache window for full message bodies; refetch older
mail as needed. Retain minimal message ids/cursors and aggregate lineage needed
for safe resumption while the account is enabled. Keep a bounded representative
set of up to 500 style excerpts total, each at most 2,000 characters, replacing
redundant examples as history expands. These limits bound retained examples,
not the historical evidence eligible for analysis. Retain unsent owner drafts
until discarded; remove sent/discarded draft body revisions after 30 days.

Structured feedback and derived profiles remain until reset or erased, with
the freshness rules above and obsolete observations retired on correction.
**Selected mail and draft content in ordinary tool events, checkpoints, and
session/run history retain the existing until-session/source-deletion policy.**
This can outlast the thirty-day cache and draft-store windows, including for
operational ingestion sessions. Encrypted backups retain deleted copies for at
most the existing 35 days. This is an explicit retention choice, not a promise
that all email copies expire after thirty days. The source-lifecycle action must
remove matching retained source content and derived influence across these
stores, including duplicate observations, while retaining independently
supported owner assertions. Canonical mechanics enumerate those copies before
activation. Gmail remains the original mailbox; no separate lossless mailbox
mirror is required.

Disconnect/disable stops new account work and hides inaccessible live content;
it does not silently delete shared memories. Offer a distinct authenticated
clear-learning/source action, with a precise description of what is removed.
Existing principal erasure includes every new table, exemplar, draft, job, and
derived-source link. Suppression markers retain only the minimum opaque source
identity needed to prevent reimport; principal erasure removes those too.

Treat incoming mail and historical examples as untrusted data. Subject lines,
signatures, forwarded instructions, and embedded text cannot grant authority,
change recipients, weaken approvals, or alter trusted persona. The existing
secret/material restrictions and policy gates remain mandatory.

## 9. Delivery sequence

Owner approval authorizes this feature scope, not a silent declaration that
earlier milestones have completed. Milestone 26 is assigned and its thirty-two gates are registered. Preserve the current verified
milestone ceiling and record existing email activation gaps separately.

| Phase | Deliverable | Exit evidence |
| --- | --- | --- |
| 0. Design contracts | Canonical email-experience design, approved ADR scope, state/map/gate registration, API/events/schema/retention contracts, test corpus and evaluation definitions | Documentation checks; all affected requirements reconciled before code. |
| 1. Shared mode shell | Chat/Email navigation on all three Apple clients; shared connection, preserved Chat state; capability unavailable fallback | Mode-switch, streaming/composer preservation, accessibility and native navigation tests. |
| 2. Inbox foundation | Account identity, reliable bounded sync/history checkpoints, typed projections, safe thread rendering, freshness and other-mail access | Fake-provider contracts, API boundaries, PostgreSQL parity/migrations, two-account failure/recovery tests. |
| 3. Personalized attention | Historical interaction evidence, role/topic profiles, feedback/undo, high-precision selection, automatic history progress | Chronological replay, source/identity deduplication, correction tests, declared ranking benchmarks. |
| 4. Shared semantic memory | Evaluated email extraction with exact provenance and useful shared recall, owner-correction precedence, source lifecycle | Existing memory gates unchanged plus new semantic corpus, cross-mode recall and erasure tests. |
| 5. Adaptive drafting and send | Shared style learning, internal drafts, edit/conflict/revision lifecycle, correct reply threading, exact-message approvals and outcomes | Style/reply-need evaluations, API and multi-device journeys, adversarial/uncertain-send tests. |
| 6. Integrated validation | Performance/cost calibration, long-history resumption, all Apple journeys, real-mailbox smoke on both accounts | Complete local checks and private owner acceptance evidence. |
| 7. Release | Hosted verification and, only when separately authorized, PR/review/merge and production delivery | Final-head CI/CodeRabbit, merged-revision release identities, delivery and installed-client smoke. |

Backend contracts and the client shell can proceed in parallel after phase 0.
Ranking and semantic-memory work can proceed independently after source
provenance exists. Drafting depends on thread/reply contracts and profile
access; final delivery requires all five requested feature areas, not merely an
inbox shell. Do not enable unfinished autonomous drafting or unevaluated
semantic extraction as if the complete mode has shipped.

Likely implementation surfaces are `clients/apple/Veetbot` and its shared tests,
`src/gmail_mcp`, the API/composition/runtime application seams in `agent_core`,
new email domain/repository services, memory formation/context/lifecycle,
migrations, and evaluation fixtures/gates. Keep changes limited to these
requirements; no unrelated framework migration or general provider rewrite.

Canonical documentation surfaces include the engineering plan,
project state/current milestone/map/readiness, the new design, email integration,
Apple client, API/policy scope tables, memory/context/lifecycle, deployment and
data-use documentation. Add verified evidence as phases complete, never in
anticipation of a passing result.

## 10. Acceptance and validation

Behavior changes use Lane A and strict red-green-refactor. Add the smallest
documented behavioral test first, run it to an expected behavioral failure, then
implement and rerun its partition and risk-relevant checks. Environment/import
errors do not count as red evidence. New adapters start with shared contract
tests; every public command includes validation, authorization, failure, and
retry coverage.

### Functional release requirements

| Requirement | Required proof |
| --- | --- |
| Modes are presentation | Switching preserves Chat, memory/persona identity, drafts, active streaming, and selections; thread handoff reuses an ordinary session. |
| Active-only retrieval | Entry/foreground/timer refresh works; hidden/background clients admit no new slices; multi-device requests coalesce; in-flight work finishes within bounds; aggregate reservations survive concurrency/recovery and cannot renew limits per batch. |
| Complete and honest source state | Pagination/change replay/cursor expiry/revocation/partial account failures are correct; no false fully-current claim. |
| Historical power | Progress eventually reaches arbitrarily old eligible history under repeated available active slices; import restarts/duplicates do not double-count evidence. |
| Learning | Person/topic/thread feedback have distinct effects; Chat and Email use the same profile; corrections/undo/reset remove prior influence and rerank already-loaded mail on another device. |
| Relevant drafts | Auto-draft only when a reply is appropriate; no duplicate proposal, overwrite of edits, already-answered reply, or attachment hallucination. |
| Multi-device editing | Revision conflicts preserve both edits; stale source requires review; saves are visible on another client. |
| Sending | Exact account/recipients/subject/body require owner approval; fresh Gmail read detects an external reply since polling; failed freshness defers send; edits invalidate review; post-dispatch uncertainty cannot auto-retry; threading/recipients are correct; cold/warm notification recovery opens the associated draft safely. |
| Shared memory | Email facts affect relevant project-scoped Chat and other-account tasks with provenance; owner corrections prevail; old board/deal evidence remains historically useful without asserting a current role; import time does not renew it. |
| Isolation and trust | Zero foreign-principal exposure, credential storage, instruction promotion, unsupported owner-authority escalation, or cross-account credential dispatch in the adversarial corpus. |
| Lifecycle | Cache expiry, source exclusion, profile reset, and principal erasure cover projections, examples, jobs, revisions, lineage, and formation replay. |

### Approved quality targets

Freeze a private representative corpus before tuning: at least **200 labeled
threads**, spanning both accounts and the six relationship categories, ordinary
personal correspondence, new relevant senders, bulk mail, and low-value mail.
Use a chronological holdout with at least 30 independent inbox snapshots and
100 held-out priority judgments; build profiles from earlier evidence only.
Keep all messages of a conversation out of cross-split leakage, and do not form
memories from held-out future correspondence before assessing it. Label the
complete candidate pool for every holdout snapshot so missed important mail has
a valid denominator; count repeated appearances separately from independent
threads when reporting uncertainty.

Measure the following as release gates, not current capability claims:

- **Priority precision:** at least 90% of displayed top-five items are judged
  worth attention; neither account below 85%. Report sample counts and
  confidence intervals rather than hiding uncertainty behind one percentage.
- **Useful coverage:** on snapshots with at most ten owner-labeled important
  threads, at least 90% appear within the first ten. Include important FYIs and
  material founder/board updates, and report actionable and informational
  subsets and misses by relationship class. For overloaded snapshots report ranked recall and
  overflow separately; the five-row presentation is not a promise that only
  five important messages exist.
- **Personalization:** compare with newest-first, Gmail-important, and the same
  ranker without history/feedback. Target ten percentage points of top-five
  precision lift over the strongest baseline, or at least 95% absolute precision
  without coverage regression when the baseline already exceeds 85%. Declare
  this ceiling rule before evaluation; do not invent an exception after a miss.
- **Draft relevance:** at least 90% of auto-drafted threads are judged to need
  a reply. Draft for at least 80% of unambiguous, fully supported reply
  opportunities within the top-three automatic-draft slots. Assess at least
  50 reply/no-reply cases with at least 30 emitted drafts across both accounts;
  report abstentions and capacity exclusions so doing almost nothing cannot pass.
- **Style usefulness:** in at least 30 blind paired draft comparisons, the
  owner prefers the personalized version at least 70% of the time, and at least
  70% need no substantial rewrite. Score factual faithfulness separately; a
  fabricated commitment or recipient is a blocking correctness failure.
- **Semantic memory:** at least 95% precision and 85% recall on labeled useful
  email facts, zero attribution/authority violations, and demonstrable utility
  beyond the current excerpt adapter. Preserve every existing memory benchmark
  requirement; bind activation evidence to the exact new policy tuple.

The owner reviews small batches through the feedback experience; labels and
examples stay private. Synthetic fixtures cover deterministic and adversarial
behavior, but cannot substitute for the owner's style/importance judgments.
If a target fails, diagnose and improve the implementation or explicitly revise
the requirement before retesting; never weaken an existing acceptance rule.

Performance targets are a cached priority view within one second and a fresh
two-account view within fifteen seconds under the representative test workload.
Report p50/p95, mailbox size, provider latency and partial-result behavior.
Historical work must not block an opened thread, draft editing, or Chat. These
are controlled-load targets, not guarantees against provider outages. Record
historical throughput, model calls, spend, cache hit rates, and estimated
remaining work before activation under existing budgets.

Run `make docs-check`, `make check`, `make test-integration`,
`make test-apple`, and `make test-apple-ui` for implementation delivery, plus
the focused email/memory suites, migration round trips, and governed evaluation
commands registered by phase 0. Run `make citations-fix` after cited document
edits and inspect the result. Native UI evidence must cover iPhone, compact and
regular iPad, and Mac; retain the existing Chat/SSE regressions.

Real-mailbox acceptance proves both account identities and retrieval, historical
progress, meaningful feedback, one useful draft on each account, shared Chat
recall, and an explicitly owner-approved test send with verified recipients and
threading. Live model calls and sends occur only during authorized execution,
only under the applicable explicit authorization. Existing Milestone 18 owner-smoke gaps remain visible until
their own evidence is recorded.

## 11. Authorized scope and delivery boundary

Milestone 26 includes the mode shell, active priority inbox, broad historical
learning, shared sender/content/style personalization, relevant internal drafts,
native editing and approved sending, and useful semantic email memories.

It excludes another provider, self-service account onboarding, terminal UI work,
autonomous sending, automatic Gmail writes, Gmail draft synchronization,
background monitoring/digests, attachment processing, calendar operations,
permanent mailbox deletion, offline-authoritative editing, a separate email
persona, model routing changes, and a general learned memory policy. Existing
Chat tools remain available under their existing contracts.

Owner approval authorizes this implementation scope and its canonical
specification/gate work. It does not authorize creating a pull request,
publishing these documents, merging, deployment, live test sends, or increased
provider budgets. Those actions follow the user's existing authorization
boundaries when the concrete work is ready.

## 12. Source basis

- [Engineering plan](engineering-plan.md), especially Milestones 18, 21,
  22 and the shared-core requirement in Section 29.
- [Email integration](email-integration.md),
  [ADR-0071](../adr/0071-milestone-18-email-integration.md), and
  [ADR-0085](../adr/0085-operator-managed-multi-account-gmail.md).
- [Attributed communication memory](../adr/0090-attributed-communication-memory-formation.md),
  [adaptive memory design](adaptive-memory-distillation.md),
  [persona design](persona-surface.md), and
  [memory retrieval design](memory-retrieval-and-ranking.md).
- [Apple client contract](../apple-client.md) and
  [ADR-0049](../adr/0049-native-apple-client.md).
- Current source and test inspection: Gmail normalization/tool roster,
  communication source admission/formation, Chat-owned root navigation,
  versioned API and native test patterns. Existing implementation is a baseline,
  not evidence that Milestone 26 capabilities pass.
- Glen history: “Finish Gmail Integration Runbook” (Andy, 2026-09-03),
  “Complete Gmail account authorization” (Andy, 2026-09-09), and “Veetbot Memory
  Formation Issue” (Andy, 2026-09-10). Current repository contracts take
  precedence over historical recollection.

## Hard gates

These thirty-two blocking gates belong only to Milestone 26. Pending check
references remain pending until matching implementation and verification exist.
Private quality and owner-smoke evidence cannot be replaced by synthetic tests.

1. **Shared mode state.** Chat and Email preserve the same principal, agent, persona, memories, active Chat run, composer and selections across switching and thread handoff. Registered as
   `gate.email.experience_shared_mode_state`. **M26.**
2. **Native layouts.** iPhone, compact and regular iPad, and Mac expose accessible adaptive navigation and editing while retaining supported operating-system floors. Registered as
   `gate.email.experience_native_layouts`. **M26.**
3. **Scope and feature confinement.** Email application routes and capabilities require the email experience feature flag, exact email.read or email.write scope, and current account-specific authority; disabled composition exposes no email application routes. Registered as
   `gate.email.experience_scope_and_flag`. **M26.**
4. **Stable account identity.** Verified principal-scoped mailbox identity survives default-account changes; reconnecting another Google identity cannot reinterpret old evidence or dispatch with another account credential. Registered as
   `gate.email.experience_account_identity`. **M26.**
5. **Foreground admission.** Entry, foreground return and sixty-second visible refresh admit bounded coalesced work; hidden or background clients admit no new slices and repeated multi-device requests cannot create an unbounded backlog. Registered as
   `gate.email.experience_foreground_admission`. **M26.**
6. **Synchronization contract.** The first-party MCP contract returns bounded versioned account-qualified identities, history changes, labels, direction, dates, reply headers and completeness with shared fake-provider contract coverage. Registered as
   `gate.email.experience_sync_contract`. **M26.**
7. **Synchronization recovery.** Projection updates and cursors commit consistently; duplicate or reordered observations, pagination, cursor expiry, deletions, revocation and partial account failure preserve accurate recoverable source state. Registered as
   `gate.email.experience_sync_recovery`. **M26.**
8. **Historical progress.** Active bounded slices resume through recent and arbitrarily old eligible history without starvation, duplicate evidence or claiming metadata-only coverage as semantic understanding. Registered as
   `gate.email.experience_history_progress`. **M26.**
9. **Governed task execution.** Typed deterministic ingestion uses ordinary durable worker leases, checkpoints, cancellation, policy and budgets without direct API-handler Gmail calls, fabricated owner messages or model-selected pagination. Registered as
   `gate.email.experience_governed_tasks`. **M26.**
10. **Aggregate automatic-email cost.** Atomic reservations and actual usage cover all automatic email stages across accounts and devices, enforce USD 20 per UTC day and USD 200 per rolling thirty days with lower limits prevailing, and release or reconcile reservations on every terminal path. Registered as
   `gate.email.experience_aggregate_cost`. **M26.**
11. **Feedback scope.** Thread, person, topic and reply-need feedback have distinct authenticated effects; explicit owner corrections dominate inferred evidence across Email and Chat. Registered as
   `gate.email.experience_feedback_scope`. **M26.**
12. **Feedback replay and undo.** Idempotent feedback, undo, correction and reset rebuild derived influence from surviving evidence without replaying removed influence or learning negative preference from mere viewing or dismissal. Registered as
   `gate.email.experience_feedback_replay`. **M26.**
13. **Assessment invalidation.** Source and relevant profile or extractor revisions invalidate affected assessments; unchanged current polls spend no classification work while unfinished historical work remains resumable. Registered as
   `gate.email.experience_assessment_invalidation`. **M26.**
14. **Priority quality.** Frozen chronological holdouts satisfy the approved precision, coverage and personalization gates, per-account floors, no future-evidence leakage and reported uncertainty against declared baselines. Registered as
   `gate.email.experience_priority_quality`. **M26.**
15. **Relationship evidence.** Correspondent and organization linkage requires supported identity and relationship evidence; role or domain alone cannot establish portfolio membership, ownership or personal importance. Registered as
   `gate.email.experience_relationship_evidence`. **M26.**
16. **Style lineage.** Historical SENT examples exclude quoted, forwarded, automated and ambiguous-author material as specified; user edits dominate, generated-draft lineage prevents self-training, and learned style never edits trusted persona. Registered as
   `gate.email.experience_style_lineage`. **M26.**
17. **Style quality.** The private paired evaluation satisfies the approved style preference and rewrite thresholds with separately blocking factual and recipient faithfulness checks. Registered as
   `gate.email.experience_style_quality`. **M26.**
18. **Draft relevance.** At most three relevant automatic reply proposals are prepared, with approved reply-need quality and abstention for answered threads, missing decisions, essential unread attachments and unsupported commitments. Registered as
   `gate.email.experience_draft_relevance`. **M26.**
19. **Draft revisions.** Server-owned internal drafts preserve owner edits and immutable history, avoid duplicates, use optimistic concurrency and retain both conflicting device edits without a Gmail write. Registered as
   `gate.email.experience_draft_revisions`. **M26.**
20. **Exact send approval.** Every send requires approval of the exact mailbox, recipients, subject and body; edited content or observed source changes invalidate prior approval, and no direct send API bypass exists. Registered as
   `gate.email.experience_send_approval`. **M26.**
21. **Atomic send claim.** A fresh bounded thread read precedes a serialized local frozen-action claim; concurrent edit or double-send cannot reuse stale approval, while the unavoidable external post-read race is stated honestly. Registered as
   `gate.email.experience_send_claim`. **M26.**
22. **Send uncertainty.** Confirmed sends persist outcome; pre-dispatch failures follow governed retry, but any possibly dispatched non-idempotent send becomes uncertain and never automatically resends after crash or lost response. Registered as
   `gate.email.experience_send_uncertainty`. **M26.**
23. **Reply headers and recipients.** Reply and reply-all use verified sender identities, correct Reply-To and owner exclusion, matching subject/thread and RFC reply headers without trusting message-body recipient instructions. Registered as
   `gate.email.experience_reply_headers`. **M26.**
24. **Semantic memory grounding.** A separately versioned evaluated email extractor yields exact source-span-grounded useful facts with account, message, author and event provenance and the approved precision and recall floors. Registered as
   `gate.email.experience_semantic_grounding`. **M26.**
25. **Semantic memory authority.** Correspondent and historical SENT evidence stay attributed, inferred and sensitivity-governed, cannot overwrite owner assertions, and leave existing evaluated extraction policies unchanged. Registered as
   `gate.email.experience_semantic_authority`. **M26.**
26. **Semantic memory lifecycle.** Historical source age and freshness govern current beliefs; import, repeated reads and self-citation cannot renew stale claims, and deliberate historical recall remains dated. Registered as
   `gate.email.experience_semantic_lifecycle`. **M26.**
27. **Shared recall.** Eligible new-policy contextual email memories and profiles are relevantly usable across the same owner's accounts and Chat without broadening tenant, principal, sensitivity or credential authority or the old adapter's LOCAL contract. Registered as
   `gate.email.experience_shared_recall`. **M26.**
28. **Source erasure.** Scoped authenticated exclusion and erasure remove source copies and derived influence across projections, examples, events, checkpoints, profiles and memory with replay suppression while preserving independently supported owner evidence. Registered as
   `gate.email.experience_source_erasure`. **M26.**
29. **Retention.** The thirty-day body-cache and sent/discarded-draft windows, five-hundred bounded style excerpts, until-deletion audit copies and backup expiry are enforced without misrepresenting cache expiry as universal deletion. Registered as
   `gate.email.experience_retention`. **M26.**
30. **Privacy and trust.** Adversarial mail cannot confer action authority, alter persona or leak foreign-principal data or secrets; raw personal content stays out of logs, notifications and public or checked-in evaluation material. Registered as
   `gate.email.experience_privacy`. **M26.**
31. **Application API boundaries.** Every new public command has happy-path, validation, authorization, conflict, failure and retry coverage with scoped repositories, PostgreSQL RLS, migration parity and current account authority. Registered as
   `gate.email.experience_api_boundaries`. **M26.**
32. **Integrated release evidence.** All functional and quality gates, native platform lanes, cost/performance calibration and authorized two-account mailbox smoke pass; final-head hosted review and merged-revision production evidence remain required before milestone completion. Registered as
   `gate.email.experience_release_evidence`. **M26.**

## Persistence contract

The first implementation stores independently revisioned email application
records in `email_records`, keyed by tenant, principal, kind and opaque key.
The closed application DTO supplies each JSON payload; revision, created time
and updated time are explicit columns. The repository never returns mutable
aliases into stored payloads. Key-ordered pagination accepts an exclusive `after`
key and a limit from one to one thousand. `put` compares the exact existing
revision (zero means absent), requires the new revision to be expected plus one,
and raises a conflict without mutation on a mismatch. Delete likewise requires
the exact positive existing revision; missing or foreign records conflict.

The shared repository contract runs on both in-memory and PostgreSQL adapters.
PostgreSQL uses explicit principal predicates and forced tenant RLS. A principal
lock serializes budget admission, source mutations and dispatch claims inside a
short unit of work; PostgreSQL holds the advisory lock through transaction end,
and the deterministic adapter holds an async lock to context exit. No network
call is made inside either lock. PostgreSQL supplies rollback; the ordinary
in-memory unit of work retains its documented deterministic, non-transactional
scope. Immutable draft and feedback histories use independently keyed records,
not destructive rewriting of a prior revision's content.

### Source-content erasure mechanism

The owner-authorized email-source erasure operation is a narrow content-erasure
exception to append-only event payloads. It preserves event identity, sequence,
actor, time, tool/action identity, approval decisions, and argument hashes; it
replaces only the selected source's content with an explicit erasure marker and
appends an `email.source.erased` audit event. It does not rewrite user messages
or reinterpret an earlier action. The original Gmail account-to-server binding is
server-owned session metadata and must never be refreshed from a later manifest.
The builtin `email.context` result instead carries server-authored source
wrappers in its ordinary runtime event. These remain erasable in an existing
Chat session that has no Gmail-specific metadata; the source's account, thread
and message identities must match. Independent context and writing-example
wrappers preserve unrelated accounts and owner-authored content.

Both persistence adapters expose source erasure through the existing lifecycle
repository. Before mutation they reject an active associated run or unfinished
potential producer: target-account operational sessions, runs with Email or
Gmail-read authority, and pending `email.context` invocations. These must first
be cancelled and allowed to settle, even before their first source result exists.
This conservative owner-scoped guard may wait for another email-capable Chat
run; it does not delete that run or its unrelated content. Session/run locks
serialize the check with admission, and exclusion tombstones prevent new
formation after commit. Source-qualified normalized result
objects are removed from tool events and invocation result copies. Terminal
checkpoints and derived summaries are invalidated so a replay cannot restore the
content; other messages in a mixed thread and owner-authored conversation events
remain. Independently supported owner beliefs remain. Source tombstones survive
content removal and prevent reimport. Exported artifact copies require the
existing artifact deletion/retry machinery; a pending byte deletion is reported
as pending, never as successful erasure. No generic API gains event editing.


### Semantic activation and passage provenance

`AGENT_EMAIL_SEMANTIC_EVIDENCE` optionally names an operator-reviewed JSON
activation artifact. No path means the semantic policy stays off. The loader
requires `email-semantic@1`, the actual provider/model, the exact deployed build
reference, a corpus digest, at least 95% supported precision and 85% useful-fact
recall, a positive improvement over labeled excerpt-baseline comparisons,
preserved existing memory benchmarks, zero fabrication/authority/relevance failures,
positive attribution/cross-project/historical cases, and the current
implementation digest. That digest covers the assessment prompt/application,
source validator, formation and retrieval code; changed code invalidates an
older artifact. Synthetic test fixtures are not activation evidence.

The pure `agent_core.evals.email_quality.score_email_quality` scorer validates
an anonymized label-only corpus and emits aggregate counts, uncertainty and
threshold results. Missing owner evidence and synthetic corpora remain pending.
Its report is distinct from an operator-reviewed activation artifact; unit
tests of the scorer never satisfy the private quality gates.

The semantic source contract accepts bounded passages from `get_thread_page`
and `get_message_body`. A continuation carries its byte offset and the original
header event/session, so the service checks the same account, message, provider
revision, sender, and original evidence date against real governed tool events.
It stores passage hashes and source receipts, not a second body copy. Source
facts deduplicate across passages and re-reads. A passage processed successfully
does not mark an unread remainder as assessed. Coverage remains explicit until
all resumable work is complete. Semantic writes append a content-free current-run
audit under the worker lease in the same transaction; cached older evidence
keeps its original source session/date.

Historical import order cannot supersede newer evidence, and several supported
facts in one source remain distinct. Source erasure also removes selected
inferred beliefs from shared recall traces and invalidates terminal checkpoints
that carried them. Any active dependent run must settle before these retained
copies are purged; independently confirmed owner beliefs remain.
