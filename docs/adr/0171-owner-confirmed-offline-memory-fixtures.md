# ADR-0171: Owner-confirmed real-data fixtures for offline memory evaluation

- Status: Accepted — owner approved on 2026-10-08; isolated evaluator implemented; exact-text confirmation remains required
- Date: 2026-10-08
- Amended: 2026-10-09 — owner authorized removing review expiry and checking confirmation at Run
- Related: ADR-0169, ADR-0170, ADR-0077, ADR-0100
- Governing design: `docs/plan/memory-reconsolidation.md`

Renumbered from ADR-0150 on 2026-10-09, with owner authorization, when the
unpublished M32 work was integrated with dev. The decision and approval dates
are unchanged. Frozen evaluation artifacts retain their original ADR identifiers.

## Problem

The owner requested a review of actual reconsolidation output using their own
data. Their completed usefulness review covered assistant-prepared suggestions,
not output from the reconsolidation executor. Those decisions explicitly set
`apply_changes: false` and cannot authorize changes to stored memories or promote
the suggestions into original evidence.

Production predates M32's content revisions, reconsolidation repositories and
retained People attribution keys. A read-only preflight also found legacy purged
People heads whose current payloads no longer exist. The current migration leaves
their attribution unknown; the repository deliberately refuses source preparation
for that owner. Installing the migration alone does not recover lost keys.
Retained erasure receipts name some legacy identities, but identity coverage alone
does not prove complete attribution. Some attribution-bearing identities are not
named by those receipts either. Do not guess an empty footprint or restore erased
payloads to make an experiment run.

The documented `agent memory reconsolidate --dry-run` selects IDs and exclusion
reasons without provider calls or writes. It cannot generate suggestions. The
existing synthetic evaluator creates fixture events from seed statements; putting
real memory summaries into that helper would misrepresent their original evidence.

## Decision

Keep production admission, legacy-erasure refusal, the provider-free dry run,
numeric quality floors and release requirements unchanged. Introduce a separately
named, explicitly requested offline evaluation using owner-confirmed fixture text.
This is an addition to evaluation source admission, not a production workaround.

1. Prepare at most sixteen currently owner-visible memory statements from the
   already reviewed sample, together with exact source text where it is still
   available through authorized reads. Exclude any known rejected, erased,
   excluded or unavailable source. Display the exact text and current status
   locally. Do not expose purged records or retrieve unrelated history.
2. Before any provider call, ask the owner to confirm the exact fixture text as
   information they currently choose to supply for this one experiment. Display
   the selected provider/model and maximum USD 0.25 cost. Earlier usefulness
   judgments and approval of this ADR are not this text-level confirmation.
   Cancel or changed text invalidates that confirmation.
3. Treat the confirmation as a new, evaluation-only owner input with its actual
   timestamp and fresh evaluation identities. Retain original memory IDs only as
   display references. Never impersonate old authenticated events, invent missing
   attribution, backdate the confirmation, or describe this as a faithful replay
   of the production bank. Assistant-written suggestions are not fixture evidence.
4. Use the existing grouping, proposal builder, executor, source-only verifier
   and local operation validators against an isolated store. Apply operations only
   to that disposable store to observe actual accepted outputs and rejections.
   No production database connection or write capability reaches the evaluator.
5. Keep the existing per-content classification, explicit provider-residency pin,
   source completeness, scope, token/byte/time bounds and single-attempt requests.
   Admit at most four groups and two provider calls. Reserve the full USD 0.25
   experiment ceiling durably before sending, retain that charge after uncertain
   completion, and include it in a shared USD 2 daily experiment ceiling. Do not
   retry implicitly or fall back to hand-authored suggestions.
6. Keep source and generated text in the local review process, with no-store
   responses. Persist only consent/source digests, opaque IDs, implementation/model
   identity and content-free usage/outcome receipts. The owner may explicitly
   export their feedback. The review has no automatic expiry; saved corrections,
   selections and results survive browser refresh or tab closure while the local
   process runs. **Cancel & discard** closes the experiment and discards private
   content, as does process shutdown. Browser tab closure is not experiment closure:
   browsers cannot reliably distinguish it from refresh. Run records a fresh
   submission bound to the unchanged packet and selected references; the evaluator
   refuses submissions older than thirty minutes without renewing the original
   per-input confirmation timestamps. This separates review retention from consent
   freshness without disk or browser persistence of private text.
7. Present actual accepted outputs, local rejections and abstentions honestly.
   Label the input as owner-confirmed real-data fixtures and the output as an
   isolated evaluation. This measures usefulness of the processing code on those
   inputs; it does not certify historical attribution, whole-bank selection,
   production erasure/recovery behavior, comparative quality or activation.

## Required verification before the first experiment

Tests must first demonstrate the missing boundaries, then cover: absent/stale
consent, changed text or model, owner isolation, unavailable/erased sources,
accurate confirmation timestamps and identity mapping, prohibited egress,
oversized evidence, duplicate attempts, concurrent spend admission, unknown usage,
timeout/cancellation, zero production writes and faithful presentation of rejected
or empty output. Fixture tests must use fabricated data; private owner text does
not enter the repository or CI. The existing M32 contracts and final repository
checks remain required for any implementation change.

## Alternatives and limits

- Keep waiting for faithful bank replay. This preserves every existing boundary
  but needs a separate solution for irrecoverable legacy attribution; a schema
  upgrade alone is insufficient.
- Ignore unknown attribution, substitute readable memory summaries for original
  messages, or label hand-written suggestions as pipeline output. Rejected.
- Activate production processing to obtain examples. Rejected while quality and
  release gates fail.

The owner explicitly approved ADR-0171 on 2026-10-08 after confirming its number.
Approval authorizes implementation of this offline fixture path. It does not
authorize production changes, data export before the exact-text confirmation,
applying reviewed suggestions, restoring deleted material, changing benchmark
labels, or weakening the current runtime's fail-closed behavior.
