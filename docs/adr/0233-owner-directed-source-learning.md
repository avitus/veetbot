# ADR-0233: Owner-directed source learning

- Status: Implementation decision under the owner's source-learning request
- Date: 2026-10-21
- Related: ADR-0018, ADR-0051, ADR-0090; Milestone 21
- User authorization: Implement governed source learning and truthful memory-save
  reporting; form more useful memories and reinforce tentative beliefs (2026-10-21).

## Context

An owner-directed family archive investigation saved the owner-supplied location
after the answer, but none of the discoveries. Reading external content tainted
the manual write, while automatic formation admitted only the owner's message.
The reply described a failed manual save without distinguishing later formation.

## Decision

1. Add `memory.learn`, a bounded batch of source-grounded proposals from the
   interactive model. No new provider calls or changes to frozen extraction
   policies are required. The default agent proactively uses it when the owner
   asks to learn from a named website. Ordinary user-message formation continues.
   Source learning and `workspace.write_text` use the existing deferred-tool
   dispatcher, preserving the browser configuration's 6,000-token definition
   budget after adding that dispatcher; both remain enabled and pinned.
2. Admission requires the current interactive run's authenticated owner message
   to explicitly request learning or remembering from a public website URL.
   Successful first-party `web.fetch`, `browser.navigate`, or `browser.observe`
   results from that run must remain within that URL's host and path subtree.
   Neither assistant text, search snippets, arbitrary tools, failed reads,
   scheduled prompts, nor external instructions can supply that authorization.
   Redirected content outside the authorized subtree is ineligible.
3. Each proposal names its read's call ID and an exact evidence excerpt. Local
   validation checks the retained result, source URL, source hazards, claim
   grounding, polarity, ownership, source erasure, and existing corrections.
   Grounding is a conservative lexical check, not proof of semantic entailment;
   the model must distinguish observation from inference. Unsupported personal
   identities, precise dates, current activities inferred from old albums, and
   visual claims from text-only reads are not permitted.
4. New memories are attributed, inferred, provisional, tentative, local, at least
   sensitive, flagged for review, and capped at 0.35 confidence with thirty-day
   expiry. They never retract, supersede, or promote owner beliefs. Independent
   claims coexist, including when they share a subject. Atomic claims retain
   the original owner and tool event provenance and source URL.
5. A proposal may name an existing tentative inferred belief to reinforce.
   Its complete claim must be grounded in the cited excerpt; mere topic overlap
   is insufficient. Reinforcement retains its statement, authority, derivation,
   portability, and current status. It raises confidence only up to 0.55
   (preserving already higher confidence) and preserves both original and
   supporting evidence. One website host contributes at most
   once to a claim, across pages, sessions, retries, and repeated tool calls.
   Corrections, deletion, suppressed sources, and expired beliefs cannot be
   revived through this path. One batch and one run admit at most 32 observations.
   Identical quotations on other websites do not add corroboration. Source-only
   evidence links preserve cross-chat provenance; deleting supporting evidence
   fences the dependent inferred belief for erasure rather than retaining
   unsupported confidence.
6. Each result reports committed, reinforced, unchanged, or rejected separately.
   A manual `memory.remember` trust refusal explicitly describes that individual
   failure and the separate, not-yet-known background formation outcome. Agent
   instructions prohibit equating a failed manual write with zero memories or
   presenting pending background work as completed.

## Consequences

This explicitly amends the source exclusions in ADR-0018 and Milestone 21 for
owner-directed study only. External content remains untrusted for all policy,
instructions, credentials, and consequential actions. No global website trust,
automatic historical backfill, People identity creation, or promotion to a
confirmed fact is authorized by this mechanism. Delivery and a governed replay
of the motivating conversation remain separate from implementation verification.

Migration `e6a3c9d4b217` admits the source-only evidence-link kind in the existing
People head constraint. It creates no person identities. Downgrade refuses while
any such attribution remains, including erased-head suppression history. Source
learning stamps its own policy version on new memories and consolidation audits;
reinforcement keeps the original belief's formation version.
