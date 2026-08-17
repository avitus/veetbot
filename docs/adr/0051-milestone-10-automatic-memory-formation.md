# ADR-0051: Milestone 10 automatic memory formation

- Status: Proposed
- Date: 2026-08-17
- Related: Milestone 10, ADR-0003, ADR-0018, ADR-0023, ADR-0045
- User authorization: authorize Milestone 10 and continue building a rich,
  comprehensive memory system

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

## Proposed decision

1. **Make the post-run hook durable and idempotent.** After the terminal
   transaction commits, append `memory.formation.requested` with a derivation key
   containing the run id. Hook failure is logged and never changes the terminal
   run. Waiting runs do not enqueue it.
2. **Consolidate at the idle boundary.** The maintenance role selects flagged
   sessions after 30 seconds without committed activity. Session close remains
   an immediate boundary. The interactive request performs no extraction. The
   selector requires both the session-idle cutoff and the flag's persisted
   `not_before`; a legacy flag without `not_before` falls back to its event time.
   It streams all in-memory session pages through a batch-sized oldest-candidate
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
7. **Keep model-assisted extraction evaluation-gated.** This change does not
   activate an unaudited provider call. The next extractor may use a model only
   as a restricted maintenance or child run with a principal, agent version,
   policy, scopes, budget, deadline, usage record, and the same deterministic
   source and safety gates.
8. **Version the richer policy separately.** New consolidations record
   `formation@2`. Existing `formation@1` records remain valid history, and the
   explicit version boundary keeps replay and later re-derivation comparable.
9. **Serialize each committed prefix.** Extraction stays outside a database
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
- The deterministic extractor is substantially broader and fully offline, but
  it is not open-ended natural-language understanding. Model-assisted extraction,
  semantic conflict resolution, graph memory, and re-derivation hints remain
  future Milestone 10 memory work.
- Milestone 10 is authorized and in progress. The verified gate ceiling remains
  Milestone 9 until its six skill-authoring gates and five memory-maturation
  gates all pass.

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
- **Activate a provider-backed extractor immediately:** deferred until it has a
  budgeted audited run representation and passes the no-fabrication and
  no-policy-regression evaluations required by ADR-0045.
