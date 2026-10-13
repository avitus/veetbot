# Live development pilots, October 12, 2026

These are diagnostic runs over ten fabricated development scenarios. They are
not the independent release corpus, production latency measurements, a held-out
comparison or activation evidence. No gate advances. Every run, including the
incomplete run and failed answers, is retained. `summary.json` summarizes the
unaltered per-run observations, corpus snapshot, identity and provider ledger.

All runs use 1,000 newer project-ticket distractors, a 20-item/2,000-token cap,
both automatic and deliberate belief retrieval, and the configured GPT-5.6 Sol
reader. Semantic runs use the verified local MiniLM tuple in the adjacent
`20261012-local-encoding/configuration.json`. Output fields request answers or
proposed arguments; no external action is executed. Gold labels and answer
values are supplied only to seeded formation and post-response scoring, never
to query formation, retrieval or the reader.

| Run | Supported correct answers | Recorded provider cost (USD) |
| --- | ---: | ---: |
| V1 sparse, stopped on a fixture error | 10/12 completed | 0.036920 |
| V2 sparse | 14/20 | 0.058130 |
| V2 dense | 16/20 | 0.101590 |
| V2 hybrid | 18/20 | 0.100890 |
| V2 hybrid with source recovery, seeded | 17/20 | 0.09990100 |
| V2 formation with an insufficient reservation ceiling | 4/20 | 0.09833550 |
| V3 formation@9 with source recovery | 7/20 | 0.49368675 |
| V4 formation@11, fusion coefficient 0.2 | 6/20 | 1.12621500 |
| V4 formation@11, fusion coefficient 0.8 | 8/20 | 0.67043675 |

A correct guess without all required rendered evidence is not counted as recall
success. V2 sparse guessed the breakfast correctly despite missing the dietary
constraint, illustrating why answer values alone cannot certify retrieval.

V1 stopped because the superseded-time fixture lacked its required dated
replacement. The original normalized corpus was reconstructed and its digest
verified against the recorded digest; it is preserved with the incomplete run.
The fixture was corrected before the complete V2 runs. V2 source recovery
returned the entrance detail, but the reader included opening hours and failed
the exact-value format check. V3 clarifies the requested entrance format. V4 also
accepts an explicitly authored equivalent rationale sentence. Do not compare
scores across these corpus versions as paired quality measurements.

The V2 formation run's USD 2 cap was below the conservative maximum reservation
for a formation request. Formation fell back to its deterministic extractor;
only reader calls were sent. It is a failed formation experiment even though
all observations completed. V3/V4 use a USD 5 cap, retain per-attempt completion
and error classes, actual formed records and rendered context, and expose the
formation audit. V3 explicitly selects the older formation@9 control. V4 selects
the current formation@11 People extractor. Each path/arm forms its own isolated
bank, so differences can include formation variability; these runs cannot isolate
a causal fusion-weight effect by themselves.

The live runs exposed candidate-to-rank loss for inferred provisional memories.
The subsequent regression repair adds an experimental fusion coefficient and
orders lexical/structured arms by their own relevance before final weighting;
it preserves authority, confidence and lifecycle labels. That repair has a new
implementation identity and is not measured by these earlier observations.

The runner exercises the shared belief retriever and the actual `memory.search`
tool with a controlled reader. It does not yet reproduce the complete runtime
context builder, People context expansion, planning/escalation or production
PostgreSQL latency. Formation is real, but these limitations prevent claiming a
complete production formation-to-answer comparison. Release work still requires
the independently reviewed corpus, exact final source tuple, preregistered
production budgets, full-path observations and passing gate receipts.

The v5 runtime runs use the real run loop, context builder, automatic People
service and governed memory.search tool. Deliberate access is scripted once;
the answer reader remains a live call and performs no external actions. Each
path still forms its own memory bank, so path differences are not a paired
formation ablation. Runtime results record actual trace queries/policies, People
rows and whether rendered bytes reached the reader. These runs still use
in-memory storage and fixed clocks; they do not establish production latency
or runtime-lease admission.

V5 seeded results pass 18/20 supported-answer observations at USD 0.22182750;
the two failures are unaccepted rationale paraphrases. The full formation run
retains failures in relationship formation, the sparse People ranking path,
mechanical subject/statement matching and temporal state. Its original scores
and source artifacts are preserved. A correct latest-time answer does not erase
the fact that an older, unclosed time was also rendered. The reranking experiment
introduced afterward is not represented by these earlier implementation hashes.
