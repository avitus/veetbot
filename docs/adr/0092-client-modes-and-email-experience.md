# ADR-0092: Client modes and a shared adaptive email experience

- Status: Accepted — owner authorized Milestone 26 implementation on 2026-09-11
- Date: 2026-09-11
- Related: ADR-0049, ADR-0071, ADR-0077, ADR-0079, ADR-0085, ADR-0090
- Approval proposal: [Client modes and email experience](../email-mode-proposal.md)

## Context

The owner requested a comprehensive plan before any code for a new Email mode
on iPhone, iPad, and Mac. Email is a user experience over the existing Veetbot:
shared memories, persona, learning, and capabilities across Chat and both
existing Gmail accounts. Retrieval starts on entering Email and refreshes during
active use. The inbox should contain a short, high-signal selection; relevant
replies should be drafted automatically, edited in Veetbot, and sent only after
explicit approval. Historical received and Sent email should inform importance,
writing style, and useful semantic memories.

The current integration supplies account-isolated Gmail MCP tools, but no
persistent email application interface, foreground synchronization, feedback
model, or native draft state. ADR-0090 admits attributed excerpts rather than
semantic correspondence extraction. These are new requirements, not repairs to
the existing milestone's acceptance criteria. The owner approved the complete proposal on 2026-09-11 and explicitly
authorized a new milestone. Milestone 26 is an independent parallel workstream
with thirty-two gates; the verified ceiling remains Milestone 12. The canonical
design is [email-experience.md](../plan/email-experience.md).

## Decisions

1. **Modes are presentation modules over one assistant.** Introduce a small
   Apple app coordinator and Chat/Email descriptors, preserving the shared
   agent, persona, memory, connection, policy, and run machinery. Mode selection
   confers no authority and does not fork the owner's knowledge. Preserve
   existing Chat state and minimum platform versions.
2. **Add an email application surface backed by MCP.** Extend ADR-0071's
   original tool-only application boundary with typed inbox, synchronization,
   feedback, personalization, source-lifecycle, and draft services. Introduce
   provider-neutral application contracts and an MCP-backed adapter where
   needed. Gmail networking and credentials remain in `gmail_mcp`, with the
   existing import isolation and read/write/send separation. No second provider
   or separate execution queue is introduced.
3. **Refresh is foreground-request-driven.** Immediate and sixty-second active
   client refresh requests admit bounded, coalesced server work. Stop new
   admission when clients cease requesting; finish/checkpoint admitted work.
   Historical processing resumes across visits without a fixed age cutoff.
   This is an extension to email retrieval, not authorization for interval
   schedules, Gmail push, continuous monitoring, or device-presence routing.
   Add aggregate automatic-email cost reservations across accounts, devices,
   history, drafting, style, and formation; approved defaults are USD 20/day
   and USD 200/rolling thirty days. Existing scheduled-cost ceilings are not
   assumed to cover foreground work. Lower applicable limits prevail.
4. **Application-generated ingestion remains governed.** Finite deterministic
   ingestion uses ordinary durable task/run records and the normal tool/policy
   pipeline. Define typed task intent and operational-session indexing in the
   canonical design. Imported mail stays attributed external evidence, never
   forged owner speech. User thread interactions reuse ordinary sessions and
   can be continued in Chat.
5. **Personalization is shared, explainable, and reversible.** Keep an owner
   feedback ledger and derived sender/content/style profiles with exact source
   lineage. Explicit feedback overrides inferred history. A small evidence
   index supports relevant relationship classes without a general entity graph.
   Own rankings and unedited generated drafts are not independent evidence.
   Source or relevant profile-version changes invalidate existing assessments.
   No model fine-tuning, reinforcement learning, new semantic-retrieval stack,
   or general learned memory policy is authorized by this milestone.
6. **Learned style does not write trusted persona.** Supply shared, contextual
   style observations as bounded task data. ADR-0079's explicit affirmation
   requirement remains intact; work and personal context do not become separate
   personas or isolation domains.
7. **Internal drafts have their own revision lifecycle.** Auto-generation and
   autosave affect Veetbot state only. Every external send passes through the
   existing approval lifecycle for exact account, recipients, subject, and body.
   Revision/source changes invalidate review. Preserve the non-idempotent
   uncertain-outcome rule and correct reply-header handling. Automatic Gmail
   draft writes and standing send approval are not included.
   Read the live thread before sending, then atomically claim the local frozen
   action. External changes after that read remain a provider race, not a
   guarantee the local lock can eliminate.
8. **Semantic email memories are a new evaluated source policy.** Extend
   ADR-0090's deterministic excerpt-only mechanism with separately versioned,
   span-grounded extraction. Retain attribution, inferred authority, sensitivity,
   owner-correction precedence, and existing lifecycle gates. Historical import
   does not reset evidence age; preserve expired dated records for deliberate
   historical/as-of recall while excluding them from current-fact snapshots.
   Owner rules, dated interaction aggregates, and inferred current roles/deals
   have the distinct freshness rules in the canonical design. Preserve the old adapter's
   LOCAL portability. The new evaluated policy may emit CONTEXTUAL evidence
   across the same owner's projects, with no tenant/principal/sensitivity
   widening; this explicitly changes ADR-0090's relevance ceiling for that
   policy. Existing provider extractor versions are not changed in place.
9. **Account provenance is durable.** Account-qualified message and thread
   ids, verified mailbox identity, and source-time capability binding survive
   default-account changes and reconnection. Shared knowledge never changes
   which credential may send a particular reply.
10. **The API remains explicit and scoped.** Use `email.read` and
    `email.write` for the new application resources, intersected with current
    account-specific MCP authority and existing run/approval scopes. No direct
    send bypass and no arbitrary write verbs under `/v1/memories` are added.
    Narrow email-source exclusion/reset operations use governed lifecycle
    services and remove derived influence with replay suppression.
11. **Retention and rollout are part of the feature.** Declare cache, exemplar,
    draft, event, backup, and profile retention together; extend principal
    erasure to every new record. Keep raw email and private evaluation content
    out of logs, notifications, and repository fixtures. Activate only after
    the stated correctness, quality, native, and real-mailbox evidence exists.
    Ordinary event/checkpoint/session copies retain the existing until-deletion
    policy even after thirty-day cache expiry, with at most 35 days in backups
    after deletion. Source erasure spans those retained copies and dependent
    observations; the cache window is not represented as global deletion.
12. **Milestone 26 is authorized.** The owner approved this scope and its
    USD 20/day and USD 200/rolling thirty-day budgets on 2026-09-11.
    Register the canonical design and gates before implementation. Existing
    milestone completion and the verified ceiling do not move. PR creation,
    publishing, merge, deployment, live sends, and raised spend limits retain
    their separate authorization boundaries.

## Consequences

- The product gains a native attention and drafting experience without a second
  assistant. New server state and contracts are necessary even though the user
  distinction is primarily a UX mode.
- Current email, memory, and client architecture remain useful foundations;
  their completed safety and acceptance requirements are not weakened.
- Broad historical learning becomes resumable and progressively useful, with
  visible coverage and bounded cost rather than an all-or-nothing import.
- Semantic memory and style quality require private owner evaluation; synthetic
  correctness tests alone cannot establish the owner's definition of relevance.
- Richer HTML/attachments, Gmail draft synchronization, offline edits, and
  unattended monitoring remain future scope.

## Alternatives considered

- **Separate email agent/persona/memory:** rejected; contradicts the owner's
  explicit single-assistant direction.
- **Prompt-only chat triage:** insufficient for durable priority state,
  discoverable feedback, native editing, cross-device revisions, and reliable
  synchronization.
- **Direct Gmail access from native clients:** rejected; duplicates credentials
  and bypasses the authoritative shared core.
- **Always-running mailbox monitoring or Pub/Sub:** unnecessary for the stated
  active-mode requirement and adds independently deferred infrastructure.
- **Automatically saving every draft to Gmail:** unnecessary for editing and
  sending inside Veetbot and conflicts with the existing external-write floor.
- **Treating all historical Sent mail as trusted owner instruction:** rejected;
  source direction does not establish authorship or instruction authority.
- **Silently widening formation or persona trust:** rejected; extraction needs
  new evaluated evidence and persona promotion continues to require affirmation.

### Source-content erasure implementation

Owner-approved source erasure includes retained operational copies. The lifecycle
repository therefore has a narrowly scoped exception to append-only **content**:
an authenticated source-erasure operation may replace a selected, account-bound
email's content in retained event/tool results while preserving event identities,
ordering, action identities, timestamps, approval decisions, and argument hashes.
A separate erasure audit event records the operation. Derived terminal checkpoints
and summaries are invalidated; unrelated messages and owner-authored evidence
remain. Active work must settle first. This is not a general event-update API.
Artifact byte deletion remains durable retry work and is not reported complete
before its acknowledgment. The canonical mechanism is in
[email-experience.md](../plan/email-experience.md).
