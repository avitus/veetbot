# ADR-0169: Milestone 32 memory reconsolidation (dreaming)

- Status: Proposed
- Date: 2026-10-02
- Amended: 2026-10-03 (Phase 1 begun at the owner's instruction)
- Amended: 2026-10-06 (owner operation surfaces and validation vocabulary)
- Amended: 2026-10-07 (source-only requests, explicit egress, execution and call audit)
- Amended: 2026-10-08 (grounded writers, composed maintenance and measured comparison)
- User authorization: "Authorize that and propose a plan", referring to
  revisiting the entire memory bank, merging related beliefs and synthesizing
  new connections from roadmap B6
- Related: ADR-0018, ADR-0019, ADR-0069, ADR-0077, ADR-0079, ADR-0090,
  ADR-0100, ADR-0117, ADR-0125
- Detailed design: `docs/plan/memory-reconsolidation.md`

Renumbered from ADR-0148 on 2026-10-09, with owner authorization, when the
unpublished M32 work was integrated with dev. The decision and approval dates
are unchanged. Frozen evaluation artifacts retain their original ADR identifiers.

## Context

Idle-session formation, expiry and daily decay exist. They do not implement
the broader scheduled merging and synthesis described by the original memory
design. Milestone 16 explicitly left belief merge and global consolidation in
B6; Milestone 21 supplies adaptive formation and forgetting rather than a
whole-store pass. Milestone 28 admits only a bounded People graph and its
identity rules. The owner now authorizes broader reconsolidation and asks for
a plan. That authorization is established; the architecture below is proposed.

## Proposed decisions

1. Assign this bounded B6 scope to parallel Milestone 32 without changing the
   sequential verified ceiling. Phase 1 specifies the detailed contracts and
   registers twenty-four gates before runtime work; it also supplies the pure
   evaluation scorer and frozen synthetic fixtures. Runtime gates stay pending.
2. Reuse the maintenance role with durable leases, changed-input and full-scan
   cursors, bounded slices, fair coverage and shared spend reservations. Global
   means across one principal's eligible memories, never across principals.
3. Separate equivalent-claim merging, lossless related summaries and tentative
   inferred connections. Preserve atomic source records and provenance;
   uncertainty, contradiction and person identity cannot be compressed away.
4. Store operations and reverse source dependencies. Revalidate source
   revisions, policy and erasure fences on atomic commit. Invalidate dependent
   outputs before deferred cleanup, and make merge undo respect later deletion
   and correction. No provider call holds a database transaction open.
5. Generated memories are not fresh evidence. Derivation cannot increase
   authority, widen visibility, add corroboration or reset evidence clocks.
   Hypotheses follow the existing finite evidence-based lifetime; persona
   promotion remains an explicit human action.
6. Use the existing lexical and entity substrate first. General multi-hop graph
   inference, embeddings, external memory services, arbitrary history mining,
   learned policies and external enrichment remain in B6. Do not change
   People identity-merge thresholds or re-derivation consent.
7. Require comparative evidence on a frozen corpus and holdout: zero false
   merges or privacy failures, preserved atomic recall, useful new connections
   and better cross-session answers within declared budgets. Exact targets
   and the six build phases are specified in the design, not reported as passed.
8. Gate activation on evidence for this behavior and its actual dependency
   tuple. Milestone 21 is still in progress; its authorization or a historical
   artifact is not proof of current readiness. Keep a kill switch and valid
   source records for rollback. Follow existing PR and deployment authorization.
9. Keep operation inspection and revision-checked merge undo independent of
   processing activation. HTTP requires both default-off memory and reconsolidation
   surface flags; CLI uses the same owner-explicit service. Missing complete support
   withholds all source content, links and counts. When its lower sensitivity cannot
   be certified, only the owner's restricted-ceiling view may inspect opaque history
   or replay an existing undo receipt. Add `400 validation_error` to the closed HTTP
   vocabulary for the M32 routes as their specified contract requires; leave other
   resources' validation behavior unchanged. No arbitrary text-write or provider
   execution route is admitted by these controls.

10. Extend the existing memory routes with opt-in derived browsing and a separate
    many-source summary view. Keep ordinary responses unchanged. Derived owner
    actions preserve originals: review changes only review state, local restriction
    applies to historical reads, and rejection/deletion suppress equivalent summary
    claims and erase their generated copies. Namespaced claim/attribution and leaf
    identity hashes survive policy/model changes without retaining prose. Receipts
    and controls commit together; a dedicated owner-bound, forced-RLS receipt table
    shares the logical memory-write key namespace with ordinary writes. Retain
    deletion fences and refuse downgrade while controls, receipts or summary
    metadata unreadable by the previous application remain.
    Generated-copy erasure participates in the in-memory rollback journal so a
    failed write restores copied context as well as the derived record.

11. Present native inspection as an evidence journal in the Synthesis collection.
    Claims disclose their actual supporting originals, including omitted inputs;
    source navigation fetches the current original. Operation history distinguishes
    kind and state, and undo previews the affected originals. This is presentation
    over the existing owner routes, with no local synthesis, new provider call or
    inferred relationship. Original browsing stays compatible. Private content is
    ephemeral; only an uncertain write's opaque identity survives backgrounding
    for retry on the same connection.

12. Build provider proposals only from authenticated, complete original text parts
    and current direct-memory versions. Require a local egress decision for each
    memory and excerpt; unknown residency or classification defers before
    serialization. Expose this explicit policy seam without an implicit production
    allow rule. Price the entire serialized neutral request conservatively, bind
    its provider/model and policy, and preserve its exact bytes. Preparation remains
    a read-only preview; fresh source/lease/policy checks and durable reservations
    still precede sending. A provider response remains insufficient for a write.
    The verifier receives only locally admitted candidates with fresh original
    evidence. Bind its reply to the full proposal digest and exact requested clause
    set; keep local exclusions separate and irreversible by provider verdicts.
    Classify proposed clause text independently before export. A worker-facing
    admission service holds owner/People guards through the original read, local
    policy decision and durable reservation, and returns only after commit. This
    does not activate the production maintenance pipeline.

13. Execute a proposal/verifier batch outside transactions using one copied model
    resolution, the caller's original slice deadline and a thirty-second lease
    heartbeat. Add an optional neutral per-request send cap; M32 sets one so hidden
    adapter retries, including optional-summary downgrade, cannot spend without a
    reservation. Preserve ordinary retry defaults. Cancellation and uncertain
    usage retain full reservations; publish lower-charge receipts only after
    settlement commits. Return transient reviews for local validation without
    committing operations. Apply merge/summary reviews through the existing
    deterministic planners in one transaction per group, with candidate subsets,
    current source checks and unchanged operation budgets. Complete each group
    only after its writes, publishing receipts after commit. Preserve independent
    group retries and owner undo. Extend existing reservations with closed,
    content-free admission and completion audit, verified usage and immutable
    stage decisions; keep transport completion distinct from local validation.
    Audit and settlement commit together, while recovery retains unknown charges
    without inventing usage. Preserve older unaudited reservations and refuse
    downgrade that loses retained call audit.

14. Reuse the derived projection and owner controls for tentative hypotheses.
    Ground them in independent original events, preserve original evidence clocks,
    and retain complete source lineage. Record conflicts as fixed uncertainty
    notices with dependencies; do not suppress either source. Compose the existing
    bounded executor/application in the maintenance slot behind an independent
    default-off flag and exact release certificate. Independently classify each
    exported content value and require an explicit residency provider pin.
    Revocation removes new processing and derived recall, preserving owner history.

15. Measure the configured model through a separate three-arm runner. Freeze the
    answer-bearing original control first and retain every failed case and uncertain
    charge. Collect observations from actual stores and recall traces; labels stay
    only in the unchanged scorer. Publication recomputes complete observations and
    checks the implementation/model/privacy tuple, upstream formation artifact and
    unchanged Milestone 16 recall floors on the release revision.
    A dirty-tree experiment is failure/quality evidence, never activation evidence.

## Consequences and alternatives

This adds a many-to-one lineage contract, durable maintenance state and owner
inspection/undo work; a periodic prompt alone cannot satisfy those obligations.
No new runtime dependency is proposed. Phase 1 adds twenty-four registry entries
with the detailed design: two observe the frozen evaluation yardstick, while
twenty-two runtime/release assertions remain pending. Its manifest pins M16
control references; recording an original-only control on M32 seeds and the
three-arm comparison remain explicit implementation work.

A destructive summary that replaces source facts would lose detail and make
correction/erasure unsafe. Unbounded all-pairs model comparison would make cost
and completion unpredictable. Reusing inferred outputs as evidence would make
the system self-confirming. All three alternatives are excluded from the
proposal. Keeping only existing decay would not deliver the newly authorized
cross-session consolidation and connections.
