# ADR-0051: Milestone 10 automatic memory formation

- Status: Accepted
- Date: 2026-08-17
- Related: Milestone 10, ADR-0003, ADR-0018, ADR-0023, ADR-0045, ADR-0057
- Superseded in part: ADR-0057 replaces decisions 7 through 9 where they govern
  production activation, the provider budget, call audit, and policy selection.
- User authorization: authorize Milestone 10 and continue building a rich,
  comprehensive memory system
- User authorization: activate a richer extractor because memory formation is a
  key product differentiator (2026-08-18)

## Context

Milestone 9 implemented durable beliefs, explicit `memory.remember`, correction
and deletion, recall, and a session-close consolidation callback. The ordinary
conversation path was still broken in two basic ways. The runtime never emitted
the post-run formation flag its design required, so clients that did not call
the internal close operation never reached consolidation. The deterministic
extractor recognized only “remember that” and one preference grammar and
returned at most one candidate per event. An ordinary message mentioning an
Apple Watch and a BMW X3 therefore formed neither memory.

The detailed design calls for schema-constrained model extraction as a
restricted background job. ADR-0045 deliberately withheld activation until an
evaluation demonstrated formation lift without fabrication or policy regressions.
Calling a provider inline from the interactive run would evade that evidence
gate, add unbudgeted model usage, and contradict the session-idle cadence.

## Decision

1. **Make the post-run hook durable and idempotent.** After the terminal
   transaction commits, append `memory.formation.requested` with a derivation key
   containing the run id. Hook failure is logged and never changes the terminal
   run. Waiting runs do not enqueue it.
2. **Consolidate at the idle boundary.** The maintenance role selects flagged
   sessions after 30 seconds without committed activity. Session close remains
   an immediate boundary. The interactive request performs no extraction. The
   selector requires both the session-idle cutoff and the flag's persisted
   `not_before`; a legacy flag without `not_before` falls back to its event time.
   A malformed or timezone-naive value is ineligible in both adapters instead of
   aborting a sweep or depending on the PostgreSQL session timezone. The selector
   streams all in-memory session pages through a batch-sized oldest-candidate
   buffer and orders PostgreSQL candidates oldest first, so newer unflagged
   sessions cannot starve older work. PostgreSQL adds a composite
   event-type/session/sequence index for this scan.
3. **Add structured candidate and extractor boundaries.** `MemoryCandidate` is a
   frozen proposal value carrying type, subject, statement, polarity, source ids,
   confidence, scope, portability, sensitivity, and temporal hints.
   `MemoryCandidateExtractor` is the replaceable port. The deterministic v2
   implementation can emit multiple independently addressable candidates for
   ownership, preferences, user attributes, relationships, decisions, outcomes,
   and ordinary ownership retractions, including coordinated possessives without
   reasserting their trailing entity. New sentence boundaries, first-person
   conjunctions, and structured text parts are separate candidate boundaries, so
   unrelated durable statements cannot be combined into one conflict subject.
   Accepted candidate validity and expiry hints are preserved on the belief.
4. **Recheck provenance after extraction.** The formation service accepts an
   automatic candidate only when every named source is a selected
   `user.message.created` event authored by the owning principal. This check is
   independent of the extractor so a future model cannot grant itself trust.
   The service likewise rejects a candidate whose proposed scope differs from
   the scope authorized for that consolidation job.
   Automatic beliefs enter as `INFERRED` and `PROVISIONAL`; sensitive ones are
   also flagged for review. Extractor confidence is untrusted proposal metadata;
   inferred records are capped at `0.55` until independent reinforcement, while
   explicit user-authored writes retain their higher-authority confidence path.
5. **Use semantic subjects as conflict keys.** Device entities are separate
   subjects. Common preference domains use stable subjects such as answer style,
   interface theme, indentation style, and measurement units. A correction then
   supersedes only the related belief instead of every preference about the user.
   Other preferences derive a topic key rather than falling back to `user`, and
   a subject retracted in a source event is excluded from positive possessive
   proposals from that same event. First-person singular and plural preferences
   also share the canonical statement shape `User prefers {value}.`, so changing
   pronouns cannot turn an unchanged preference into a contradiction.
6. **Bound formation before commit and preserve audit truth.** The deterministic
   extractor may scan up to 256 proposals, while the service consumes at most
   twelve automatic candidates even if an extractor overproduces. Existing
   secret, injection, transient-detail, portability, rejection, and conflict gates
   still run for each consumed proposal. The audit records the full returned
   proposal count and counts every excess proposal as rejected, rather than
   allowing the extractor's resource ceiling to masquerade as the policy ceiling.
7. **Provide hybrid model-assisted extraction only on the maintenance path.**
   The reference implementation makes one strict-schema provider call over trusted
   principal-authored events and a compact view of at most one hundred existing
   beliefs; injection-shaped existing statements are replaced by `[BLOCKED]`.
   Non-routed policies, missing credentials, routing
   failures, malformed responses, timeouts, and budget failures use the
   deterministic v2 result. The hybrid result preserves deterministic candidates
   and adds only proposals whose subjects plus named and numeric claims are
   grounded in their cited source text. The formation service still owns every
   provenance, scope, portability, eligibility, rejection, conflict, and commit
   check. ADR-0057 subsequently requires exact evaluation evidence before this
   kind of extraction can be selected by production composition; a routed policy
   alone is not activation evidence.
8. **Give the reference extractor a separate budget and content-free usage audit.** A model
   attempt is limited to one call, 65,536 input bytes, 16,384 reported input
   tokens, 4,096 output tokens, $0.25, 60 seconds, and 256 proposed candidates.
   `memory.extraction.completed` records the provider, resolved model, model
   policy, normalized usage, returned count, fallback state, and error class; it
   stores neither source text nor model output.
   ADR-0057 defines the tighter production provider budget and its expanded
   `memory.provider_extraction.*` audit contract.
9. **Version the richer policy separately.** Evaluated provider-assisted consolidations
   record `formation@3`. `formation@1` and deterministic `formation@2` records remain
   valid history, and the explicit boundary keeps replay and later re-derivation
   comparable.
10. **Serialize each committed prefix.** Extraction stays outside a database
   transaction. Before writing, the service takes a per-principal, per-session
   claim and rechecks the watermark. PostgreSQL holds a transaction-scoped
   advisory lock until the beliefs, one aggregate audit, and the watermark commit
   atomically; the in-memory adapter provides the equivalent process-local claim.
   Lock contention is a no-op, and a rollback leaves the prefix pending. The
   aggregate audit owns the formation id stored by every new belief and measures
   from before extraction through commit preparation. A non-positive maintenance
   batch limit selects no sessions in either adapter. Because the existing audit
   schema has no `unchanged` field, an idempotent same-source replay counts as a
   rejection; this preserves a reconciled terminal outcome for every proposal
   without misreporting the no-op as a write or reinforcement. PostgreSQL wraps
   each replacement insert and guarded retirement in a savepoint, so catching a
   stale-current conflict cannot commit an orphan replacement in the outer unit
   of work.

## Consequences

- Ordinary Apple Watch and BMW X3 mentions now form two separate memories after
  the session goes idle, even when the client never explicitly closes it.
- Runs complete at the same point they did before; memory enrichment is
  eventually consistent and may appear about 30 seconds later.
- PostgreSQL and in-memory maintenance use the same selection contract. No new
  table is required because the flag is an event and the existing consolidation
  watermark is the completion cursor; one migration adds the composite event
  index required to keep the formation scan bounded in PostgreSQL.
- Multiple maintenance workers may select the same flagged session, but only one
  can claim and commit a given prefix. The aggregate audit distinguishes commits,
  reinforcements, supersessions, rejections, and no-op contention.
- Routed production policies are eligible for model-assisted extraction during
  maintenance only when ADR-0057's exact evidence check passes. The deterministic
  extractor remains the default and the fallback for every provider or validation
  failure. Semantic conflict resolution, graph memory, and re-derivation hints
  remain future memory work.
- Milestone 10 is authorized and in progress. The verified gate ceiling remains
  Milestone 9 until its six skill-authoring gates and eleven memory-maturation,
  inspection, and provider-assistance gates all pass.

## Alternatives considered

- **Run full extraction after every turn:** rejected because it adds latency and
  cost to the user-visible run and contradicts the documented cheap-flag/idle
  split.
- **Rely on clients to close sessions:** rejected because normal clients keep
  conversations open and lifecycle correctness belongs to the shared core.
- **Ask the primary model to call `memory.remember` for every durable fact:**
  retained as the explicit path, but rejected as the sole automatic mechanism
  because it depends on prompt compliance and cannot be replayed from the event
  log under a newer formation policy.
- **Activate an unbounded provider-backed extractor:** rejected. Provider output
  remains a proposal behind strict schema validation, lexical grounding,
  dedicated budgets, content-free usage audit, deterministic fallback, and all
  existing service gates.
