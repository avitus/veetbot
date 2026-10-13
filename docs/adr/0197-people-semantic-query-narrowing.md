# ADR-0197: Preserve prepared vectors through verified People query narrowing

- Status: Accepted implementation mechanism under ADR-0178; activation remains gated
- Date: 2026-10-12
- Related: ADR-0179, ADR-0181, ADR-0182, ADR-0188
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

People discovery transforms an ordinary prepared query into a query restricted to
resolved identities. The complete query digest changes even when the exact encoder
input does not. Reuse the already admitted vector only when the transformation
provably narrows that original query. This is a local derived lookup, never another
embedding call, an evidence cache or permission to broaden a search.
Direct preparation of a People-scoped query remains refused; without a live
eligible unscoped parent, retrieval retains its sparse fallback.

The original query must be current and have no People scope. The derived query must
have a People scope and identical encoder text. Tenant, principal, project scope,
temporal fields, lifecycle flags, exclusions, belief filters, ranking policy and
all other fields remain exact. Explicit included IDs can only shrink; sensitivity,
item and token ceilings can only decrease. People-linked expansion IDs remain soft
relevance signals under those unchanged hard restrictions. Clearing redundant
subject context is allowed only when it leaves the actual encoder bytes identical.
Subjects may only be removed, since adding one can admit a local-portability
original from another project even when the subject already occurs in the text.

The cache retains the original validated query alongside its vector under the
existing shared 128-entry bound. Derivation searches only the same live context,
uses the original expiry, does not create another entry and returns the derived
query's complete digest. Admission withdrawal, changed authority, checkpoint,
lease, task, scope or model configuration retain their existing revocation rules.
An unprovable transformation remains a sparse fallback.

Every original is rejoined under current hard predicates at use time. People
identity filtering precedes both sparse and dense candidate limits in both stores, including
hidden and unresolved assignments. People discovery still validates its focal
identities and shares the normal run search budget. The final identity filter
remains as defense in depth. Explicit scoped lookup also preserves a caller's
included-ID restriction rather than replacing it with all of a person's links.
Composition binds each memory store to the same unit of work's People store;
this closes the existing dependency cycle without constructing a second identity
bank. An unbound current scoped query fails closed. The memory store's shared
current predicates then govern both retrieval channels.

This does not admit historical vectors, source expansion for People, different
encoder input, cross-person reuse from an already scoped query, broader read
clearance or any new production channel. Exact release evidence remains required.

## Verification

Shared contracts cover relevant scoped results behind stronger unrelated vectors,
restricted/unresolved links, and all disallowed query transformations. Runtime
contracts must show actual People discovery reuses one prepared vector without a
provider call inside retrieval, while expired and revoked parents remain unusable.
These contracts are mechanism evidence, not a held-out answer-lift claim.
