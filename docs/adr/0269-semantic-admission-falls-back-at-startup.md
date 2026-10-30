# ADR-0269: Semantic admission falls back to sparse recall at startup

- Status: Accepted (explicit owner direction on 2026-10-29)
- Date: 2026-10-29
- Related: ADR-0187, ADR-0188, ADR-0218, ADR-0223, ADR-0246
- Detailed design: `docs/plan/bootstrap-and-composition.md`

## Context

The optional semantic admission is bound to exact production inputs. Rejecting
that admission as an application startup failure couples ordinary app updates
to a recall trial. The owner explicitly requested that updates continue with
automatic sparse fallback, while preserving the ability to restore admitted
live semantic retrieval.

## Decision

Runtime bootstrap catches configuration rejection from the optional semantic
composition only. It logs the fixed event `semantic_recall_unavailable_using_sparse`
without exception prose, paths, credentials or memory contents, then completes
the ordinary application graph. No semantic services, query preparation, source
recovery or maintenance indexing callback are installed in that graph. Sparse
recall and ordinary maintenance remain available. The runtime retriever uses
the sparse profile, retaining owned-resource and home-location anchors even
when the configured profile requests semantics. The configured profile remains
unchanged in the evidence identity; valid semantic composition retains its
existing anchor normalization. Unrelated application
configuration errors and unexpected programming errors still propagate.

The lower-level semantic composer and admission validators remain strict.
Missing, expired, withdrawn, incompatible or malformed admission does not grant
semantic authority. Rejected admission is checked before any embedding provider,
local model or credential access. An unavailable configured local model also
leaves runtime startup on sparse recall. Valid admission uses the existing
semantic graph and its revocation checks unchanged.

This does not hot-reload evidence, renew a trial, restore withdrawn manifests,
or transfer approval across releases. Restoring semantic use requires current
valid evidence and recomposition. App health means the app can serve requests;
it is not proof of semantic activation. Operators must inspect semantic service
admission independently. Core schema readiness and authentication checks remain
mandatory, and unsupported database migrations may still block startup.

## Verification

Composition regressions exercise missing dependencies and rejected evidence,
provider non-construction, ordinary maintenance and actual sparse retrieval.
Existing strict validator, admitted indexing and local query composition tests
remain required. These fixtures establish failure isolation, not production
answer quality or new activation evidence. No M33 acceptance gate moves.

Local model factories report expected asset/tokenizer/native-load unavailability
as `EmbeddingError`, which the semantic composer converts to `ConfigurationError`.
The native adapter catches only deserialization and native model-load failures;
unexpected Python implementation errors propagate through composition and startup.
Regression coverage exercises both the actual adapter initialization and the
factory path through application bootstrap, not only a substituted composer.
