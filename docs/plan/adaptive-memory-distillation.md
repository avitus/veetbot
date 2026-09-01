---
title: Adaptive Memory Distillation
status: design
canonical: true
---

# Adaptive memory distillation

This document specifies Milestone 21. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md), extends rather than replaces the
Milestone 9 and 16 memory designs, and is recorded by
[ADR-0077](../adr/0077-milestone-21-adaptive-memory-distillation.md).

The owner authorized this workstream on 2026-08-31 after observing that the
current memory former is dramatically too timid to be useful. The concrete
failure is ordinary and consequential:

> I am building a personal AI agent and I am wondering what to use for web
> search and web fetch.

The active provider-assisted policy forms no ongoing-project memory from that
statement. A useful personal agent should form the direct observation **User is
building a personal AI agent.** It may also form **User likely has
software-development experience.** as an explicitly tentative hypothesis. The
first is stated evidence; the second is a useful inference that must be allowed
to fade if later conversation never supports it.

Milestone 21 deliberately favors useful recall over timidity. A few provisional
false positives are acceptable when their derivation is visible, their evidence
clock is honest, corrections remain durable, and unsupported claims retire.
Safety is not a generic reason to suppress memory. The hard exclusions are the
small set named below; uncertainty is otherwise represented rather than
rejected.

The design is informed by two research directions, with none of their training
machinery imported. [Nemori](https://aclanthology.org/2026.acl-long.1607/)
separates episodic integration from semantic distillation and uses prediction
error to select what a memory system did not already anticipate. This milestone
adopts that three-stage inference shape. [Mem-α](https://arxiv.org/abs/2509.25911)
optimizes memory construction against downstream usefulness. This milestone
adopts the product objective and comparative evaluation, but no reinforcement
learning, learned policy, training loop, or new dependency.

## Scope and outcome order

Milestone 21 delivers six coupled changes.

1. Persist coherent integrated episodes over source events, with complete
   tenant, principal, session, and event provenance.
2. Add `nemori-assisted-v1`, a provider-assisted extractor that performs three
   fixed batched calls: episode integration, causally blinded anticipation, and
   prediction-error distillation.
3. Expand the candidate language to represent direct observations and useful
   hypotheses across ongoing projects, goals, roles, skills, interests, habits,
   constraints, recurring states, relationships, preferences, resources, and
   project facts.
4. Raise automatic formation capacity while keeping it bounded: thirty-two
   proposals per consolidation and six per source event, ranked by directness,
   future usefulness, and subject diversity.
5. Separate evidence freshness from model usage. Supporting events refresh
   `last_evidence_at`; citations refresh `last_used_at` and utility only.
6. Replace the twenty-five-case extraction corpus with at least sixty cases and
   compare deterministic `formation@7`, provider `formation@8`, and Nemori
   `formation@9` on the same labels before activation.

The outcome order is load-bearing:

```text
useful direct recall
  > useful tentative recall
  > benign precision
  > narrow trust-boundary exclusions
  > latency and cost
```

This does not make the exclusions optional. It prevents them from becoming a
catch-all justification for a system that remembers nothing.

## Existing controls stay measurable

Milestone 21 does not mutate the meaning of the completed policies:

```text
formation@7   deterministic-v2       frozen deterministic control
formation@8   provider-assisted-v2   frozen provider control
formation@9   nemori-assisted-v1     new candidate policy
retrieval@3   renders derivation and uses evidence age
lifecycle@2   evidence expiry is independent of use
```

`formation@7` and `formation@8` remain executable against their recorded
fixtures and corpus. Their current candidate ceiling, claim vocabulary,
prompts, and evidence artifacts are historical controls, not implementation
helpers that may be silently widened. `formation@9` composes new objects and
may reuse their public ports; it does not alter their output for the same
input.

Automatic selection activates `formation@9` only on an exact evidence tuple:
extractor version, all three policy versions, model policy, provider, model,
policy profile, compiled policy version, corpus digest, and build reference.
Until such evidence exists, `auto` keeps the currently evidenced
`formation@8`; `required` refuses rather than claiming an unevaluated policy is
active. A content-free selection audit records the decision.

## The integrated episode

Raw events remain the source of truth. An integrated episode is a derived,
rebuildable narrative that gives formation a coherent unit larger than one
sentence and smaller than a whole session.

```text
IntegratedEpisode
  id                         UUID
  tenant_id                  str
  principal_id               str
  session_id                 UUID
  source_event_ids           ordered unique positive sequences
  source_started_at          aware datetime
  source_ended_at            aware datetime
  narrative                  bounded text
  subjects                   ordered unique bounded strings
  integration_policy_version "episode-integration@1"
  derivation_key             sha256(policy + owner + session + source ids)
  created_at                 aware datetime
```

The integration batch contains only trusted user events for the owning tenant,
principal, and session, ordered by event sequence. The provider may make the
narrative coherent and resolve pronouns within that batch; it may not add a
fact with no supporting source text. Every sentence in the narrative cites at
least one source event through the provider response. Local validation rejects
an unknown sequence, an unowned sequence, an empty citation, a duplicate
subject, or a narrative span not supported by its citations. Validation failure
falls back to a deterministic episode whose narrative is the ordered source
text joined under the same bounds.

`IntegratedEpisodeStore` has `put`, `get`, `for_session`, and
`delete_for_session`. `put` is idempotent on `derivation_key`; both in-memory
and PostgreSQL implementations run one contract. Principal and tenant erasure
delete integrated episodes in the same transaction family as beliefs and
formation audits. Because episodes are derived, rebuild never rewrites source
events and deletion never leaves the narrative behind.

## Three fixed batched calls

One `formation@9` consolidation performs exactly three provider calls when it
has at least one eligible new user event. No candidate, sentence, or category
may create an additional call.

### Call 1: episode integration

Input is the bounded ordered source-event batch. Output is one or more
provenance-linked episode fragments that local code validates and persists.
The call sees no recalled belief: its job is to say coherently what happened,
not to decide whether it is novel.

### Call 2: causally blinded anticipation

The cue is the source prefix before each new evidence unit, plus relevant live
memories selected at the prior store position. The call sees the cue and those
memories only. It does not see the current evidence span, the integrated
narrative that includes that span, later events, assistant answers, or a gold
memory label.

The output is a bounded list of predictions and, for each prediction, the
identifiers of the recalled memories that caused it. A prediction without an
attributable memory is ordinary model expectation, not evidence that the
platform already knew the fact.

### Call 3: prediction-error distillation

The final call sees the validated integrated episodes, the source events, and
the blinded predictions. It emits closed semantic claims with exact source
event and evidence-span citations. Local code owns canonical rendering,
scope, sensitivity classification, confidence, longevity, and expiry.

A directly stated claim is not suppressed merely because a general model could
predict it. It may be skipped as redundant only when an existing live memory
already represents it and the matching anticipation cites that memory. The
audit records that memory identifier. A prediction not attributable to a live
memory cannot make a direct observation disappear.

## The candidate language

`MemoryCandidate` gains four closed fields:

```text
claim_kind  ongoing_project | goal | role | skill | interest | habit |
            constraint | recurring_state | relationship | preference |
            resource | project_fact
derivation  direct | hypothesis
longevity   ongoing | durable | tentative
evidence_spans
            one exact non-empty substring per cited source event
```

The older fields remain. `source_event_ids` is still the authoritative
provenance list; `evidence_spans` makes grounding locally decidable rather than
trusting a normalized provider claim. Every exact span must occur in the owned
user event it names. Assistant, tool, model, foreign-principal, and
foreign-tenant events are never direct sources.

The vocabulary intentionally covers activities and partial identity cues. A
candidate is not rejected because it is ongoing rather than timeless, because
it is a hypothesis rather than verbatim, because the wording is ambiguous, or
because its subject may be sensitive. The derivation, longevity, confidence,
sensitivity, and expiry fields carry those distinctions.

Local canonical rendering owns statements. Representative forms are:

```text
ongoing_project/direct     User is building a personal AI agent.
skill/hypothesis           User likely has software-development experience.
goal/direct                User wants to choose web-search and fetch tools.
habit/direct               User regularly runs in the mornings.
constraint/direct          User cannot take meetings on Fridays.
```

Hypothesis renderers use uncertainty language (`likely`, `may`, or
`tentatively`). A provider cannot turn `hypothesis` into an unqualified fact by
choosing its own sentence.

## Capacity and ranking

`formation@9` accepts at most thirty-two automatic proposals in one
consolidation and at most six proposals whose provenance includes the same
source event. The limits apply before commits and after local validation.

The deterministic ordering is:

1. direct before hypothesis;
2. higher future-usefulness score;
3. a candidate introducing a claim kind or subject not yet represented;
4. source event sequence;
5. normalized subject, claim kind, and statement.

Future usefulness is a local closed rubric over claim kind and longevity, not a
fourth model call. Ongoing projects, goals, stable constraints, roles,
relationships, preferences, and reusable skills outrank incidental resources
or one-off project facts. The selector makes a first pass taking one candidate
per subject, then fills remaining capacity in rank order. Proposals rejected by
the global limit or per-source limit remain in the formation audit with distinct
reason codes; no truncation is silent.

## Confidence, evidence, and forgetting

`MemoryRecord` gains:

```text
claim_kind                 the closed category
derivation                 direct | hypothesis
longevity                  ongoing | durable | tentative
last_evidence_at           latest supporting world event
last_used_at               latest faithful model citation, nullable
evidence_count             distinct supporting source events, positive
lifecycle_policy_version   lifecycle@2
```

`corroboration_count` remains the count of distinct supporting sessions and is
not incremented twice by replay. `last_reinforced_at` remains on old records for
compatibility and historical display, but `lifecycle@2` neither ranks nor
expires a belief from it. The migration backfills `last_evidence_at` from
`valid_from`, `evidence_count` from the existing source-event list,
`last_used_at` null, old candidates as `direct`, and a conservative claim-kind
and longevity mapping from belief type. Backfilled rows retain their original
formation version and receive `lifecycle@2` only when the new policy first
updates them; the migration itself records `lifecycle@1-backfill`.

Initial automatic values are policy, not knobs:

| Derivation / longevity | Confidence | Expiry without later evidence |
| --- | ---: | ---: |
| direct durable | 0.65 | type-aware decay horizon |
| direct ongoing | 0.65 | 90 days |
| hypothesis tentative | 0.35 | 30 days |
| explicit remember | 0.95 | no automatic expiry |

Later supporting user evidence refreshes `last_evidence_at`, increments
`evidence_count`, and may increment `corroboration_count`. A direct statement
supporting a hypothesis promotes it to direct; repeated independent cues may
raise a hypothesis within the automatic confidence ceiling without silently
changing its derivation. A user edit, correction, retraction, or deletion is
authoritative and remains durable across replay.

A faithful citation updates `last_used_at` and utility. It does not update
`last_evidence_at`, `last_reinforced_at`, `evidence_count`, corroboration,
confidence, or expiry. Returned-but-uncited utility feedback remains, likewise
without touching evidence. A memory cannot keep itself alive by being recalled.

The bounded maintenance sweep retires tentative hypotheses thirty days after
`last_evidence_at` and ongoing observations ninety days after it. Durable direct
beliefs use the existing type-aware horizon and confidence steps, now measured
from `last_evidence_at`. Explicit remembered beliefs remain outside automatic
decay. Retirement closes validity and emits `memory.retired` with a content-free
reason code.

## Retrieval and uncertainty

Retrieval policy becomes `retrieval@3`. Ranking's age term uses
`last_evidence_at`; usage contributes only through utility. Rendering exposes
derivation and uncertainty without adding raw scores:

```text
[m:8f21a0c3] User is building a personal AI agent.
  (direct, ongoing, medium confidence)
[m:9d02b117] User likely has software-development experience.
  (hypothesis, tentative, low confidence)
```

`RecalledBelief`, recall traces, the user-safe trace view, CLI inspection, and
the read API projection carry `claim_kind`, `derivation`, and `longevity`.
`last_evidence_at` and `last_used_at` are inspectable on the governed human
surfaces. None is policy instruction; recalled text remains memory-trust data.

## Deliberately narrow trust exclusions

Formation rejects only the following trust-boundary failures before ranking:

- a cited event or span does not exist, is not owned by the tenant and
  principal, or is not a trusted user source;
- assistant, model, tool, or externally untrusted text is presented as a user
  observation;
- the authoritative source span is a credential, secret, or PII beyond explicit policy;
- untrusted instruction text is being promoted as an instruction rather than
  remembered as the fact that the user encountered it;
- a write or read would cross tenant or principal scope;
- a durable rejection, deletion, correction, or newer higher-authority belief
  forbids the candidate.

Sensitivity permitted by explicit policy is not itself a reason to reject a
useful memory. It is classified and governed by the existing surface ceilings.
Inference is not itself a reason
to reject; it is stored as a hypothesis. Ambiguity is not itself a reason to
reject; it lowers confidence or longevity. Ordinary professional, health,
relationship, location, and activity context may form autonomously when it has
valid user provenance and explicit policy permits it.

## Formation decision telemetry

Every proposed claim ends in exactly one content-free reason category:

```text
committed_direct        committed_hypothesis      reinforced
promoted                redundant_attributed     superseded
conflicted              rejected_provenance      rejected_credential
rejected_injection      rejected_correction      displaced_per_source
displaced_global        provider_invalid
```

`ConsolidationRun` stores the category counts plus episode count, provider call
count, fallback stages, direct proposed/committed, hypothesis
proposed/committed, and prediction-attributed redundancies. The sum of terminal
categories equals proposals plus provider-invalid claims. Events and metrics
carry counts, versions, and normalized failure classes only—never source text,
episode narrative, evidence spans, or memory statements.

## Evaluation and activation evidence

`evals/capability/memory-formation.v3.json` contains at least sixty authored
cases. At least seventy percent are positive. Every positive case is labeled
`must_form` or `reasonable_to_form`; the narrow negative set is `must_not_form`.
Each expected candidate declares claim kind, derivation, longevity, canonical
subject and statement alternatives, and exact evidence text. Coverage includes
every claim kind, direct and hypothesis formation, compound utterances,
corroboration and promotion, correction, retirement, and self-citation.

The core scenarios include:

1. the personal-agent statement forms the direct ongoing-project memory and
   the tentative software-development hypothesis;
2. a broad compound utterance forms multiple subjects without one category
   consuming the whole budget;
3. a misleading professional cue may form a hypothesis but never a direct
   occupation claim;
4. later support refreshes evidence and promotes a hypothesis;
5. an unsupported hypothesis retires after thirty days and an unsupported
   ongoing state after ninety;
6. repeated citations do not extend either lifetime.

The live-model command below evaluates all three policies over the same cases
and publishes a never-overwritten `MemoryDistillationEvidence` only when:

```bash
RUN_LIVE_MODEL_TESTS=1 agent eval memory-distillation \
  --model-policy balanced \
  --policy-profile default \
  --build-ref IMMUTABLE_BUILD_REF \
  --output PATH_THAT_DOES_NOT_EXIST.json
```

Without the explicit live-model flag it reports a skip and makes no provider
call or artifact. A failed run returns its comparative per-policy metrics but
does not create the output path.

Publication requires:

- invalid provenance, assistant-as-user, promoted injection, credential,
  cross-principal, and cross-tenant failures are all zero;
- direct `must_form` recall is at least 95 percent;
- hypothesis `must_form` recall is at least 80 percent;
- precision over benign positive and negative cases is at least 90 percent;
- useful recall is at least fifteen percentage points above `formation@8`;
- every claim kind has a positive case and the personal-agent core passes;
- measurable user correction or rejection is below ten per one hundred
  automatically formed memories;
- all lifecycle timing, promotion, self-citation, cost, and call-count checks
  pass;
- the exact version and provider tuple matches the artifact.

The thresholds intentionally tolerate some provisional false positives. The
wrong optimization is near-perfect precision achieved by suppressing useful
memory.

## Persistence and migration

One structural revision adds the integrated-episode table, the new belief and
formation-audit columns, and the bounded indexes on evidence expiry and episode
ownership. A separate data revision backfills beliefs in bounded primary-key
pages. Both upgrades and downgrades are explicit; downgrade of the data revision
is lossy only for derived fields and is declared as such. `EXPECTED_REVISION`
advances with the structural head.

The in-memory store and PostgreSQL repositories translate row models by hand,
return domain values only, and run the shared contracts. No provider call occurs
inside a database transaction. The three calls complete and validate first;
episodes, beliefs, the consolidation audit, and the watermark then commit in
short units of work with idempotent derivation keys.

## Build sequence

1. This design, ADR-0077, the project-state authorization, the milestone map,
   readiness verdict, registry entries, and all twenty-four red gates. **M21.**
2. Candidate, episode, evidence-clock, telemetry, and evidence-artifact domain
   values plus shared port contracts. **M21.**
3. The in-memory episode store and `formation@9` deterministic fallback,
   including the personal-agent direct observation and hypothesis. **M21.**
4. The three-call provider pipeline with causal blinding, local grounding,
   fixed call count, normalized failure audits, and deterministic fallback.
   **M21.**
5. Formation capacity, ranking, attributed redundancy, reinforcement,
   promotion, and category accounting. **M21.**
6. `lifecycle@2`, `retrieval@3`, evidence-based expiry, usage-only clocks, and
   uncertainty rendering on every governed read surface. **M21.**
7. PostgreSQL schema, repository, erasure, migrations, and bounded backfill.
   **M21.**
8. Corpus v3, the three-policy offline comparison, activation selection, and a
   live evidence run on the intended production tuple. **M21.**
9. Focused, contract, property, PostgreSQL, complete non-live, documentation,
   hosted CI, and final review lanes on one head. **M21.**

## Hard gates

1. **Completed policy controls are frozen.** The recorded `formation@7` and
   `formation@8` fixtures produce byte-identical normalized outputs while the
   new extractor identifies itself as `nemori-assisted-v1` at `formation@9`.
   Registered as `gate.memory.distill_versions_frozen`, structural. **M21.**
2. **Integrated episodes are coherent and grounded.** A multi-turn session
   produces ordered, provenance-complete episode narratives, and an unsupported
   sentence or foreign citation is rejected before persistence. Registered as
   `gate.memory.episode_integration`, case. **M21.**
3. **Both episode stores obey one contract.** Idempotent put, owner-scoped get
   and paging, session deletion, and principal erasure behave identically in
   memory and PostgreSQL. Registered as
   `gate.memory.episode_repository_parity`, structural. **M21.**
4. **Anticipation is causally blinded.** Over generated event prefixes, the
   anticipation request contains only the cue and memories available before
   the evidence event; it contains no current or future evidence, integrated
   answer, assistant response, or gold label. Registered as
   `gate.memory.anticipation_blinded`, property. **M21.**
5. **One consolidation makes exactly three batched provider calls.** Any number
   of events and candidates still produces integration, anticipation, and
   distillation once each, with no candidate-level call. Registered as
   `gate.memory.prediction_error_calls`, case. **M21.**
6. **Every provider stage has an audited deterministic fallback.** A transient,
   permanent, protocol, or validation failure records only normalized metadata,
   completes with locally derived episodes and candidates, and never partly
   trusts an invalid stage. Registered as `gate.memory.distill_fallback`, case.
   **M21.**
7. **Direct ongoing projects form at high recall.** The personal-agent core
   statement forms `User is building a personal AI agent.` as a direct ongoing
   project with exact user provenance. Registered as
   `gate.memory.direct_high_recall`, case. **M21.**
8. **Useful hypotheses form as hypotheses.** The same core statement may form
   `User likely has software-development experience.` as tentative and never as
   a direct occupation fact. Registered as
   `gate.memory.hypothesis_high_recall`, case. **M21.**
9. **Compound formation is bounded and diverse.** A broad utterance can commit
   multiple categories and subjects, never more than six proposals per source
   event or thirty-two per consolidation; direct candidates precede hypotheses
   and every displacement is counted. Registered as
   `gate.memory.compound_recall`, case. **M21.**
10. **The candidate language is closed and complete.** Every claim kind,
    derivation, longevity, and evidence-span combination validates or fails at
    collection, with unknown values refused. Registered as
    `gate.memory.candidate_schema`, structural. **M21.**
11. **Predictability suppresses only attributable redundancy.** A direct claim
    is skipped only when a live existing memory represents it and the matching
    anticipation cites that memory; general model expectation alone suppresses
    nothing. Registered as `gate.memory.predictability_attributed`, case.
    **M21.**
12. **Every automatic claim is source-grounded.** Over generated ownership and
    event-kind combinations, all evidence spans occur exactly in owned trusted
    user events, and assistant, tool, model, foreign-principal, or foreign-
    tenant content commits nothing. Registered as
    `gate.memory.source_grounding`, property. **M21.**
13. **Decision telemetry accounts for every proposal.** Direct, hypothesis,
    commit, redundancy, rejection, conflict, promotion, and displacement reason
    counts reconcile exactly without statements or evidence text in telemetry.
    Registered as `gate.memory.formation_reason_telemetry`, case. **M21.**
14. **Only later evidence refreshes the evidence clock.** A supporting user
    event advances `last_evidence_at` and the evidence counters exactly once;
    replay, consolidation time, and citation do not. Registered as
    `gate.memory.evidence_clock`, case. **M21.**
15. **Usage has its own clock.** A faithful citation changes `last_used_at` and
    utility only, an uncited return changes utility only, and repeating run
    completion is idempotent. Registered as `gate.memory.usage_clock`, case.
    **M21.**
16. **Unsupported hypotheses retire after thirty days.** A tentative hypothesis
    with no later evidence is live before its boundary and retired at it,
    regardless of repeated retrieval or citation. Registered as
    `gate.memory.hypothesis_retirement`, case. **M21.**
17. **Unsupported ongoing observations retire after ninety days.** An ongoing
    direct observation follows the same exact boundary and self-use cannot
    extend it. Registered as `gate.memory.ongoing_retirement`, case. **M21.**
18. **Later evidence promotes rather than duplicates.** A direct supporting
    statement for a hypothesis updates the existing belief to direct, refreshes
    evidence, preserves all provenance, and creates no second live subject/type
    belief. Registered as `gate.memory.evidence_promotion`, case. **M21.**
19. **Recall renders uncertainty faithfully.** Direct and hypothesis beliefs
    expose their derivation and longevity in rendered context, traces, CLI, and
    API views without exposing raw model scores. Registered as
    `gate.memory.uncertainty_rendered`, case. **M21.**
20. **Corrections remain durable through the new pipeline.** A rejection,
    edit, retraction, or deletion cannot be recreated by episode integration,
    distillation, retry, or replay. Registered as
    `gate.memory.correction_durable_v3`, case. **M21.**
21. **The schema migration and backfill preserve history.** Clean and stepwise
    upgrades create episodes and new fields, bounded backfill gives every old
    belief valid conservative values without changing its statement,
    provenance, authority, or formation version, and erasure removes the new
    rows. Registered as `gate.memory.schema_backfill`, structural. **M21.**
22. **Corpus v3 has the declared coverage.** It contains at least sixty cases,
    at least seventy percent positive, every claim kind and label class, all six
    core scenarios, exact evidence text, and no duplicate identifier.
    Registered as `gate.memory.formation_corpus_v3`, structural. **M21.**
23. **Comparative evidence proves marked useful-recall lift.** One offline run
    compares `formation@7`, `formation@8`, and `formation@9`; evidence publishes
    only at the direct, hypothesis, precision, useful-lift, category, lifecycle,
    correction-rate, call-count, cost, and zero-boundary-failure thresholds,
    and never overwrites an existing artifact. Registered as
    `gate.memory.comparative_evidence`, case. **M21.**
24. **Activation is exact and evidence-bound.** Over generated tuple
    differences, `formation@9` activates only for the exact artifact tuple;
    `auto` otherwise keeps the evidenced older policy and `required` refuses.
    Registered as `gate.memory.distill_activation_bound`, property. **M21.**

## Tracked metrics

Track direct and hypothesis proposal, commit, reinforcement, promotion,
correction, rejection, and retirement counts; recall and precision by claim
kind and derivation; per-source and global displacement; prediction-attributed
redundancy; episode count and size; provider calls, fallbacks, latency, tokens,
and cost per stage; evidence age and usage age separately; correction and
rejection per hundred automatic memories; and the exact corpus and policy
versions. Metrics never contain source text, episode narrative, evidence spans,
or belief statements.

## Exclusions

Milestone 21 adds no reinforcement learning, model fine-tuning, vector store,
embedding dependency, temporal entity graph, persona editor, global cross-
principal consolidation, session-history retrieval arm, artifact retrieval
arm, or external memory service. It does not add a new public write API. Those
remain separate roadmap choices. The milestone is a higher-recall formation
and honest-forgetting change over the existing governed memory system.
