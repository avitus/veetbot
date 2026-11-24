# ADR-0349: Pack more ranked facts within the turn budget

- Status: Proposed on a local experiment branch.
- Date: 2026-11-24
- Amends: ADR-0309 and ADR-0310 for an automatic turn with no retained deliberate result.
- Detailed design: [Context assembly](../plan/context-engine.md#freshness-of-in-turn-recall).

## Context

On the November 24 production copy, all 114 automatic question runs filled
twenty slots. The median block used 875 of 2,000 tokens. Five undelivered
required facts ranked between 20 and 43. The owner chose to measure the item
cutoff using the existing questions and scoring criteria.

## Decision under test

Double the item ceiling once, to forty, for a fresh automatic relevance query
with the full turn allowance. The additional twenty slots are available only to
already-ranked beliefs. Each candidate still has to fit whole in the remaining
tokens. This tests a single general rule across the set.

Context assembly assigns the extra slots before semantic preparation and records
them on the query. Only current search pins that reserve automatic evidence can
use them. A retained search block keeps the original shared limits. The
retriever uses the original allocation for candidate reads, identity discovery,
People reservations, attention-flagged beliefs and summary slots. Overview pages
and snapshot deltas receive no additional capacity. Visibility and sensitivity
checks run before packing. A trace written before this experiment has zero extra
slots.

## Measurement

Both scripted runs use the same starting database, local encoder and version-two
scoring rule. The copy contained 113 active questions. The saved held-out file
supplied the remaining question on the copy. All 114 scoring fingerprints match
the November 23 report. The baseline reproduced 103 passes across the 58
development and 56 held-out questions. These measurements concern delivered
evidence. Answer quality and milestone acceptance use their own evaluations.

| Result on the same copy | Twenty slots | Up to forty slots |
| --- | ---: | ---: |
| Development questions passed | 54 of 58 | 55 of 58 |
| Held-out questions passed | 49 of 56 | 52 of 56 |
| All questions passed | 103 of 114 | 107 of 114 |
| Required facts delivered on each path | 115 of 125 | 119 of 125 |
| Questions receiving forbidden evidence | 1 | 1 |
| Median automatic block size in items | 20 | 40 |
| Median automatic block size in tokens | 875 | 1,645 |

All four gains occurred on both paths. Every previously passing question passed
again. The same question received forbidden evidence in both runs. Of the
remaining six missing required facts, five reached ranks 43, 56, 72, 137 and 164,
and one stopped before candidate selection. The largest automatic block used
2,000 tokens. The local median recall time was 127 ms on each path before and
139 ms on each path after. These are observations from the quality experiment.

Semantic search ran on all 228 paths in each run. The production copy was
deleted. The proposal awaits the owner's decision before submission.
