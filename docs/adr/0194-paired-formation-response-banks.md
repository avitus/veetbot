# ADR-0194: Paired formation response banks

- Status: Accepted for experimental evaluation; activation remains gated
- Date: 2026-10-12
- Related: ADR-0178, ADR-0185, ADR-0192
- Detailed design: `docs/plan/general-memory-recall.md`

## Context

The first development pilots formed each history again for each retrieval path.
Even with the same model and zero temperature, those calls can produce different
facts. A retrieval difference then mixes formation variability with search quality.
The approved comparisons require identical inputs and separately measured stages.

## Decision

An optional formation bank records one history's actual structured provider
completions. Later retrieval arms rerun the complete formation pipeline against
the same history and replay those completions, without another provider send.
The bank binds the fixture, formation policy and profile, persistence mode and
implementation digest. Every ordered request also binds its complete request and
resolved model, excluding only resolution time and the credential reference.
Changed, missing or additional requests refuse replay; there is no live fallback.

Before retrieval, compare the resulting complete current belief records and bounded
People inventory, including source attribution and superseded records. Absolute
memory store positions may differ after a scratch reset; the complete relative
order and all other belief fields remain bound. A changed state refuses the arm.
This check establishes paired current inputs, not historical-vector parity or a
claim that the provider formed every expected fact.

Banks retain ordinary assistant output and usage, never reasoning items, opaque
provider continuation or credentials. One bank permits at most 128 formation
calls and eight MiB. A pending journal records reservations and completions; an
interrupted recording cannot be overwritten. Completed banks are exclusive files
with private permissions. Unsupported completions and failed recordings remain
ineligible for replay. No label or expected answer enters formation requests.

Each observation identifies its bank digest and replayed-call count. Attributed
formation-plus-reader tokens and cost remain comparable between arms. Separate
actual-call and actual-cost fields report new provider work; the experiment's
budget journal counts only actual sends. A replay is never reported as a fresh
live provider measurement. Retrieval, context assembly and the reader still execute
for each arm through the actual runtime.

A matrix uses one provider reservation ledger across all arms. An arm cannot renew
the monetary allowance, hide uncertain usage or compare different source corpora,
formation banks or loads. Its metadata reports actual cost deltas per arm and the
total spent and held by the matrix. This preserves worst-case request reservations
without mistaking an individual arm's small expected cost for sufficient admission.

## Evidence and limits

Tests first reproduced repeated formation sends and accepted changed requests.
Replay tests require identical structured output, ordered inputs and resulting
state, and reject changed identities, extra/missing calls and changed state.
The paired PostgreSQL regression exercises the normal People formation composition
and reduces the second path to its real reader call. Reasoning retention and
incomplete-recording refusal have separate coverage.

The development runner exposes `formation_bank_directory`; banks can be shared
across separately configured arms only when all their formation identities match.
Failed attempts and earlier unpaired pilots remain recorded. Human corpus review,
live comparative measurements, all release receipts and publication remain open.
No production formation policy or activation threshold changes.
