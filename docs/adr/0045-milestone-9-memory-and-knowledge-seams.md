# ADR-0045: Milestone 9 memory and knowledge seams

- Status: Proposed
- Date: 2026-08-04
- Related: Milestone 9, ADR-0003, ADR-0006, ADR-0014, ADR-0018,
  ADR-0019, ADR-0020, ADR-0021, ADR-0033
- Detailed design: `docs/plan/memory-formation-and-consolidation.md`,
  `docs/plan/memory-retrieval-and-ranking.md`, and
  `docs/plan/knowledge-documents.md`

## Context

Milestone 9 is the first implementation of governed belief formation,
consolidation, hybrid retrieval, faithful recall traces, and separately governed
knowledge documents. The detailed designs fix provenance, isolation, trust,
deletion, temporal, and evaluation properties but intentionally leave several
reversible seams for the first implementation: the human management surface,
the initial extraction mechanism, conflict granularity, audit-event ownership,
the first retrieval backend, evaluation carry semantics, and builtin tool
classification.

These choices preserve the engineering plan's requirements. They are recorded as
Proposed because the repository owner asked to review decisions made while the
milestone was implemented unattended.

## Proposed decisions

1. **Human memory management initially uses the existing CLI.** `agent memory
   list`, `edit`, and `delete` provide the required inspect-and-correct surface.
   The closed HTTP route set is unchanged until a later milestone explicitly
   designs a remote management API.
2. **Formation begins with a deterministic structured extractor.** Explicit
   `memory.remember` requests and a deliberately narrow consolidation grammar
   recognize durable preferences and “remember that” facts. Model-assisted
   extraction remains behind the formation port and requires evaluation evidence
   before activation.
3. **A principal has one live belief per normalized subject and belief type.** An
   exact duplicate reinforces the existing belief; a different statement for the
   same pair supersedes it and links both records. Rejected or deleted beliefs
   are tombstoned so replay cannot silently recreate a correction.
4. **Management audit events remain in the belief's source session.** Each belief
   retains its source session and source event sequences. Edit, reject, delete,
   expiry, reinforcement, and supersession append public audit events to that
   session so provenance remains reconstructible without inventing a global
   administrative session.
5. **The initial PostgreSQL retrieval backend uses built-in full-text search.**
   Structured subject matches and lexical matches are fused with deterministic
   reciprocal-rank fusion and hand weighting. No vector extension or embedding
   dependency is introduced until a semantic-retrieval evaluation demonstrates
   measurable lift.
6. **Memory-carry evaluations re-form beliefs in the isolated second arm.** The
   harness copies no database identifiers. It creates new, valid user source
   events and passes the prior arm's supported statements through the ordinary
   formation service, preserving semantic carry and valid provenance in each
   isolated composition.
7. **The five new builtins use the ordinary centralized tool pipeline.** Memory
   and knowledge reads are low-risk read-only operations. `memory.remember` and
   `knowledge.ingest` are medium-risk, idempotent application writes; ingestion
   requires `knowledge.write`. Tool output is labeled `MEMORY` or `KNOWLEDGE`,
   and untrusted tool output cannot be promoted by `memory.remember`. The closed
   side-effect vocabulary has no internal-application-write value, and
   `EXTERNAL_WRITE` specifically means modifying data outside the platform, so
   these governed internal writes retain `SideEffectClass.NONE`; their risk,
   write scope, origin-trust check, and memory action kind supply the applicable
   controls. `knowledge.ingest` derives a stable document id from the invocation
   when the caller omits one, and an identical retry returns the existing
   version, which makes its `IDEMPOTENT` declaration true under crash replay.
8. **Retained knowledge sources have no ordinary artifact expiry.** Successful
   ingestion atomically promotes the verified source artifact into the knowledge
   corpus and clears its transient expiry. Document deletion removes versions,
   chunks, retained bytes, and citation visibility together while leaving an
   explicit deletion marker in historical recall traces.
9. **Session memory snapshots are immutable for one context epoch.** The first
   model request retrieves and records the core snapshot, watermark, and trace.
   Later in-turn recalls may inform the mutable body but do not rewrite the
   stable prefix or its hash. Request assembly records whether a non-empty
   snapshot or in-turn recall actually contributed, and the runtime persists
   that trust marker before dispatching any model-proposed tool call.
10. **Plain-text knowledge ingestion is capped at 32 MiB.** The service rejects
    declared sources above that ceiling before opening them, and the extractor
    independently enforces the same bound while streaming so incorrect artifact
    metadata cannot create an unbounded buffer. This is a reversible operational
    ceiling for the first in-process UTF-8 extractor, not a format limit on the
    retained source artifact.
11. **Sensitivity is a surface ceiling, not a new read scope.** The Milestone 9
    runtime surface is private and therefore requests the `RESTRICTED` ceiling.
    Snapshot callers can now pass both a lower ceiling and a surface id. The two
    search tools keep empty `required_scopes`, as the knowledge design requires;
    adding an undocumented read scope would expand the closed vocabulary and
    make the implementation diverge from the plan. A later shared-surface caller
    must supply its lower ceiling rather than infer sensitivity from principal
    write scopes.

## Consequences

- Memory behavior is deterministic, testable without model credentials, and
  replaceable through explicit formation, retrieval, ranking, and storage ports.
- Corrections and deletions are durable and auditable, including after a policy
  re-derivation or process restart.
- A model proposal derived from recalled memory retains `MEMORY` provenance and
  cannot be mistaken for a direct user-sourced persistent-memory write.
- PostgreSQL installations need no extension beyond the existing service, at the
  cost of deferring semantic similarity until it proves useful in evaluation.
- Remote clients do not yet have a human memory-management API; operators and
  local users use the CLI.
- Historical knowledge traces remain honest about deleted sources without
  retaining deleted passage content as a user-visible citation.
- Oversized text sources fail before extraction, while retained artifacts remain
  available for a future streaming or format-specific extractor.

## Alternatives considered

- **Expose management routes in the Milestone 5 API:** rejected because the route
  set is closed and no authorization or error contract is designed for them.
- **Use a provider model for all formation:** rejected because it would make the
  initial gates credential-dependent and could fabricate unsupported beliefs.
- **Keep multiple conflicting beliefs live:** rejected because the first policy
  would make current recall ambiguous; history remains available through linked
  superseded records and `as_of` retrieval.
- **Copy memory rows between evaluation arms:** rejected because copied source
  event identifiers would not exist in the isolated second arm.
- **Add pgvector immediately:** rejected because the plan requires measured
  retrieval quality, not a particular index technology.
- **Leave source artifacts on their ordinary expiry:** rejected because a live
  knowledge document must remain reopenable and deletable as one governed unit.
