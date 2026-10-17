# ADR-0199: Preserve recall evidence across identical release inputs

- Status: Accepted by the owner; binding implemented, confirmatory execution remains pending
- Date: 2026-10-12
- Related: ADR-0178, ADR-0185, ADR-0187
- Scope: M33 recall evidence only; M16/M32 evidence remains unchanged

## Problem

ADR-0185 binds measurements to a full source commit. ADR-0187's runtime binder
also requires that commit to match the deployed release identifier. The existing
`test_manifest_refuses_changed_active_dependencies[release]` verifies this refusal.
A normal GitHub merge has a different commit identity even when its evaluated
inputs are identical to the reviewed source branch. Under the current contract,
source-branch evidence cannot activate that merged release. Simply replacing the
identity on old observations would misrepresent their provenance.

This matters before the full confirmatory run: the approved workflow verifies the
implementation before the dev-to-main pull request, reviews every code-changing
push, and then verifies the merged production release. An identity-only difference
should be distinguishable from a change to any evaluated behavior. The existing
source implementation digest alone is insufficient to make that distinction:
it does not cover every migration or deployment input that can affect execution.

## Decision

Preserve the measurement identity, source commit and original observations exactly.
Add a separate, versioned deployment binding identifying the measured artifact and
the actual deployed commit. Accept that binding only when the trusted release
process independently verifies equality of the complete evaluated input manifest.
A branch name, claimed ancestor relationship, copied digest or passing aggregate
score cannot establish equivalence.

The manifest must bind the implementation and scorer; corpus, labels and frozen
controls; effective principal authority, policies, profiles and context/work
budgets; resolved formation and reader configurations; embedding bytes and tuple;
index/schema state; dependency locks; and migration, deployment and packaging
inputs affecting memory execution. Its path set and exclusions are versioned and
reviewed. Missing inputs, symlinks, unknown exclusions and changed files refuse
binding. An unchanged source-code digest with a changed migration or dependency
must fail. Pure documentation and generated evidence outputs may be excluded only
by the reviewed manifest contract, never by a caller-supplied glob.

The production binder still recomputes active inputs and all quality gates. The
original observation and review bytes remain immutable. The new deployment receipt
cannot manufacture a corpus review, repair a failed quality result, admit stale
index state, renew work budgets or enable an optional channel by itself. Observed
replacement, removal or mismatch permanently revokes the binding. Required CI and
CodeRabbit review still apply to the final code, and application/documentation
release checks still identify the actual merge commit.

A change to any evaluated input requires new matching measurements and review.
This decision changes only how an identical evaluated input set is connected to
a later release identity; it does not alter numerical quality, privacy, budget,
review or deployment requirements.

## Required verification

- An independently constructed, identical input manifest permits a new deployment
  commit while preserving every original measurement and review digest.
- Changes to source, scorer, authority, model, embeddings, schema/migrations,
  dependencies, packaging, budgets or corpus refuse reuse before provider or
  index work. Missing or forged deployment bindings also fail.
- A changed release identifier without the new verified binding still fails the
  existing regression. The old behavior is not silently relaxed.
- A changed receipt, active input or evidence file withdraws admission, including
  after restoring earlier bytes. No M16/M32 artifact or verifier changes.
- The confirmatory runner captures actual effective inputs before reading holdout
  tasks or creating providers. Publication retains those original inputs and
  independently checks the deployment binding instead of retagging observations.

## Alternative considered

Keep exact-commit admission and run matching confirmation again against the actual
merged revision before activation. This preserves the existing rule but adds a
full evaluation after an otherwise identical merge; source-branch results alone
remain insufficient. The owner explicitly approved verified input binding on 2026-10-12. The strict
exact-commit rule remains the fallback when no valid binding is installed.


## Implementation

An optional `evaluated_inputs_sha256` in the original measurement identity commits
its versioned input manifest before measurement. It is omitted when absent, preserving
legacy identity serialization. Existing observations cannot acquire this field by
retagging: their original identity hashes would no longer match.

`recall-evaluated-inputs@1` binds every non-excluded repository file's SHA-256 and
executable bit, plus every identity dependency except the source commit and the
input commitment itself. The fixed exclusions are documentation (`docs`, `archive`,
`superpowers`, root `AGENTS.md`, `CLAUDE.md`, `README.md`, `docs-manifest.yaml` and
`mkdocs.yml`); generated evidence (`evals/observations`); build and test outputs
(`site`, `dist`, `build`, `test-results`, `website/out`); local tooling (`.git`, `.chunk`, `.venv`,
`venv`, `.agent`); Python, Node and Apple caches (`__pycache__`, `.ruff_cache`,
`.mypy_cache`, `.pytest_cache`, `.hypothesis`, `node_modules`, `.next`, `.build`,
`DerivedData`, `xcuserdata`, `.egg-info` directories and `.pyc`/`.pyo` files);
`.DS_Store`; and root `.env`/`.release.env`/`.owner-scopes.env` files. The release
script generates the owner-scope file, and hosted verification generates the
website output; neither belongs to the measured Git tree. Effective configuration,
including the reconciled owner scopes, remains
bound through the existing independently resolved composition digest; credential
bytes are never included. No caller may add exclusions. New paths are inputs by
default. Included symlinks and non-regular files fail closed.

The trusted offline command
`uv run python -m agent_core.evals.general_recall_release_binding --evidence PATH --deployment-build-ref FULL_SHA --output NEW_PATH`
independently reads both committed Git trees, checks the original input commitment,
requires a clean deployed checkout and matching local bytes, recomputes the quality
gates, then writes a new `recall-deployment-binding@1` receipt. It never changes the
published evidence. The receipt is supplied through the existing semantic manifest
setting; an ordinary identity file still requires its original commit.

Production independently inventories and hashes the current input files and compares
them with the commitment, receipt and original evidence before creating providers.
Every admission callback checks the complete path census and file device/inode,
mode, size, modification and change timestamps, as well as evidence and receipt
bytes. An observed mutation revokes admission permanently, even if restored. This
relies on the existing trusted host and release process; filesystem metadata and
hashes are not authentication of an operator or reviewer.

Focused regressions exercise a real pair of Git commits differing only in excluded
documentation; changed migrations, source, scorer, locks, deployment, corpus, new
files, missing files, modes, symlinks and forged/unknown receipts; immutable original
evidence; and latched withdrawal. Publication additionally rechecks the input
commitment before and after compiling original observations. Fabricated unit bundles
supply no review or release evidence. The confirmatory runner must still capture
actual effective inputs before provider work or holdout task execution. No optional
channel is activated and no M33 gate is marked complete by this change.

Bootstrap now exposes the same effective configuration, formation selection and
reader catalog that it supplies to production admission, including when indexing
is inactive. Its explicit model-metadata capture mode preserves configured model
availability without constructing external model adapters; those descriptors
refuse execution. Shared tests compare the normal and captured compositions and
verify that activation receives the captured values. The confirmatory runner must
still bind these values to the corpus, frozen controls, actual index state and
each measurement before it can produce release evidence.

The fixture driver now accepts an independently selected principal throughout
history formation, index work, query authority, search and observed-result scoring.
Its default synthetic principal remains unchanged. A foreign fixture account can
never alias that selected account. Before source histories, indexing or reader
calls, an optional validation callback receives the actual composition, retrieval,
embedding, reader and experiment settings. Refusal stops measured provider work;
each successful row records the digest of those inputs. This is the runtime hook
for confirmation, not a complete confirmatory collection or an independence review.

The optional `execution_manifest_sha256` commits all twelve captured runtime variants
and the independently selected principal before execution. It is omitted when absent
to preserve legacy identity bytes. The fixture driver validates the actual composition
before formation, indexing or reader calls, and again before returning an observation.
It records the original release identity and capture digest on the raw observation.
Nested configuration mutation also refuses execution; a frozen outer model alone does
not establish immutable input values.

`recall-collection@2` retains that original capture as a hashed artifact. Collection
checks the complete runtime matrix, authority, capture time, preregistered aggregate
spend ceiling and every raw observation's original identity/configuration before
scoring. Attribution uses the independently captured principal. Current publication
requires this confirmed collection; a legacy development collection remains readable
for diagnosis but cannot become release evidence by assigning a new identity later.
Regressions first demonstrated that retagging and unconfirmed publication succeeded,
then verified refusal alongside valid fabricated-matrix collection. These fixtures
are not release observations. The outer confirmatory runner must still independently
capture the deployment graph, model assets, actual index/schema and frozen controls
before loading held-out tasks or constructing live providers.

The runtime preflight now captures all twelve configurations with a fixed development
probe unrelated to the corpus. It does not open the corpus, load encoder assets or
construct external provider adapters. Non-callable model and encoder descriptors
refuse any unexpected work, and capture stops before histories are formed. It uses
the same explicitly authorized disposable PostgreSQL composition as measurement;
it must never be used to inspect a production database. This closes per-variant
capture, while the outer runner's independent deployment, reviewed corpus, verified
assets, schema and frozen-control checks remain required.

The outer `agent_core.evals.general_recall_confirmatory` runner now implements
those checks. It requires an accepted independent corpus review and a clean
committed build before opening held-out tasks. It captures the intended deployment
graph through bootstrap's model-metadata callback, redirecting writes to a separately
named, explicitly authorized scratch database and temporary artifact directory.
Returning from the callback is an error; capture cannot expose an executable runtime.
The same bounded file reader verifies encoder and tokenizer bytes without importing
the native inference libraries. Normal inference independently verifies them again.

PostgreSQL capture reads schema definitions in one repeatable-read, read-only
transaction. It records the actual revision, server/extension versions, relations,
columns, constraints, index definitions and validity, RLS policies, triggers and
functions. It excludes stored memory rows, physical identifiers, statistics and
sequence counters. Production semantic startup and evaluation both bind this
fingerprint in their effective configuration; a revision label alone is insufficient.
The intended migrated scratch schema is evaluated before delivery, and production
recomputes its own schema identity when semantic indexing is composed.

The runner freezes all twelve actual runtime variants, checks the hybrid retrieval,
embedding, reader, formation and context ceiling against the intended deployment,
and records preregistration before parsing the reviewed corpus. A fixed inventory
preserves M16/M32 controls and retained reconsolidation decisions. Deployment, schema
and controls are checked again before provider construction and after measurement.
Repository inputs are checked throughout execution. Original review and baseline
bytes, original identities, per-scenario raw observations, shared formation banks
and the settled/uncertain cost journal are retained in a new output directory.
An interruption retains the failed attempt; it cannot overwrite an earlier attempt.

On 2026-10-12 the owner authorized a **USD 100 aggregate model-spend cap** for the
4,800-observation confirmation. One budget and reservation journal cover the complete
matrix; individual variants do not receive separate allowances. On 2026-10-13,
after the interrupted first confirmation and a measured USD 231 projection, the
owner explicitly raised the aggregate authorization to **USD 300**, including all
prior reported spending and uncertain reservations. The runner keeps its existing
USD 100 default, accepts an explicitly configured allowance up to USD 300 and
refuses higher values. The one authorized replacement is allocated USD 290; adding
USD 5.45365250 from prior attempts leaves USD 4.54634750 unallocated. It may start
only after the final evaluated inputs pass CodeRabbit review and exact-head CI.
The stopped attempt remains immutable, and no quality gate or input-binding rule
is relaxed. Its CLI is
`uv run python -m agent_core.evals.general_recall_confirmatory --configuration PATH`.
It requires `RUN_LIVE_MODEL_TESTS=1`, a separate `M33_SCRATCH_DATABASE_URL` named
`veetbot_m33_eval_<name>`, and explicit scratch authorization in the configuration.
The configuration names the reviewed corpus, review, baseline, committed build,
numerical ceilings and measured runtime options. Intended deployment settings and
the explicit local index overlay remain independently resolved inputs.

These are mechanism checks, not quality measurements or human decisions. The
400-scenario corpus received the owner's explicit approval on 2026-10-13; the
accepted receipt binds its unchanged corpus and scenario-manifest digests.
The first paid confirmation was interrupted after 34 of 4,800 observations when a
required integration-fixture correction changed the bound test inputs. Its original
records and failed status are retained under
`evals/observations/general-memory-recall/20261013-confirmation-v1/`. The separately
authorized replacement later failed at 4 of 4,800 observations on reviewed and
CI-verified `7214a8df`; its originals remain under `20261014-confirmation-v2/`.
An evaluation-isolation defect reserved automatic work even when the deliberate
arm disabled automatic context. A delayed model response expired that reservation
before its search. Deliberate measurement now suppresses automatic preparation
as well; validated tool calls retain all production budget checks. The unchanged
source cannot certify that repair. Aggregate reported and held cost is
USD 5.62063000; another paid attempt requires a new owner decision. All twelve
M33 gates remain pending.
Completion of a raw collection
does not publish evidence or activate a channel; independent failure review,
mechanical scoring, all gate reports, CodeRabbit, hosted CI and delivery remain required.

Confirmation requires the production model router, provider pin, chat reasoning
effort and reasoning-summary setting. The earlier diagnostic driver disabled those
services; its reader identity could therefore never match a normally pinned
production chat. The confirmatory mode keeps them active, including for seeded
cases, and wraps the selected provider only to request a mechanically scored
answer. Its configured reader digest must match the captured catalog/router tuple,
and the actual run checkpoint must report that digest after execution. Both recall
paths have scripted-provider regressions through the real runtime; no external
provider work or quality measurement is supplied by those tests. Historical
diagnostic reproduction continues to select its explicit unpinned mode.
Confirmation-manifest validation itself rejects that diagnostic mode, including
when a collection is supplied directly to the offline publisher.

Production configuration inspection on 2026-10-13 found Astra/high for chat and
Sol for memory formation. The PostgreSQL evaluator now permits distinct model
policies when both resolve through its one budgeted provider. It still refuses
a formation provider that would escape that shared allowance. The integration
regression first failed on the former same-policy restriction and now captures
all twelve runtime variants without executing a model call.
Unsettled provider calls also retain their exception class, without exception
text or request contents. Their reservations remain held and the attempt remains
ineligible for publication; diagnosis never turns uncertain usage into zero cost.

The explicit-cap regression first rejected the newly approved USD 290 allocation
with a validation error under the old USD 100 maximum. The revised boundary permits
explicit USD 290/300 allowances, retains the existing USD 100 default and refuses
USD 301. The actual replacement allocation must still subtract all prior charges;
a maximum accepted configuration is not authority to spend it again per attempt.
