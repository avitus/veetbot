# ADR-0100: People and relationship memory

- Status: Accepted — owner authorized Milestone 28 implementation on 2026-09-15
- Date: 2026-09-15
- Related: ADR-0018, ADR-0019, ADR-0045, ADR-0050, ADR-0069, ADR-0070,
  ADR-0077, ADR-0079, ADR-0090, ADR-0092, ADR-0096
- Design: [People and relationship memory](../plan/people-and-relationships.md)

Activation and rollout are amended by [ADR-0101](0101-people-availability-without-evaluation-gates.md).

## Context

The owner wants Veetbot to understand the people in their life, relationships
to the owner and each other, and the history of past interactions. Existing
relationship beliefs, integrated episodes, and email learning supply parts of
this behavior. They do not establish a stable person identity across sources,
a correctable person-linked history, or a complete People experience.

People is not a single belief kind. A person's identity connects preferences,
relationships, interests, decisions, events, and commitments. A category label
alone cannot provide identity continuity or reconstruct the relevant history.

The engineering plan defers a temporal entity graph and broader session-history
retrieval under roadmap B6. Milestone 17 keeps `/v1/memories` read-only, and
Milestones 21 and 26 exclude the general graph. ADR-0096 caps current automatic
email learning at 90 days. This proposal must name its scope extensions rather
than treating them as already authorized repairs.

## Decisions

1. **Add a person dimension to existing governed memory.** Stable owner-scoped
   person IDs and evidence links connect atomic beliefs and interactions. The
   belief store remains authoritative for factual assertions; profiles are
   rebuildable views, not independently editable biographies.
2. **Use relational temporal links.** Add directed, source-backed relationships
   among owner, people, and bounded organization references in PostgreSQL.
   Distinguish effective, occurred, evidence, and recorded times. Allow bounded
   one-hop retrieval; defer arbitrary graph inference and new infrastructure.
3. **Separate identity resolution from claim confidence.** Reliable identifiers
   and owner-confirmed mappings can link evidence. Names or model confidence
   alone cannot merge people. Keep ambiguity, source-local attribution,
   reversible owner merge/split operations, and durable distinctness decisions.
4. **Make person history a governed cross-session query.** Index eligible
   interactions by participant and source, retaining observed/report/mention
   distinctions. This is an explicit extension beyond the current-session
   episode tool, not a claim that it already provides the desired history.
5. **Keep formation source policies independently evaluated.** New owner and
   email extraction versions emit person-aware claims without modifying frozen
   policies. Existing source trust, sensitivity, portability, provider egress,
   budgets, and approval boundaries remain. Rich SMS body extraction and
   call-derived memory require later explicit source amendments.
6. **Use narrow persistence rules.** Person identities and dated supported
   interactions do not disappear with infrequent contact. Direct owner-stated
   kinship and owner-confirmed identity persist until correction/retraction or
   erasure. Other claims retain their applicable current-evidence horizons;
   stale facts are historical or last-known. This is a new lifecycle decision
   requiring versioned evidence before activation.
7. **Share one bounded retrieval service.** Chat, Email, People tools, and the
   native browser resolve the same identities and respect source/owner/ceiling
   restrictions. Task context fits the existing budget and frozen-prefix
   contract. Memory remains data, never authorization or trusted persona.
8. **Add explicit People read and correction surfaces.** New `people.read` and
   `people.write` scopes govern `/v1/people` routes. Person-linked corrections
   call the governed memory service. Keep `/v1/memories` GET-only. This grants
   a narrowly scoped public write capability only when the milestone is
   accepted, not merely because an alternative URL exists.
9. **Make correction and erasure prerequisites.** Source links and identity
   operation provenance support merge/split, source deletion, person forgetting,
   and principal erasure. Fencing prevents in-flight work from resurrecting
   data. Shared summaries are removed or safely rebuilt. Derived-memory
   forgetting is clearly distinct from original-source/provider deletion.
   PostgreSQL indexes opaque references, fences affected reads and writes, then
   removes generated payloads in durable pages. Dependency discovery follows
   generated artifacts and knowledge into later recalled contexts; original
   source messages remain governed by their existing source-deletion boundary.
10. **Preserve the automatic history limit.** Default email learning remains
    within 90 days. Older source re-extraction requires an explicit range,
    accounts/sessions, record limit, and finite budget, with resumable jobs,
    honest coverage, aggregate cost limits, and cancellation.

## Scope admission and consequences

The owner authorized Milestone 28 as an independent workstream on 2026-09-15. Its phase 0 updates the engineering plan, project state,
current milestone, milestone map, readiness, AGENTS routing, relevant API/tool/
policy contracts, configuration inventory, and executable gate registry.

This ADR admits only the People-specific portion of B6:
person/relationship identity and time, bounded relationship retrieval, and
person-linked interaction history. Embeddings, external memory providers,
general graph reasoning, general belief merging, and global consolidation
remain deferred. Identity merge does not merge belief contents.

The proposal defines 36 acceptance requirements, adapter parity, red-green
implementation slices, synthetic/frozen/private evaluations, performance and
cost targets, migration, rollback, and release evidence. The 36 requirements are registered under Milestone 28. Registration is not
passing evidence. Production selection, historical data access, evaluation
spend, and delivery remain subject to their explicit scopes.

## Alternatives considered

- **Add `people` as a belief type:** cannot represent preferences, relationship
  direction, interactions, and person-specific corrections coherently.
- **Store one generated biography per person:** creates opaque source mixing,
  difficult corrections, and another source of truth.
- **Use the email sender as identity:** fails for aliases, shared/recycled
  addresses, non-email relationships, and people with several accounts.
- **Adopt a graph database or external service immediately:** adds deployment
  and privacy dependencies before bounded relational queries are evaluated.
- **Automatically merge likely matches:** a false identity merge contaminates
  multiple memories; tentative links and explicit repair are preferable.
- **Require confirmation of every memory:** repeats the timid formation problem.
  Formation stays autonomous while established-person merges require owner intent.

## Acceptance status

The owner approved the complete implementation plan with “Implement it” on
2026-09-15. Implementation may proceed through its evidence-backed slices.
Historical import, provider evaluation spend, PR creation, merge, and production
activation retain the explicit boundaries of the accepted design.
