# ADR-0187: Semantic index production composition

- Status: Accepted implementation mechanism under ADR-0178; activation remains gated
- Date: 2026-10-11
- Related: ADR-0180, ADR-0182, ADR-0185
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

Connect bounded source indexing to the maintenance role behind the default-off
`AGENT_MEMORY_SEMANTIC_INDEX_ENABLED` deployment flag. This prepares the derived
index; it does not activate semantic recall or authorize query egress. Production
processing requires PostgreSQL so leases, cursors and conservative spending survive
restarts. Ordinary context assembly and `memory.search` schedule no work.

The reviewed `memory/semantic-index.yaml` catalog ships empty. An operator overlay
must supply exactly one complete configuration: an explicit credential reference,
embedding provider/model/revision/dimensions, price and pricing revision, egress
revision, residency permission and request/day ceilings. Nothing selects a model,
price or credential implicitly. The first supported transport is OpenAI; the local
egress policy revision and explicit residency provider must match. Interpolation,
unknown fields, multiple configurations and missing values are rejected. This
optional catalog adds no populated tuning default to the existing knob inventory.

`AGENT_MEMORY_SEMANTIC_INDEX_MANIFEST` identifies a separately reviewed
`RecallReleaseIdentity` JSON artifact. `AGENT_MEMORY_SEMANTIC_INDEX_EVIDENCE`
identifies the measured `general-recall-release@1` bundle. Composition independently
hashes the effective policy, agent, principal/scopes, context and memory profiles,
storage, People/dreaming modes and withheld query authority; the complete embedding
configuration; and the formation/reader policies, upstream formation identity,
model registry including resolved limits/prices, and offered model choices. These
values must match the manifest before the existing release verifier recomputes the
bundle's complete quality and boundary gates. The manifest's full revision must
match the deployed release suffix, and the verifier also checks local source and
scorer bytes. No credential is resolved or provider constructed before admission.

The remaining corpus, reviewed-independence, preregistration, index-experiment and
frozen-control digests come from the independent reviewed manifest. As ADR-0185
requires, the operator trusts that review and the harness's honest measurement;
this composition does not prove those artifacts' provenance. A manifest copied
from an unreviewed result is not valid release evidence. The evaluated index
manifest describes the experiment, not a hash of the changing production bank.
The reader/formation catalog binding does not certify a later query's selected
model or content provenance; query activation needs its own run-bound resolution.

Both files are bounded regular, non-symlink files with duplicate JSON fields
rejected. Observed removal, replacement or unreadability of either permanently
withdraws admission; restoring bytes requires explicit revalidation. Configuration
is fixed for the process lifetime and changes require recomposition and matching
new evidence. The flag off performs no evidence read or credential resolution.

Maintenance starts one indexing task only when its durable live-lease check is
empty. Repeated polls cannot start an overlapping task. It remains independent
of ordinary sweeps and the dreaming task. The existing slice bounds its work to
sixty seconds and failure cleanup to five seconds; maintenance adds a seventy-second
outer limit. Shutdown cancels and joins the task. Repeated stop requests do not
interrupt cleanup that is already settling an uncertain reservation. Exceptions
log their type without source or provider prose. Existing owner/day budgets,
source sensitivity/egress checks, source-version fences and fair inventory persist.
A foreground run that starts after admission is not preempted or cancelled; the
already bounded index slice can finish alongside it.

## Verification and remaining work

Behavioral red tests reproduced ignored activation, absent background dispatch,
missing manifest matching, and repeated shutdown interrupting cleanup. Tests cover
explicit configuration, real release-verifier binding using fabricated metric
fixtures, dependency mismatch, credential ordering, source sensitivity, withdrawal,
single-task dispatch, foreground lease deferral, exception isolation and bootstrap
wiring. A PostgreSQL integration checks the same lease and source boundaries.
Fabricated unit evidence is never published or installed as activation evidence.

Trusted query sensitivity/surface provenance, query preparation scheduling, semantic
recall composition, historical/People parity, planning/recovery and independent
held-out formation/answer-use evidence remain open. No M33 gate moves. No provider
is activated in a running deployment, and this is not completion of the memory fix.
