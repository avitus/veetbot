# Failed replacement confirmation

This attempt failed after 4 of 4,800 required observations. It is not activation
evidence and supplies no passed quality gate. Its original source remains
`7214a8dfe0e463ebb82adeee79b7b0c188f596bb`; no record is relabeled.
CodeRabbit review 5511874464 and all eleven exact-head CircleCI pipeline 1105
checks, including ordinary signing, passed before this one authorized launch.

The first four observations cover seeded automatic sparse/dense/hybrid recall and
seeded deliberate sparse recall for `dev-attributes-01`. The following deliberate
dense run completed its two model calls, but its search returned
`memory_recall_budget_exhausted` before any candidate read. The evaluator then
raised `ValueError` because it had no observed retrieval result to certify.
The terminal record was written on October 14 at 01:44:38 UTC.

The retained scratch checkpoint reserved two automatic search slots before the
reader, despite this arm disabling automatic recall. Its deadline was
12:00:10.247013 on the synthetic evaluation clock; the reader's search plan arrived
at 12:00:12.385381 after an 11.798-second model call. That unused automatic
reservation exhausted the deliberate arm's allowance. The repair suppresses
automatic preparation together with automatic context in deliberate measurements;
the first validated deliberate call still uses the ordinary durable reservation.
Production deadlines, slot limits, refusal behavior and automatic arms are unchanged.

The delayed-reader regression first reproduces the absent-recall failure in all
three channels (3 failures, 9.64 seconds). After the repair, all 43 focused
evaluation and budget checks pass in 15.59 seconds, and the 23-test PostgreSQL
runtime/capture partition passes in 101.87 seconds, including all three delayed
channels. These deterministic checks use scripted responses, incur no provider
spend and do not replace confirmatory quality evidence.

The original journal retains eight settled provider calls, USD 0.1669775 reported
cost and no new held cost. Including the earlier USD 5.45365250, aggregate reported
and held cost is USD 5.62063000 under the approved USD 300 cap. Earlier uncertain
reservations remain charged. This attempt is never resumed; any further paid
confirmation requires a new owner decision and matching reviewed inputs.

`original-records.tar.gz` contains all ten original files: the four observation
rows, corpus, accepted independence review, baseline, preregistration, complete
input capture, identity, provider journal and failed run record. `report.json`
preserves the original retention manifest and remote archive name. The archive
SHA-256 is `fd86093bd35391a4fe2f3c2caf6a145d018885fa1e461c648a33215f9ad402c7`;
all ten member hashes were independently verified after copying. The scratch
database and its backup remain protected operator evidence, outside the repository.

The missing fifth row is not reconstructed as a successful observation. Complete
comparative evidence, independent difficult-failure review and release gates remain
outstanding.
