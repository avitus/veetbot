# ADR-0290: Semantic retrieval activates on the owner's standing decision

- Status: Accepted (explicit owner direction on 2026-11-03)
- Date: 2026-11-03
- Related: ADR-0178, ADR-0188, ADR-0199, ADR-0220, ADR-0223, ADR-0246, ADR-0269, ADR-0274, ADR-0289
- Detailed design: `docs/plan/general-memory-recall.md`, `docs/plan/knowledge-documents.md`

## Context

Semantic recall has run in production only under the bounded owner trial
(ADR-0223): an admission that expires 24 hours after the last timely owner
check-in and seven days after it started, bound to the exact release tree so
that a deployment withdraws it unless the handoff verifier (ADR-0274) carries
it across. The trial that began on 2026-10-28 lapsed, the one that began on
2026-11-01 expired on 2026-11-02, and on 2026-11-03 production answered from
sparse recall again while the newly merged knowledge semantic arm (ADR-0289),
which borrows the memory index's admitted provider, had no provider to borrow.

On 2026-11-03 the owner directed: "Activate the semantic arm. It has been
turned on and off for many months now and we are failing to make progress."
That is a decision about the activation contract, not another trial.

Two facts shape the mechanism. The production encoder is the local MiniLM
tuple (ADR-0188): 384 dimensions, a 256-token input capacity, and no network
egress, so belief text never leaves the host. Knowledge chunks target 600
tokens, so under that encoder every knowledge ingest would have failed on
capacity, and the documents already in knowledge carry no vectors. And when
gate 13 was re-recorded in that encoder's space, the lift ADR-0289 measured
with OpenAI's model was not there: MiniLM scores by shared words, so a
section that repeats "click" or "tray" outranks the passage that answers.

## Decision

1. **A standing owner admission.** `recall-standing-admission@1` and
   `recall-standing-evidence@1` bind the owner's actual instruction (its text,
   a reference to the message that carried it, and the statement's digest),
   one tenant and owner, a decision time at or before activation, and the
   embedding configuration key of the encoder the index runs. The manifest
   carries no expiry and no deployment build reference, and the binder takes
   no release census: the admission survives releases, policy edits, and
   reader or formation changes by design. It carries the owner's standing
   decision, not quality evidence measured under one configuration, so only
   the encoder tuple is hard-bound; a different encoder is a different
   decision. The composed handle is still revoked permanently when either
   file's bytes change, and restoring the bytes does not revive it. The
   binder returns the bounded marker, so composition keeps every rule the
   trial route kept: the local encoder only, interactive private owner
   queries only, no unattended reconsolidation, and the existing request and
   USD 2 daily indexing ceilings. `AGENT_MEMORY_SEMANTIC_INDEX_ENABLED=0`
   withdraws it at the next restart, as before, and ADR-0269's fallback to
   sparse recall at startup is unchanged.
2. **Preparation on the host, installation by root.** `agent memory
   standing-admission` composes the live settings exactly as startup does,
   refuses a remote profile or a profile without query preparation, and
   writes the two files from the owner's instruction; root installs them
   under `/etc/veetbot/recall/standing/` and points the existing evidence and
   manifest settings at them. No new egress, credential or route is
   introduced.
3. **Knowledge chunks are embedded in windows.** `knowledge-text@2` embeds a
   chunk as one or more windows, each the heading path over a slice of the
   chunk text, packed under the provider's token capacity with the local
   tokenizer's own count; an unbounded provider gets one window identical to
   `knowledge-text@1`. A chunk's semantic score is its best window.
   `knowledge_chunk_vectors` keys on chunk, space and window. A maintenance
   sweep embeds the chunks of the owner's current documents that have no
   vector in the active space, so documents ingested before activation gain
   vectors without a new version, and a new encoder re-embeds every document
   without an operator step.
4. **The knowledge arm turns on with its own provider.**
   `AGENT_KNOWLEDGE_SEMANTIC_PROVIDER=openai` gives the arm OpenAI's
   `text-embedding-3-small` at 1536 dimensions on the existing `openai`
   credential, the model gate 13 measured lift with, instead of the memory
   index's local encoder; `index` keeps ADR-0289's borrowing for a deployment
   whose encoder does lift. The arm then needs no semantic index and no
   admission of its own: a missing credential leaves search lexical and says
   so once. When an index is composed the arm reads its admission before
   every provider call, so withdrawing the standing files stops the arm's
   OpenAI calls with memory recall, and a restart with the index enabled but
   not admitted leaves the arm lexical too; only with the index flag off
   does the knowledge flag alone govern it. A question is free text the reader wrote from
   its context, so a remote provider embeds it only from a run pinned to
   that same provider, read from the run at search: with an OpenAI run
   nothing leaves the host that OpenAI has not seen, and a run on another
   provider, or a search outside a run, answers from the lexical arm while
   chunks are still embedded. The memory index's egress rule holds, so only public and internal
   text reaches the provider and a restricted document stays lexical; the
   passages the arm returns already reach the same provider as prompt
   context, so this adds no new reader of the owner's documents. The channel
   is `dense`, as measured. Spend is unmetered and small: a 403-chunk manual
   is about 225,000 tokens once, and the backfill embeds at most 64 chunks a
   maintenance round.

## Evidence

Gate 13's corpus, re-recorded with the local encoder at its full 256-token
window and at smaller window budgets, against the OpenAI recording. Ranks are
the answering passage's position in what `knowledge.search` returns under the
dense channel; "arm" is its position among its own document's chunks by best
window. `scripts/record_knowledge_embeddings.py --local DIR --window-tokens N`
records a budget and `--report --fixture PATH` prints it.

| Member | OpenAI, 1 window | MiniLM 256 | MiniLM 96 | MiniLM 64 | MiniLM 32 |
| --- | --- | --- | --- | --- | --- |
| Owner's manual | 2 | miss (arm 3 of 5) | miss (2 of 5) | 2 | 1 |
| Printer guide | 1 | 1 | 2 | miss (3 of 4) | miss (3 of 4) |
| Thermostat guide | 2 | miss (4 of 4) | miss (4 of 4) | miss (4 of 4) | miss (4 of 4) |

Smaller windows help the manual, whose answer is one sentence inside a long
section, and hurt the printer guide, whose other sections share the question's
words in every short window; the thermostat guide never reaches the top two
because "click" and "heating" appear in its other sections. The local
recording at the full window is kept as
`evals/fixtures/embeddings/knowledge_paraphrase.onnx.json`, and the gate
records the miss beside the lift so a change of local encoder is measured.

## Deviation from the plan

The plan admits semantic recall on passing release evidence (95% required-
evidence recall within the top twenty, a ten-point implicit answer lift) and
the bounded routes admit it for at most seven days on human review or a
supervised trial. This decision activates it on the owner's explicit standing
instruction instead, with no expiry and no quality precondition. It does not
relabel the failed comparison, complete Milestone 33, or move any gate; the
original goals and evidence stand, and the trial routes remain available.
Security requirements are not weakened: egress, visibility, erasure, correction,
interactive-only authority and the indexing ceilings are the trial's. What is
given up is the periodic owner review, which the owner judged to be stopping
progress rather than protecting it.

## Consequences

- Semantic recall and knowledge semantic search stay on across releases until
  the owner withdraws them: by the flag, or by removing or changing the
  installed files.
- A changed encoder withdraws the admission; the owner decides again.
- Knowledge chunk text of public and internal sensitivity goes to OpenAI for
  embedding; restricted documents stay lexical. Belief text still never leaves
  the host.
- Under the `index` provider with a local encoder, chunks embed in windows on
  the host's CPU; the local provider admits one request at a time, so a
  backfill round that collides with a question's embedding ends early and
  continues on the next round.
- ADR-0289's "never composes an embedding egress of its own" no longer holds
  for the `openai` provider; the measurement above is why.
- `knowledge-text@2` supersedes `knowledge-text@1`. The backfill fills chunks
  with no vector in the space; a vector carrying the older text version is
  not replaced by it. No deployment holds one, since the arm was off
  everywhere before this change; a later text version needs the older
  vectors dropped first.

## Verification

- `tests/unit/test_standing_recall_admission.py`: admitted across a year of
  clock; dispatch from `bind_index_manifest`; refusals for owner, version,
  digests, instruction and times; other dependency digests may differ; file
  mutation withdraws permanently; a remote profile is refused at composition;
  the preparation command writes both files and refuses a remote profile.
- `tests/unit/test_knowledge_windows.py`, `tests/unit/test_knowledge_embedding.py`,
  `tests/contract/test_knowledge_semantic_contract.py`,
  `tests/integration/test_knowledge_semantic_postgres.py`: windows, best-window
  scoring, the backfill sweep, and the migration.
- `tests/unit/test_knowledge_provider_composition.py`: the provider setting,
  the OpenAI route composing without the memory index, lexical fallback
  without a credential or index.
- `tests/gates/test_knowledge_semantic.py`: `test_paraphrase_lift` in the
  OpenAI space, `test_the_local_encoder_does_not_reach_the_top_two` in the
  local space, the unrelated-question floor in both.
