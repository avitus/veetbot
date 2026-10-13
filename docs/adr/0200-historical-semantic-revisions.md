# ADR-0200: Semantic retrieval of governed historical revisions

- Status: Implemented mechanism under ADR-0178; activation remains evidence-gated
- Date: 2026-10-12
- Related: ADR-0179, ADR-0180, ADR-0181, ADR-0197

## Decision

Extend the existing derived index with an explicitly enabled local historical arm.
Current vectors remain distinct and retain their current-only contract. Historical
vectors identify the original content revision and the current original's source
version at capture. Correction, restriction, attribution change and erasure delete
all dependent vectors in the same transaction. A late write must match both the
historical original and the current source fence; it cannot restore invalidated
content. Rebuilding a permitted historical vector never makes it current evidence.

Use the existing owner source-change sequence for finite historical inventory.
It already identifies immutable original content versions, is ordered independently
of recency ranking, and advances over deleted/ineligible originals. Historical
indexing uses that sequence as its full-sweep cursor under an explicit configuration
identity; ordinary indexing retains its existing creation cursor. A completed sweep
restarts when its bound changes or on the next day under the existing job lifecycle.
Each slice keeps the existing bounded page, byte, request, retry, time and spend
limits. Historical source text stays inside the verified local encoder; this does
not authorize remote historical export or automatic historical re-extraction.

For `known_at`, choose the authoritative revision before similarity or candidate
limits. For `as_of`, apply the existing valid-time semantics to authoritative
originals. Both paths retain current erasure, principal, sensitivity and portability
restrictions and the selected revision's sensitivity, portability, validity,
lifecycle, type and explicit-exclusion restrictions. Dense selection must share
these predicates with sparse selection. Source revisions remain authoritative;
embedding rows provide relevance only. People visibility must remain a hard
predicate before result limits and needs dedicated historical parity checks.

The query cache binds both temporal coordinates as part of the complete query.
Historical query preparation is enabled only with the explicitly admitted local
historical configuration. Unavailable vectors retain the traced sparse fallback;
evaluation cannot relabel that fallback as a dense observation. All final context,
trust, provenance and correction checks continue to apply.

## Verification required

Shared in-memory and PostgreSQL cases must retrieve an older semantic match after
an update, distinguish valid time from knowledge time, reject current use of old
content, and enforce current and historical restrictions before candidate limits.
Current-only indexing contracts remain unchanged. Tests cover stale writes after
correction/erasure, rollback, finite inventory, same-time revisions, query-cache
isolation and bounded local background indexing. Historical People parity and the
complete formation-to-answer evaluation remain necessary before activation.

## Implementation and verification

Migration `ea3310a1b005` adds a historical revision coordinate to the derived vector
primary key. Zero preserves the current slot. Historical slots retain the selected
content revision and the current original's invalidation fence. Downgrade removes
only rebuildable historical vectors and restores the previous key. Both stores
share the historical selection and visibility contracts; People identity filtering
now precedes historical sparse and dense limits as well as current limits.

The explicitly enabled local worker uses the existing source-change inventory and
durable work bounds. The planned `memory.search@2.0.0` accepts `known_at` separately
from `as_of`; M33 rendering labels both clocks. Legacy tool and rendering contracts
remain unchanged. Development experiment options record historical indexing as a
separate default-off configuration, without relabeling any earlier observation.

The first regression failed because historical capture returned no source. Further
regressions exposed dated People candidate starvation and an unlabeled knowledge
time. Shared in-memory contracts now cover those repairs, current/past separation,
same-time revisions, stale writes, erasure, pre-limit visibility, finite inventory,
explicit local indexing and query-cache time isolation. PostgreSQL verification
passed twenty checks in 68.09 seconds, including the shared historical contracts,
an actual worker/tool/reader-context case, metadata equality and migration round
trips. The earlier current-index and historical subset passed eighteen checks in
31.19 seconds. These measured checks establish mechanics, not comparative quality.

No gate is complete and no channel is activated by these implementation checks.

The full repository suite also exposed two erasure integrations that still unpacked
the old two-field history entry. Email and session erasure now retain complete source
version entries when filtering unrelated history. The focused erasure suite passes
thirty-five checks, including the shared email-source contract and preservation of an
unrelated session's historical semantic source. Neither erasure path discards retained
source-version metadata.
