---
title: Changelog
---

# Changelog

## 2026-08-18 — Configurable CLI run wait

- Added `agent run --wait-timeout <seconds>` with a positive-value boundary
  and a 300-second default, while preserving the durable run identifier and
  exit code 5 when the local wait expires.
- Kept the setting local to CLI submission; the asynchronous HTTP API and
  native Apple client behavior are unchanged.

## 2026-08-18 — Provider-neutral public-web access

- Added stable `web.search` and `web.fetch` tools with Tavily and Firecrawl
  adapters behind one provider-neutral port.
- Selected capability-level routing, recommending Tavily for discovery and
  Firecrawl for clean page extraction while allowing either provider for either
  tool.
- Kept web access default-off, brokered provider credentials at call time,
  bounded provider responses, rejected non-public fetch targets, and preserved
  external-untrusted provenance through the complete tool pipeline.

## 2026-08-17 — Complete historical transcripts in the Apple client

- Added a principal-scoped, paginated session-message read that exposes only
  durable user and completed-assistant messages from the authoritative event
  log.
- Restored every historical turn before the Apple client attaches to the latest
  run, with persisted-sequence deduplication so SSE replay cannot duplicate the
  final turn.
- Covered pagination, transient read retry, malformed cursors, scope and
  principal isolation, and the relaunch regression, and proposed ADR-0053 for
  the seventeenth-route extension.

## 2026-08-16 — Explicit memory writes after recall

- Preserved the runtime's `MEMORY` provenance through `memory.remember` instead
  of replacing it with the default untrusted argument label. Explicit writes
  now succeed after a memory snapshot or recall even when the model normalizes
  the user's wording, while external and knowledge-derived turns remain
  rejected.
- Added focused and PostgreSQL-backed regressions for the complete
  recall-then-remember path, including short normalized statements.

## 2026-08-15 — Documentation-derived regression coverage

- Made red-green-refactor evidence part of the repository operating contract,
  including a prohibition on weakening failing tests and boundary-level
  coverage requirements for public behavior.
- Added a required full-Xcode Apple test partition to local tooling and hosted
  CI. Release packaging now waits for native tests instead of accepting a
  Command Line Tools build that compiles Swift Testing bundles without running
  them.
- Added native client contract coverage for every typed HTTP operation,
  security-sensitive connection validation, missing credentials, conditional
  artifact reads, stable message retries, and unbounded loop-safe pagination.
- Added a PostgreSQL-backed conversation journey covering activity ordering,
  principal isolation, active-run deletion refusal, cancellation, deletion,
  tombstone idempotency, and converged history.
- Fixed future activity timestamps rendering as `in 0s`, normalized seeded
  in-memory credentials, and removed the approval-list 20-page truncation while
  rejecting repeated cursors.

## 2026-08-14 — Cross-device conversation titles

- Moved generated conversation titles into the authoritative shared core so
  they survive a client reinstall or move to another machine.
- Derived titles for older null-title sessions from their immutable first user
  message, avoiding an event rewrite or a one-off data migration.
- Kept title assignment first-writer-wins, principal-scoped, whitespace-
  normalized, and capped at 64 characters across the API and terminal paths.

## 2026-08-14 — Authoritative conversation history and deletion

- Added a principal-scoped, paginated server session index with latest-run
  identity and activity ordering so client history is a mirror rather than a
  device-local source of truth.
- Made idempotent `Delete Everywhere` the default destructive action. It rejects
  active work, purges the durable conversation graph, retains only a
  content-free ownership tombstone, and retries external artifact-byte deletion
  through maintenance.
- Updated the Apple client to reconcile history on connect, foreground entry,
  and a periodic poll, and to remove local state only after authoritative
  deletion succeeds.
- Reported a specific server-upgrade error when a client reaches a deployment
  that predates the history routes, instead of surfacing the generic unsupported
  HTTP-request response.
- Added a post-promotion authenticated session-index probe so a server release
  cannot pass deployment verification while omitting the API used by the Apple
  client.
- Fenced in-flight and background reconciliation so it cannot restore a session
  after Delete Everywhere succeeds, and stopped polling while the app is not
  active.
- Hardened session cursors, kept activity timestamps monotonic, aligned the
  deterministic deletion and run-index adapters with PostgreSQL, and made
  connection settings report the result of the current save attempt.
- Removed the Apple history page cap and rejected cursor loops, tightened
  release-probe URL and status validation, and gave memory origin-trust
  rejections their own stable tool reason code.
- Required initial history compatibility and authentication checks to succeed
  before connection settings report success or dismiss their form.
- Made initial reconciliation fail closed for every error, streamed server pages
  into the local history cache, and bounded parallel verification of locally
  missing sessions.
- Removed a server-confirmed deletion from the visible Apple history immediately
  even when persistent device-cache cleanup subsequently reports an error.
- Proposed ADR-0050 for the two-route post-Milestone 9 extension and its
  deletion, synchronization, and artifact-lifecycle boundaries.

## 2026-08-10 — Atomic production delivery

- Adapted Mankunku's timestamped immutable-release, deployment-lock, exact
  public revision check, bounded retention, and safe Nginx reload patterns to
  Veetbot's `uv`, Alembic, systemd, PostgreSQL, and gVisor topology.
- Added post-gate CircleCI packaging and `main` deployment jobs while preserving
  the existing verification partitions and single configuration file.
- Added `X-Veetbot-Release` to both health responses, production checks for the
  release identity and default provider credential, and isolated shell tests for
  application and proxy deployments.
- Expanded committed-secret scanning to the deployment and CI surfaces and
  proposed ADR-0048 for the privileged delivery boundary.
- Bound the production API to loopback behind Nginx, required a dedicated
  Veetbot deploy key, serialized proxy changes with application releases, and
  refused stale concurrent-pipeline promotions.
- Stripped terminal control sequences from remote client output and kept
  interactive session commands recoverable after API failures.

## 2026-08-10 — Downloadable API client

- Added a dependency-free terminal client that creates and resumes sessions,
  submits messages idempotently, reconnects SSE from the last persisted event,
  reconciles durable final messages, and handles approvals and user questions.
- Added an executable zipapp build and published the artifact from
  the existing CircleCI static job.
- Refused remote bearer tokens over plain HTTP, disabled redirects, retained
  default TLS verification, and kept tokens memory-only.
- Proposed ADR-0047 for the separate transport-only client and documented the
  initial terminal scope and deferred GUI, native-binary, and credential-store
  decisions.
- Corrected the production composition default from the deterministic fake
  policy to the declared `balanced` provider policy; development and tests keep
  the fake default, and explicit policy selections remain authoritative.

## 2026-08-10 — Minimal single-Droplet launch path

- Simplified the initial topology to one Droplet with local PostgreSQL and no
  load balancer, cloud-firewall requirement, monitoring requirement, backup
  requirement, or high-availability layer; the runbook records the accepted
  exposure and data-loss risks explicitly.
- Made the runbook safe to apply on a shared Droplet: inventory existing
  listeners and containers, reuse Docker and the active reverse proxy, choose a
  free loopback PostgreSQL port, preserve Docker daemon configuration, and
  verify existing applications after the `runsc` registration restart.
- Corrected the production environment's initial scope grant to contain only
  names from the executable closed platform vocabulary and added a regression
  assertion over the template.
- Disabled PostgreSQL TLS for the loopback-only production connection so
  asyncpg does not probe a service account home hidden by systemd hardening.

## 2026-08-07 — DigitalOcean production deployment assets

- Added the first production topology decision and operator runbook for a
  host-native DigitalOcean Droplet deployment.
- Added protected environment, systemd, Caddy, gVisor, and preflight assets for
  the API, worker, and maintenance processes.
- Kept account-, network-, host-, restore-, and smoke-test checklist entries
  open until the actual production server supplies evidence.

## 2026-08-07 — Sandbox approval-resume resilience

- Added a durable regression proving that an approved sandbox call resumes on a
  different worker composition with a fresh lease-scoped workspace.
- Normalized execution-service unavailability as a retryable transport outcome
  instead of an opaque, non-retryable internal tool failure, matching the
  sandbox isolation contract.
- Added an ignore rule for the untracked local `.env` deployment file.

## 2026-08-07 — OpenAI live-run compatibility

- Made `VEETBOT_OPENAI_KEY` the canonical OpenAI credential, with
  `OPENAI_API_KEY` retained as a compatibility fallback.
- Added deterministic OpenAI-safe wire aliases for canonical dotted tool names
  and restored canonical names before normalized tool calls leave the adapter.
- Disabled OpenAI provider-strict function schemas while retaining centralized
  JSON Schema validation, and omitted the unsupported `temperature` parameter
  from Responses requests.
- Verified the exact documented `agent run --model-policy balanced` command
  against the live GPT-5.6 Sol Responses API through the durable worker.

## 2026-08-04 — Milestone 9 long-term memory and knowledge retrieval completed

- Merged the reviewed Milestone 8 pull request after all hosted CircleCI lanes
  passed and every CodeRabbit thread was resolved.
- Authorized implementation of long-term memory formation and retrieval,
  user-managed beliefs, and knowledge-document ingestion and passage retrieval.
  The 26 Milestone 9 gates bring the cumulative active gate count to 166.
- Added provenance-bound explicit formation and deterministic consolidation,
  correction tombstones, reinforcement and supersession, human CLI management,
  expiry maintenance, hybrid structured/full-text recall, reciprocal-rank
  fusion, frozen session snapshots, in-turn recall, episodic search, and
  sensitivity-filtered faithful traces.
- Added retained knowledge-source artifacts, normalized deterministic chunking,
  versioned document ingestion, principal/project/tenant visibility, bounded
  verbatim passage search with citations, and deletion that cascades through
  chunks, bytes, and historical trace visibility.
- Added the five memory and knowledge builtins to the centralized policy and
  approval pipeline. Model-context provenance is checkpointed so snapshot and
  in-turn memory cannot silently authorize a subsequent persistent write.
- Added the Milestone 9 PostgreSQL migration and repository adapters, twelve new
  port-contract suites, four adversarial/retrieval corpora, and deterministic
  `carry: [memory]` evaluation with fresh source provenance in its isolated
  second arm.
- Proposed ADR-0045 for the reversible implementation seams. Passed all 526
  non-live tests across the static, contract, PostgreSQL, resilience, and real
  Docker partitions, all 166 cumulative gates, the strict documentation build,
  and the clean Alembic metadata check.
- Addressed all hosted CodeRabbit findings, including atomic session-close
  consolidation, and resolved every review thread. The final incremental review
  completed without a new finding. All four CircleCI lanes and GitGuardian
  passed on pull request 10 commit `e5c2140`.

## 2026-08-04 — Milestone 8 skills and MCP integration completed

- Added validated immutable skill packages, content-addressed deterministic
  archives, positive revision pinning, a capped metadata-only session catalog,
  and selective body loading with checkpoint and process-reconstruction support.
- Added official-SDK stdio and HTTP MCP adapters behind repository-owned ports,
  normalized discovery, bounded authentication recovery, dynamic tool and prompt
  registration, and ordinary centralized-pipeline execution.
- Added a managed audited worker egress proxy for HTTP MCP and exact constructed
  stdio child environments. Persisted destinations are revalidated against the
  current deployment policy whenever a composition is reconstructed.
- Added deterministic two-arm skill evaluation, scripted MCP round-trip and
  disconnect cases, and the no-socket MCP harness gate. Activated all 17
  Milestone 8 gates, bringing the cumulative active gate count to 140.
- Proposed ADR-0044 for the reversible archive, pin reconstruction, prompt,
  official-SDK, child-environment, proxy, evaluation, and trace choices forced by
  the first implementation.
- Passed `make check`, all 467 non-live tests against PostgreSQL 16 and Docker,
  the 74-test PostgreSQL integration partition, strict documentation builds, and
  all 140 cumulative gates.
- Passed all four hosted CircleCI lanes on pull request 9. Addressed and resolved
  all 19 initial CodeRabbit review threads and all three incremental findings;
  the final hosted review completed without findings. Milestone 9 remains gated
  on merging the reviewed Milestone 8 pull request into `dev`.

## 2026-08-03 — Milestone 3 completed

- Authorized Milestone 3 and began implementation of the normalized model
  gateway, OpenAI Responses, Anthropic Messages, and OpenAI-compatible
  chat-completions adapters.
- Activated the work order for 15 new hard gates covering SDK isolation,
  identical provider contracts, stream invariants, tool-call identity, secret
  exclusion, malformed arguments, pinned resume, cost failure, Ollama,
  provider metadata, profile validation, redacted export, consent, and
  trajectory-sourced evaluations.
- Installed the official OpenAI developer-documentation MCP server for future
  sessions; this session uses the documented official-source fallback.
- Added strict declarative provider profiles, collision-free aliases,
  capability ceilings, immutable registry hashes, durable model pins, normalized
  streams, exact Decimal price snapshots, failed-call accounting, and bounded
  provider metadata with persistence and span mappings as its only readers.
- Added OpenAI Responses, Anthropic Messages, OpenAI-compatible chat
  completions/Ollama, recorded-stream, unavailable-credential, and upgraded fake
  adapters behind one contract suite. Provider SDK imports remain adapter-only,
  malformed tool arguments fail identically through every runtime path, and
  provider-only reasoning continuation stays in checkpoints.
- Added consent records, prospective run stamps, a fail-closed redaction and
  verification pipeline, a narrow content-addressed trajectory artifact store,
  30-day expiry and withdrawal sweeps, CLI grant/withdraw/export commands, and
  conversion of already-redacted artifacts into trajectory-sourced eval cases.
- Added the twelfth deterministic evaluation case and activated all 15
  Milestone 3 hard gates, bringing the cumulative active gate count to 72.
- Proposed ADR-0039 for the reversible provider, configuration, pinning,
  metadata, fixture, and narrow artifact-store choices forced by the first
  Milestone 3 implementation.
- Passed Ruff, strict mypy, 145 static tests, 57 contracts, all 255 non-live
  tests against PostgreSQL 16, both credentialed vendor smoke tests, strict
  documentation and citation checks, and all 72 cumulative hard gates.
- Addressed the complete CodeRabbit review and its incremental follow-ups
  across the three stacked pull requests. CircleCI jobs 67, 68, and 69 passed
  the static, integration, and contract partitions for pull request 4 commit
  `6768e82`, closing the final acceptance gap.

## 2026-08-03 — Milestone 2 completed

- Authorized Milestone 2 and advanced the current milestone to PostgreSQL
  persistence and the durable worker.
- Began implementation against the 16 new gates for event sequencing,
  projections, upcasting, migration round trips, checkpoint recovery, fenced
  claiming, resume idempotency, transaction hygiene, and ORM confinement.
- Added the linear durable-runtime migration, confined SQLAlchemy rows and
  hand-written mappers, PostgreSQL repositories and short units of work,
  atomic per-session event sequencing, version upcasting, watermarked session
  history, trajectory scaffolding, referenced full/delta checkpoints, usage
  records, the fenced `SKIP LOCKED` queue, and worker/maintenance process roles.
- Refactored the shared session, run, executor, budget, and tool paths so state
  changes and events commit together while provider and tool I/O occurs outside
  transactions. Tool recovery now snapshots idempotency classification, uses an
  effect watermark, and returns the exact persisted result on deduplication.
- Activated all 16 Milestone 2 gates (57 cumulative) and added PostgreSQL race,
  rollback, projection rebuild, checkpoint deletion, crash/reclaim, stale-fence,
  migration, concurrent-tool, and fourteen-boundary recovery cases.
- Proposed ADR-0038 with the durable seam and schema decisions that require
  owner review.
- Passed Ruff, strict mypy, 80 static tests, 29 contracts, all 138 non-live
  tests, the clean Alembic metadata check, 123 citation checks, strict
  documentation builds, and all 57 active gates. A separate worker process
  completed the calculator flow and a repeated idempotency key returned the
  same committed run.
- CircleCI jobs 19, 20, and 21 passed the PostgreSQL integration, contract, and
  static partitions for `dev` commit `75964a0`, closing the final acceptance
  gap.

## 2026-08-01 — Milestone 1 completed

- Implemented the provider-neutral domain, ports, state machine, five in-memory
  repositories, scripted fake model, minimal context builder, inline dispatcher
  and runtime, and the shared session/run services.
- Added the validated tool registry and execution pipeline plus the bounded
  Decimal calculator and injected-clock current-time builtins.
- Added the declarative evaluation schema, authored model fixtures, eleven
  initial cases, CLI `agent run` and `agent eval run`, and shared contract suites
  for every declared port.
- Activated all 28 Milestone 1 hard gates. The 41 cumulative gates, 78 static
  tests, 17 contracts, all 107 non-live tests, strict typing/linting, and
  documentation checks pass locally.
- Verified the required calculator CLI flow: stdout is exactly `391` plus a
  newline and stderr contains the six progress lines in the specified order.
- CircleCI's static, contract, and integration jobs passed for `dev` commit
  `30664ed`, closing the final completion gap.
- Proposed ADR-0037 and added review notes for the reversible decisions at the
  in-memory/durable, pre-policy, fixture-shape, eval-loading, and cross-process
  CLI seams.

## 2026-07-31 — Milestone 0 completed

- CircleCI pipeline 3 passed its hosted static, contract, and integration jobs
  for `dev` commit `5b60bca`, closing the last recorded Milestone 0 gap.
- Project state now records the repository and engineering foundation as
  complete. Milestone 1 remains authorized but has not started.

## 2026-07-31 — Milestone 0 defaults and CircleCI

- Replaced the GitHub Actions workflow with a CircleCI 2.1 configuration that
  retains the static, contract, integration, and live partitions. ADR-0035
  records the provider change.
- Expanded the six overlayable default documents and the frozen hardline rules.
  The executable configuration inventory now resolves exactly 106 unique,
  non-null knob paths. ADR-0036 records the five initial operational defaults
  the corpus named but did not value.
- Merged every change from `origin/docs/plan-completion` before applying the
  implementation updates.

## 2026-07-31 — Milestone 0 implementation started

- Established the Python 3.12 `uv`/Hatch repository, `agent` CLI entry point,
  typed settings, structured logging, configuration assets, and documented
  security baseline.
- Added the single-service PostgreSQL Compose stack, an empty Alembic revision,
  the pinned expected revision, and the four CI partitions.
- Materialized the 172-entry gate registry and made all 13 Milestone 0 gates
  executable, including import boundaries, secret scanning, transaction
  hygiene, migration-graph validation, and the six registry invariants.
- Passed `make install`, `make check`, Docker Compose startup, and an
  empty-database migration against the Compose PostgreSQL 16 service. The live
  check exposed and corrected a Compose-version portability bug in the
  Makefile's health poll. Milestone 0 remains in progress because hosted CI has
  not yet supplied its own execution evidence.

## 2026-07-31 — The ledger now fingerprints the whole span

- **A cited range was only ever checked on its first line.** `excerpt` is one
  line because a human reads it in a diff, and it was also doing duty as the
  integrity check, so `file.md:10-20` went on matching while lines 11 through
  20 were rewritten underneath it. Each ledger entry now carries a `digest`
  over every line of the cited span. A change anywhere inside a range is drift
  and is reported as drift.
- **Relocation now has to prove the whole span moved.** A first-line match only
  proposes a candidate; the candidate survives when a span of the same width
  starting there is in bounds and digests identically. That stops `--update`
  from repointing a range onto text whose trailing lines differ, and more than
  one survivor is still reported rather than guessed.
- **Fixed the legacy path the digest change had broken.** `find_span` required
  digest equality, but an entry written before digests have an empty one while
  the file yields a real one, so no candidate survived and a pre-digest entry
  became permanently unrelocatable. Such an entry now relocates on an
  in-bounds excerpt match, and the digest is computed and stored at the
  location it moved to, so it acquires one instead of carrying an empty digest
  through every future relocation. An entry that has a digest must still match
  it exactly.
- **Rule 6's runtime half is no longer claimed as covered by the contract
  suite.** That suite asserts stream behaviour and cancellation, and two
  adapters can agree on both while passing a provider object through a
  parameter annotated `ModelRequest`, `ResolvedModel`, or `ModelAttempt`;
  behavioural agreement is not type hygiene. ADR-0001 now requires
  adapter-boundary validation or conversion, with a negative contract case per
  SDK adapter.
- No product implementation was performed.

## 2026-07-31 — Twenty-one more findings, and a trust rule that did not hold

CodeRabbit's re-review of the two preceding commits, all twenty-one posted as
inline threads this time. All twenty-one were real.

- **Argument trust was inferred from a value match, and that is unsound.**
  `argument_trust` was raised to `USER` on a verbatim sixteen-character match
  against `USER`-labelled context. Equality shows two values are equal, not
  that one came from the other, so untrusted content could quote a string
  visible in the same request and be labelled trusted for it. The stated
  rationale had the direction backwards too — raising the label *removes* an
  approval rather than costing one. Provenance is now carried from where the
  call is constructed, the lower label stands wherever an untrusted item
  contributed, and unknown provenance stays `EXTERNAL_UNTRUSTED`. Corrected in
  ADR-0021 and in [tool-system.md](plan/tool-system.md), which had the same
  rule.
- **Device revocation no longer waits for a run to finish.** Section 29.7
  requires revocation to be immediate and server-side; the stamped scope set
  let an in-flight run keep acting under withdrawn scopes. Device-channel
  actions now revalidate presence and granted scopes, with the stamped set as
  a ceiling revalidation can only narrow.
- **`run.fenced` is stated as non-terminal.** A stale worker's append can land
  above the new owner's events, so a consumer deriving run lifecycle from the
  log must read it as diagnostic. A compare-and-set guard was rejected: it
  would make the record unwritable in exactly the case it documents.
- **Three more in `scripts/check_citations.py`.** The malformed-reference
  patterns missed `LINE 3`, `README.md 3`, and `foo_bar.md 3`; they are now
  case-insensitive and accept underscores. A span is validated before use,
  because `:0` indexed the *last* line of the file through Python's negative
  indexing and a reversed range like `:10-2` produced an empty excerpt that
  `--update` would have adopted as truth.
- **Precision fixes**: exactly-once narrowed to the state transition it can
  actually guarantee; `WAITING_FOR_USER`'s resume path stated, since the index
  would otherwise deadlock every question the agent asks;
  `expected_revision` demoted from idempotency key to concurrency
  precondition; `policy_version`'s truncated hash demoted to a display label
  with full digests recorded; the signature gate no longer rejects `str`; the
  milestone map's seven gates reconciled with its own claim to own no
  requirement; the evaluation suite drains post-run hooks before truncating;
  the ingest secret-scan gate no longer deletes the caller's input artifact;
  `make check` gained `docs` so the CI union claim holds; the loopback fixture
  became function-scoped, since markers are per-test; the context engine's
  history boundary and yield order stated in one unit and one order; and the
  calculator's `"391"` and ADR-0033's route count corrected.
- No product implementation was performed.

## 2026-07-31 — Twenty review findings, eighteen of them real

A second pass over CodeRabbit's review of pull request #1, which had reported
one finding as an inline thread and nineteen more inside the review body.

- **The citation checker missed three classes of input.** `BARE` and `LOOSE`
  required two digits, so a prohibited `line 9` passed unseen; both now match
  one or more. The end-of-file check tested only a citation's first line, so
  `file.md:10-999` passed whenever line 10 existed; it now tests the span's end,
  through a `span_of` helper shared with `key`. And the ledger writer escaped
  quotation marks by hand, which produces invalid YAML for any excerpt
  containing `\d` or `\s`; both scalars now go through `yaml.safe_dump`.
- **Corrected the Pandoc version in the preceding entry.** `--syntax-highlighting`
  arrived in Pandoc 3.8, not 3.10, and `--highlight-style` was deprecated in that
  same release. The gate in `scripts/build_docs.py` moved from 3.10 to 3.8, so
  Pandoc 3.8 and 3.9 now take the new spelling instead of emitting the
  deprecation warning the change existed to remove.
- **Aligned the Python version with the specification.**
  [development-toolchain.md](plan/development-toolchain.md) and ADR-0025 both
  pin `requires-python >=3.12` and one CI version with no matrix, while
  `pyproject.toml` declared `>=3.11`, the README said 3.11+, and the workflow
  pinned nothing at all. The implementation now follows the specification rather
  than the reverse.
- **Fixed six counts and identifiers that an audit would trip over**: the type
  count in Section 18 (seven named, not eight), the four-versus-six enums in
  policy-and-approvals.md's build sequence, ADR-0004's five columns that were
  four columns and an index, ADR-0030's prose line reference, ADR-0033's
  "eleventh gate" now named `gate.knowledge.no_belief_from_document`, and the
  eight-fields-versus-ten-names ambiguity in `.env.example`.
- **Closed four contract gaps**: `AuthoringContext` is defined rather than only
  referenced; `knowledge.ingest` takes a title in both descriptions;
  `ModelRequestStarted.prefix_sha256` is `NOT NULL`, because a null cannot
  participate in the exact-one-hash stability gate; and the sandbox
  production-startup gate is parameterized over every mechanism outside
  `{microvm, gvisor}` instead of naming `docker` and silently exempting `fake`.
- **Dropped the events index that served nothing.** The migration added
  `(session_id, id)` for "watermark scans", but a projection scans
  `session_id = ? AND sequence > watermark`, which Section 15's
  `UNIQUE(session_id, sequence)` already serves.
- **Two findings were rejected with cause.** `agent run export` is not an
  undefined command: `event-log-and-persistence.md:557-558` gives its arguments
  and output, and bootstrap-and-composition.md deliberately keeps it out of "the
  twelve" as a subcommand of an existing command. And the superseded
  `ModelProvider` and `RunRepository` blocks in the plan are already labelled as
  superseded in the prose beneath them, and are retained on purpose as the
  record of what they replaced.
- No product implementation was performed.

## 2026-07-31 — The append path's three wrong sentences

- Required the append transaction to roll back when its guarded `UPDATE runs`
  affects zero rows, in
  [event-log-and-persistence.md](plan/event-log-and-persistence.md). The
  document had claimed that a writer losing the status-and-lease race "commits
  nothing at all", which is not what the SQL does: a zero-row `UPDATE` is not an
  error and aborts nothing, so the sequence allocation and the event `INSERT`
  would have committed without the state change — producing the
  `run.completed`-while-`RUNNING` record the same paragraph forbids. An append
  carrying no state change, the diagnostic `run.fenced`, is named as the
  explicit exception.
- Corrected the lock explanation behind the pinned allocation mechanism.
  PostgreSQL holds a row lock until the transaction ends, not until the end of
  the statement that took it, so the claim that the atomic increment "holds it
  for one statement rather than for the caller's whole transaction" was false
  and the stated reason for pinning it did not hold. The pin stands on the
  correct reason: it is one atomic read-modify-write with no application
  round-trip inside the lock.
- Re-founded the silent-missing-write defence on the mechanism that actually
  provides it. The document had credited the partial unique index, but `UNIQUE
  INDEX (session_id) WHERE status NOT IN (...)` constrains the `runs` table to
  one non-terminal run per session and says nothing about who appends events —
  and a session already has two appenders, since the submit handler appends the
  user message from its own transaction
  ([http-api-and-streaming.md](plan/http-api-and-streaming.md)) while a worker
  appends run events. The guarantee comes from every writer allocating its
  sequence inside the transaction that inserts the event, which the corrected
  lock lifetime makes serializing. The two errors were connected: the false lock
  claim is what had made the multi-appender case look unsafe.
- Widened hard gate 1 to append concurrently *within* one session and not only
  across sessions. Separate sessions share no sequence space, so the gate as
  written exercised none of the serialization it was there to protect, and the
  document's claim that a test asserted the defence was not yet true.
- Propagated the correction to ADR-0003, the failure table, and the document's
  closing summary, and added a failure-table row for an event committed without
  its state change. Revised the corresponding entry in
  [questions-for-review.md](status/questions-for-review.md) and recorded the new
  rollback requirement beside it.
- Found by CodeRabbit on pull request #1; the third finding was re-derived
  rather than applied as written.
- No product implementation was performed.

## 2026-07-31 — Pandoc's renamed highlighting flag

- Selected the single-HTML build's syntax-highlighting flag by Pandoc
  version in `scripts/build_docs.py`. Pandoc 3.8 added
  `--syntax-highlighting` and deprecated `--highlight-style` in the same
  release; Pandoc older than 3.8 does not accept the new spelling. Both
  take the same style name, so the build reads `pandoc --version` and
  passes the spelling that Pandoc understands, with the gate set at 3.8
  because that is where the new option appears.
- Chose the version gate over replacing the flag outright because the
  repository fixes no Pandoc version and the two environments that build
  the documentation disagree in practice. CI installs whatever
  `apt-get install pandoc` supplies in
  [docs.yml](https://github.com/avitus/veetbot/blob/main/.github/workflows/docs.yml),
  and the README tells contributors to install it from their own package
  manager, so a contributor on a current release and a CI runner on a
  distribution package can differ by several minor versions. Switching the
  spelling outright would have silenced the warning on the newer Pandoc and
  broken the documentation build on the older one.
- The style is unchanged: `pygments` remains a valid style name under both
  spellings, and the generated
  `dist/engineering-documentation.html` carries the same highlighting
  rules as before.
- No product implementation was performed.

## 2026-07-31 — The values, audited

- Corrected `StopReason.STOP` in [runtime-loop.md](plan/runtime-loop.md)
  to `StopReason.END_TURN`. The member does not exist:
  [model-gateway.md](plan/model-gateway.md) declares seven and `STOP` is
  not among them. It was the only dangling member reference among sixty
  five checked, and it names the empty-turn retry path, so the wrong
  value sat on the trigger condition for `EmptyModelTurn`.
- Gave `SandboxMechanism` its fourth value in the one file that
  enumerates them.
  [bootstrap-and-composition.md](plan/bootstrap-and-composition.md)'s
  `Settings` comment listed three; six other places say four. The enum
  is declared nowhere, so the comment was authoritative by default and
  `sandbox: fake` would not have parsed.
- Extended the same file's startup check 4 to refuse `fake` in
  production beside `docker`, which
  [sandbox-isolation.md](plan/sandbox-isolation.md)'s seventeenth
  requirement already asks for. `fake` executes nothing, so a production
  deployment configured with it would have started and reported tool
  calls as run. The setting and its refusal are both Milestone 1.
- Changed the trajectory export's `outcome` in
  [event-log-and-persistence.md](plan/event-log-and-persistence.md) from
  `SUCCEEDED` to `COMPLETED`. `SUCCEEDED` is the tool-invocation
  spelling and exists nowhere at the run level. The export is persisted,
  versioned, and read by consumers who are not us, so a filter on the
  run vocabulary would have matched nothing.
- Recorded `SuspensionKind` as an observation rather than a change. It
  is never declared, its members live in trailing comments in two
  spellings, and the open hand-off question should produce the
  declaration.
- The census behind all of it: twenty eight enum declarations, twenty
  seven distinct, one benign duplicate with identical members, sixty
  five dotted member references checked against their declarations, and
  twenty two `Literal[...]` annotations. Also a numeric-constants
  cross-check across retry counts, expiries, leases, and token budgets,
  which found no conflict.

## 2026-07-31 — The types, audited

- Added the supersession paragraph
  [model-gateway.md](plan/model-gateway.md) never wrote for
  `ProviderReasoningItem`. The plan declares three fields, the
  gateway declares six, and `opaque_payload` becomes
  `provider_payload` with no sentence anywhere saying so. The field
  is persisted through `RunCheckpoint.conversation`, so the two
  declarations would have produced two different keys in the same
  stored JSON. The paragraph names all four differences, states
  that the plan's rules for provider-opaque items still govern, and
  leaves the plan's own field name to be settled as a question.
- Added a pointer under the same document's `ModelCapabilities`
  fence to the reconciliation table three hundred and fifty six
  lines below it. The reconciliation was already complete; nothing
  at the declaration said it existed.
- Recorded the plan-side rename as that document's ninth open
  question, beside the sixth, which asks the same thing about
  `tool_calling` and `vision`.
- The census behind all of it: one hundred and forty four distinct
  types across one hundred and fifty nine declarations, thirteen
  declared more than once, four of those with identical member
  sets, and eight of the remaining nine already labelled as
  extensions or supersessions.

## 2026-07-31 — The ports, audited

- Corrected [runtime-loop.md](plan/runtime-loop.md), which said
  *"Four ports the runtime needs are named in the corpus and
  declared nowhere"* and then declared five. Four is the number of
  code fences; `Clock` and `IdFactory` share one. The same document
  declares `CancellationToken` under cancellation, so it declares
  six ports in all.
- Assigned the eight declared Protocols that
  [bootstrap-and-composition.md](plan/bootstrap-and-composition.md)
  left out of its ports table: `SkillRepository`,
  `SkillPackageStore`, `Extractor`, `Chunker`, `KnowledgeStore`,
  `WorkspaceHandle`, `ArtifactWriter`, and `CredentialResolver`.
  That table exists so the first implementer does not invent a
  layout the second disagrees with, and the harness gates on a walk
  of `agent_core/ports/` that demands a contract module per
  Protocol, so the omission was load-bearing.
- Added `ports/knowledge.py` and `ports/credentials.py`, with the
  reasoning for both, and added them to the layout additions. The
  other six went into existing modules under the document's own
  rule.
- Pinned the census the table is now checkable against:
  forty-seven `Protocol` blocks naming forty-three distinct types,
  four blocks re-declaring an earlier type and four types being the
  application services of the API document, which leaves
  thirty-nine ports across fourteen modules.
- Named the five retrieval ports and `ToolRegistry` in the table,
  which had described them in prose while naming every other port
  individually.
- Left three rows naming no type and recorded them as a question:
  `ports/telemetry.py` with no telemetry Protocol anywhere, the
  formation half of the memory row, which is prose bullets while
  the retrieval spec beside it declares five Protocols, and the MCP
  row.

## 2026-07-31 — The schema, audited

- Corrected both column counts in
  [http-api-and-streaming.md](plan/http-api-and-streaming.md). The
  paragraph introducing `GET /v1/runs/{run_id}` said the `runs`
  table has fourteen columns and that the endpoint returns nine of
  them. Section 15 declares fifteen, four other documents add
  eleven more, and the JSON body immediately below has thirteen
  top-level keys. The overview of the same document called it
  twenty-three columns, which is Section 13's error-class count.
- Stated the split rather than a count. Thirteen columns go out and
  thirteen are withheld, and the line falls almost exactly between
  Section 15 and the documents that extend it: every Section 15
  column is in the body but `lease_owner` and `lease_expires_at`,
  and every later addition is withheld, though `deadline_at`
  reaches the client inside `limits`. Resolution row 12 and
  decision 22 record it.
- Corrected [readiness.md](plan/readiness.md), which summarized the
  `Idempotency-Key` resolution as *"two scopes, two tables, two
  milestones"*. The API document resolves it as one table and one
  column — `idempotency_keys` against
  `tool_invocations.idempotency_key` — and its own resolution row
  says two mechanisms and two scopes.
- Added the missing cross-reference to
  [tool-system.md](plan/tool-system.md). It and
  [policy-and-approvals.md](plan/policy-and-approvals.md) both add
  `origin_trust` and `idempotency_class` to `tool_invocations`,
  both `NOT NULL`. Only `origin_trust` said so.
- Recorded two open questions rather than closing them: whether the
  memory, knowledge, and device stores should get DDL, since they
  are the only storage in the corpus declared as Pydantic models
  and prose, and whether any document should own a census of the
  twenty-two tables the way `bootstrap-and-composition.md` owns the
  CLI census.

## 2026-07-31 — The route table, audited

- Added the missing scope-table row to
  [http-api-and-streaming.md](plan/http-api-and-streaming.md). The
  document specifies fourteen routes and the table had thirteen
  rows; the one missing is `GET /v1/sessions/{id}`, the route the
  document itself adds. `session.read` was consequently a scope in
  the closed vocabulary that no route required. The document's own
  hard gate 5 walks the route table and fails the build on a route
  that declares no scope, so it declared a gate its own table would
  fail.
- Corrected the overview, which said the document *"adds nothing to
  the API surface that Section 16 did not already put there"* and
  that *"every route below is a route the corpus already names"*.
  Its own specification of `GET /v1/sessions/{id}`, its resolution
  row 8, and its decision 20 all say otherwise.
- Stated the sum. Thirteen is what the document inherited — nine
  from Section 16, three named elsewhere, and the health probes that
  Section 16 counts as one — and fourteen is what it leaves. The
  heading now says "Thirteen inherited routes", the opening
  paragraph carries the sum, and decision 21 records it.
- Corrected five downstream sentences that closed the API at
  thirteen routes: [readiness.md](plan/readiness.md),
  [skills.md](plan/skills.md),
  [knowledge-documents.md](plan/knowledge-documents.md) three times,
  and [engineering-plan.md](plan/engineering-plan.md) twice, one of
  which asks for an error mapping for each of thirteen routes in the
  same paragraph that says one route is added.
- Corrected a CLI ordinal. `knowledge-documents.md` called the next
  command a fourteenth noun; the CLI is twelve commands and
  [bootstrap-and-composition.md](plan/bootstrap-and-composition.md)
  calls the next one a thirteenth. The CLI census is otherwise
  sound.

## 2026-07-31 — The tool registry, audited

- Removed `context.compact` from the control-tool table in
  [tool-system.md](plan/tool-system.md). It is a span name:
  [runtime-loop.md](plan/runtime-loop.md) nests it under the step
  span, the event compaction emits is `context.compacted`, and
  [context-engine.md](plan/context-engine.md) says compaction is a
  model call and therefore *"not something `build()` does"*. The
  context engine's actual control tool is
  `context.update_working_state`, which now holds the row. The
  lead-in said *"Three of the tool names"* over four rows, so the
  set is still four and every sentence in
  [skills.md](plan/skills.md) that counts it is still correct.
- Corrected two more live sentences in
  [tool-system.md](plan/tool-system.md) that called `skill_manage` a
  control tool, against its own registration table and against
  [skills.md](plan/skills.md), which argues at length that a tool
  which writes files cannot be one. An earlier pass fixed the table
  and the skills spec; these two are what a fix by search leaves
  behind.
- The builtin roster is eight tools of eighteen.
  [builtin-tools.md](plan/builtin-tools.md) now names the ten that
  other specifications declare, scopes its classification table and
  its registration steps to the eight it owns, and imports the rule
  from [knowledge-documents.md](plan/knowledge-documents.md) that
  makes the count of eight correct. Eight of the eighteen declare no
  `ToolSpec` fields anywhere, which registration step 6 reads; that
  is recorded as a conflict rather than closed.
- Split the domain partition row that labelled `memory` and `skill`
  as control domains. `memory` holds three capability tools and no
  control tool, and `skill` holds one of each.
- Corrected [readiness.md](plan/readiness.md), which said the
  agent-facing memory surface is two tools and that both read.
  `memory.remember` is a third and it writes. The gap the sentence
  supports is unchanged: none of the three lists, edits, or deletes.

## 2026-07-31 — The event catalogue and the error taxonomy, audited

- Read the event catalogue against the documents that declare into it.
  Six declared types were outside the fifty-one that
  [questions-for-review.md](status/questions-for-review.md) recorded
  as closed. `mcp.server.reauthenticated` sits in the same
  [tool-system.md](plan/tool-system.md) table as the seven the
  consolidation took, and `knowledge.document.ingested` arrived with
  [knowledge-documents.md](plan/knowledge-documents.md). Both are
  session-scoped and both are now in the consolidated list in
  [runtime-loop.md](plan/runtime-loop.md), which stands at
  fifty-three.
- The remaining four are the `eval.*` events, and they are a different
  problem. [evaluation-harness.md](plan/evaluation-harness.md) puts
  them on the harness rather than on a run, under a span root that is
  explicitly not `agent.run`, so they have no session — and
  `events.session_id` is `NOT NULL`. They are event types that cannot
  be rows in `events` as the schema stands. That is the wall
  [multi-device-and-surfaces.md](plan/multi-device-and-surfaces.md)
  already names for device lifecycle events and leaves open. The two
  documents now point at each other, and one open question covers both
  rather than each inventing a table.
- Corrected three event names in live prose that name nothing.
  [skills.md](plan/skills.md) listed `session.opened` and
  `tool.invoked` under the claim that none of the three is new;
  [bootstrap-and-composition.md](plan/bootstrap-and-composition.md)
  told the CLI to render tool activity from `tool.invoked` and
  `tool.completed`. The Section 6.8 names are `session.created`,
  `tool.call.started`, and `tool.call.completed`. This is the same
  defect the harness had, and it was corrected there by the same rule.
- Corrected [policy-and-approvals.md](plan/policy-and-approvals.md),
  which said *"One new event type"* over a block of two. Its own
  decision 28 says two.
- The error taxonomy is twenty-nine classes, not thirty-one. The eight
  that [runtime-loop.md](plan/runtime-loop.md) classifies include
  `BudgetExceeded` and `ConflictError`, which Section 13 already lists
  and leaves unclassified, so only six are new.
  [http-api-and-streaming.md](plan/http-api-and-streaming.md)
  inherited thirty-one and then invented *"the two internal
  counterparts the loop resolves itself"* so that subtracting four
  would leave the twenty-seven rows its table actually has. Set
  arithmetic gives exactly two absent, `WorkerFenced` and
  `EmptyModelTurn`, both already named in the same sentence. The
  phantom pair is gone and both counts follow from the lists.

## 2026-07-31 — The evaluation case registry, audited

- Read the case table against every statement that counts it. Eleven
  cases are writable in Milestone 1, cases 1 through 11, which
  [development-toolchain.md](plan/development-toolchain.md), the
  engineering plan, and the harness's own build order all say. Three
  live statements said ten, and one said a Milestone 1 checkout
  fails twenty of the twenty-five when the figure is fourteen. All
  four are corrected, and each now names the range as well as the
  count so the number is checkable against the table rather than
  merely asserted.
- Corrected the heading *"The twenty-five cases, with milestones and
  gates"*, which headed a table with no gate column. It now names
  the `Kind` column the table actually carries.
- Recorded what that heading was pointing at. Ninety-five of the
  hundred and seventy-two registered gates declare kind `case`,
  against thirty-one enumerated cases, and six cases are tied to a
  named gate anywhere in the corpus. The enumeration is a floor
  rather than the finished suite, which
  [evaluation-harness.md](plan/evaluation-harness.md) now says, and
  where the binding belongs is its seventh open question.
- Ran the last two of the milestone map's own hard gates that can be
  run today. Every identifier matches the grammar and its area is
  one of the fourteen, with no area unused and no slug repeated
  within one. The written census is exactly what the registry
  derives, per milestone and cumulative. Only the set comparison
  against `evals/gates/*.yaml` remains, and it cannot run until that
  directory exists.
- Left [ADR-0022](adr/0022-evaluation-harness.md) alone. It carries
  the same stale figure and is a record at a point in time.

## 2026-07-31 — The map's own hard gates, audited by hand

- Ran hard gates 1, 3, and 4 of
  [milestone-map.md](plan/milestone-map.md) by hand against the
  corpus, the three checks over the registry that the identifier
  audit did not cover. Gate 1 holds item by item: fifteen hard-gate
  sections, one hundred and seventy-three numbered items, exactly
  one milestone token each. Gate 3 holds per spec and not only in
  aggregate: every spec's gate count minus its declared aliases
  equals the entries citing it, and all three aliases name their
  owner and its gate number.
- Found that gate 4 cannot pass as written. It requires each
  registry entry's `#hard-gates` anchor to exist in the built site,
  and the two entries the engineering plan owns have no such
  section to name — the plan is organized by milestone and declares
  them in Milestone 0's acceptance criteria. Resolved by the gate's
  own title, *"Every `spec` field resolves"*, which is also how
  [evaluation-harness.md](plan/evaluation-harness.md) states the
  rule: the two name the Milestone 0 heading that declares them,
  and that anchor is confirmed present in the built site.
- Recorded the reading of gate 7. Its sentence asserts against the
  build-sequence table, which passes for all nine specs whose
  sequences carry milestones. The stricter per-gate reading is
  possible for only one table of fifteen and fails there on two
  rows the same document places deliberately and defends at length,
  so the sentence's reading is the one stated rather than left to
  whoever implements it.
- Added one conflict, one decision, and one open question to
  [milestone-map.md](plan/milestone-map.md), and three entries to
  [questions-for-review.md](status/questions-for-review.md). No gate
  statement is changed and the census is untouched: still one
  hundred and seventy-two registry entries.

## 2026-07-31 — Gate identifiers reconciled against the registry

- Widened the identifier column in four tables of
  [milestone-map.md](plan/milestone-map.md) and wrote all thirteen
  truncated rows in full. The grammar that document sets admits no
  dot inside a slug, so `gate.runtime.one_terminal_wr..` is not an
  identifier at all, and two of the document's own hard gates fail
  on one: the grammar gate because it does not match, and the set
  comparison against `evals/gates/*.yaml` because a truncated form
  cannot be a set member of anything.
- Restored nine of the twelve affected gates from spellings already
  attested elsewhere in the corpus — eight from
  [sandbox-isolation.md](plan/sandbox-isolation.md) and one from
  both the event log and the runtime loop. The other three had
  never been spelled anywhere and are completed from their own
  declaration titles, which is the one new naming decision in this
  pass and is recorded as a question.
- Answered a question already on the record, which asked whether to
  widen the column or keep the thirty-character ceiling that had
  begun shaping identifiers. It named two truncations; the audit
  found twelve gates across thirteen rows.
- Corrected two worked examples in
  [evaluation-harness.md](plan/evaluation-harness.md) that named
  gates the registry does not hold, and two milestone counts in one
  of them that disagreed with the census.
- Re-derived the census from the corrected tables and confirmed it
  unchanged: one hundred and seventy-two registry entries, the same
  four kind totals, and the same per-milestone counts.

## 2026-07-31 — A pass that tried to falsify the corpus

- Ran a closing pass whose only job was to falsify the claim that
  the corpus is complete and that no specification names an
  unresolved blocker. It raised ten findings. Checking each against
  the sources confirmed eight and cleared two: one is an explicit
  deferral, which [readiness.md](plan/readiness.md) already rules is
  not a gap, and the other was already an open question in the
  document it was raised against.
- Reversed a verdict an earlier pass had recorded, which read
  [http-api-and-streaming.md](plan/http-api-and-streaming.md) as the
  document in error about which milestone the approval routes land
  in. It is not the document in error. Milestone 5's implement list
  is the entire HTTP surface and Milestone 1 has no HTTP API at all,
  so no route can precede Milestone 5, and
  [policy-and-approvals.md](plan/policy-and-approvals.md) already
  says the CLI calls the application service rather than the route.
  Build step 11 in that document is narrowed to the service methods
  and the CLI, and a new contradictions row records the split.
- Registered the skill management tool as `skill.manage`. Section
  30.2 spells it `skill_manage`, which the name grammar at
  `tool-system.md:336` rejects, because every registry name needs at
  least one dot and a capability tool is a registry entry. The
  `skill` domain already holds `skill.load`, so no new domain is
  needed. The plan's spelling stays as the word for the tool; the
  string the registry, the policy rules, and the model see is the
  dotted one.
- Corrected two cross references that pointed at the wrong line and
  the wrong section, one description of a sibling document that the
  sibling had since outgrown, and one sentence in
  [milestone-map.md](plan/milestone-map.md) that still read as
  though Milestone 10 added no gates of its own, after
  [skills.md](plan/skills.md) gave it six.
- Gave Section 31 the outward pointer every expanded section
  carries, naming the two documents that expand its production half
  and the one that already held its consumption half.
- Recorded the one finding this pass does not fix. The `MemoryStore`
  port declares `list`, `edit`, and `delete`, and no tool, route, or
  command in the corpus calls any of them, so *"A user can inspect
  and delete stored memories"* is the one Milestone 9 acceptance
  criterion with nothing behind it to test. Designing edit semantics
  over an append-only belief store is Milestone 9 work, so the
  finding is sharpened in [readiness.md](plan/readiness.md) and left
  open.

## 2026-07-31 — Milestone 10, answered by re-measuring it

- Answered the last open question in
  [readiness.md](plan/readiness.md), which asked whether Milestone 10
  needs acceptance criteria or is correctly an open direction. It is
  correctly an open direction. Two of its four parts gate on
  evidence rather than on a date — a scheduler comes *"only after
  durable on-demand runs are reliable"* and subagents come *"only
  when evaluation evidence shows that a single agent fails"* — and
  acceptance criteria are a promise about a delivery, which cannot
  be made about work that must not start until evidence arrives.
  What the milestone is missing is the heading, not the content the
  heading would hold.
- Re-measured all twenty-two Milestone 10 requirements against the
  corpus before answering, because the answer turns on what is
  actually behind the milestone rather than on how it is formatted.
  The scheduling and routing verdicts held. The subagent verdict did
  not.
- Corrected this review's own subagent count, the only verdict in it
  that later documents overtook. It named five of nine requirements
  as having no design; five of the nine are supplied now. Restricted
  context is `context-engine.md:278` plus the child-run recall class
  at `memory-retrieval-and-ranking.md:87`, the restricted tool set is
  `tool-system.md:949`, and the child deadline is
  `runtime-loop.md:1141`, all written after the verdict was. Two are
  partial and two still have none: the separate trace and the
  artifact references, which no specification picks up.
- Recorded one conflict the stale count was hiding.
  [event-log-and-persistence.md](plan/event-log-and-persistence.md)
  enforces one active run per session with a unique partial index,
  and a parent suspended on a child is not in a terminal status, so
  a child run in the parent's own session cannot be inserted.
  Section 27.6 offers the parent's session or a dedicated child
  session per policy, and only the second survives the index. It is
  recorded rather than resolved, because resolving it is Milestone
  10 work and this review authorizes none.
- Left the verdict and the gate census alone. Milestone 10 is still
  not a milestone, its six gates still come from
  [skills.md](plan/skills.md) rather than from criteria of its own,
  and no document was added, so the census stays at one hundred and
  seventy-two registry entries.
- Updated the verdict table's last column for Milestone 10 from *"No
  acceptance criteria exist"* to *"Its own entry gates, by design"*,
  and carried the answer into
  [project-state.yaml](status/project-state.yaml).

## 2026-07-31 — Section 29's Device model, closed as an audit

- Wrote
  [multi-device-and-surfaces.md](plan/multi-device-and-surfaces.md)
  and ADR-0034, the seventeenth detailed-design document and the
  last gap [readiness.md](plan/readiness.md) named. It audits a seam
  rather than designing one. Section 29's own last subsection says
  *"Defer the Device concept, presence, device-scoped tool routing,
  and notifications"*, and the plan's sequencing table puts inbound
  surfaces and pairing at Milestone 10, so writing contracts for the
  four ports Section 29.6 names would have been building the thing
  the plan defers.
- Made the check the deliverable. A deferred design has to be
  additive when it lands, and the only way to know whether it is
  additive is to walk the seam. Eight places already hold a
  device-shaped hole and need no edit at all. Five do not, and each
  is written down with what resolving it will cost: attach is a
  third registration source and not at session open, device
  lifecycle events have no session to be charged to, a hand-off is a
  fourth suspension kind, no client is attributed on a write, and
  `NotificationService` is a port name with nothing behind it.
- Gave the four ports Section 29.6 names a home, which is what the
  readiness finding actually asked for. They land under
  [bootstrap-and-composition.md](plan/bootstrap-and-composition.md)'s
  existing rule that a port lives in the module named for the
  capability it abstracts, not for the component that calls it.
- Found that per-device scopes are an intersection computed once at
  submission and never consulted again. `runs.principal_scopes` is
  stamped when the run is submitted and `PrincipalResolver.for_run`
  reads the stamp and never a table, so the policy engine is never
  told a device exists. The constraint that travels with it: a
  per-device scope set must be a subset of the fifteen strings and
  must never introduce a `device.` scope prefix, which would break
  [policy-and-approvals.md](plan/policy-and-approvals.md)'s hard
  gate 11. The `device.` that exists today is a tool-name domain, an
  unrelated namespace.
- Resolved three conflicts in the specifications' favour rather than
  the plan's. Presence-based exposure yields to the pinned
  advertisement prefix, on the same precedent as an MCP catalog
  change: recorded and not applied, with the change visible at the
  next session open. The registry accepting new entries from
  *"exactly two sources"* needs its count corrected, because attach
  is a third. Section 29.5's *"queue or reject"* is reject, because
  ADR-0004's partial unique index makes a second active run on a
  session impossible to enqueue rather than merely unwise.
- Declared no gates, so the census is unchanged: 166 declared across
  fourteen specifications, 175 declarations, 172 registry entries.
  Gate-less specifications go from two to three.
- Corrected a defect the audit turned up.
  [tool-system.md](plan/tool-system.md) called `tool.device_offline`
  *"the third row of the availability table"* when it is the last
  row; the table gained rows for MCP authentication after that
  sentence was written.
- Moved the counts that move: sixteen detailed-design documents to
  seventeen in `CLAUDE.md` and
  [development-toolchain.md](plan/development-toolchain.md), and
  thirty-three ADRs to thirty-four. Refreshed the prose summary in
  [project state](status/index.md), which still said Milestones 0
  through 4 were implementable and three named documents did not
  exist.

## 2026-07-28 — Milestone 9's knowledge-document gap is closed

- Wrote [knowledge-documents.md](plan/knowledge-documents.md) and
  ADR-0033, the fourteenth detailed-design specification. Milestone
  9 is titled *"Long-term memory and knowledge retrieval"* and only
  the memory half had a design; nothing said what a knowledge
  document is, how one is ingested, chunked, indexed, or scoped, or
  how retrieval over it differs from retrieval over memory.
- Separated the two stores by what they answer. A belief answers
  *what is true* and the unit of retrieval is the claim; a document
  answers *what does the source say* and the unit of retrieval is
  the passage, quoted verbatim and cited. Both memory specifications
  open with a scope line that says beliefs and episodes, which is
  why this is a fourteenth document rather than a section in either.
- Put the bytes in the artifact store under a sixth origin,
  `KNOWLEDGE_SOURCE`, and kept them after extraction. It is the one
  origin whose lifetime is not the run's: stored with no
  `expires_at`, it acquires one at deletion so
  [sandbox-isolation.md](plan/sandbox-isolation.md)'s existing
  sweeper collects it, which is ADR-0032's consent-withdrawal move
  reused.
- Made ingestion a builtin, `knowledge.ingest`, rather than a
  fourteenth route or a thirteenth CLI noun. Both of those lists are
  deliberately closed, and a tool reaches the policy engine, the
  approval path, and the event log with no new machinery. Admission
  requires `USER` origin trust, so an agent cannot admit what it
  fetched.
- Split the two scans. A detected credential refuses the whole
  ingest and nothing is written, because a secret in a permanent
  corpus is quoted back verbatim by design. Instruction-like text is
  recorded on the chunk and ingested anyway, because it is
  survivable by labelling and a blocking scan would refuse most real
  technical documentation.
- Chunked structure-first with no overlap — target 600 tokens,
  ceiling 1,000, floor 100 — and gave chunks heading paths instead.
  The chunk id is the citation, so overlapping chunks would make a
  citation ambiguous, and the chunker is deterministic under a
  `chunker_version` because a citation that stops resolving after a
  library upgrade is a broken citation.
- Inverted the isolation predicate. `visibility` in `{principal,
  project, tenant}` replaces `principal_id`, which is the exact
  opposite of the carry-by-default rule the memory layer took for
  beliefs: a document is shared unless it is scoped, a belief
  travels unless it is pinned.
- Gave knowledge its own Region B budget class in
  [context-engine.md](plan/context-engine.md) — three passages or
  3,000 tokens, first in a now four-step yield order. Passages drop
  whole and are never truncated, because a passage shortened to fit
  is a misquotation attributed to a real document.
- Added `knowledge.write` to
  [policy-and-approvals.md](plan/policy-and-approvals.md)'s closed
  scope vocabulary, taking it from fourteen strings to fifteen, and
  a `knowledge` domain to
  [tool-system.md](plan/tool-system.md)'s partition table.
- Declared twelve gates in a new fourteenth area, `knowledge`, all
  at Milestone 9: eight cases, three property gates over chunk
  stability, verbatim extraction, and citation resolution, and one
  corpus gate with a floor on passage recall and a ceiling on noise.
  The corpus goes from one hundred and sixty registry entries to one
  hundred and seventy-two, and Milestone 9 from fourteen to
  twenty-six. [readiness.md](plan/readiness.md) now carries no
  milestone with a named gap.

## 2026-07-28 — Milestone 8's MCP authentication gap is closed

- Gave `credential_ref` a counterpart in
  [tool-system.md](plan/tool-system.md). The column said where a
  secret is; nothing said what the reference resolves to, and the
  broker cannot infer it — a bearer token and an OAuth client secret
  are both opaque strings, and a resolver that guesses between them
  eventually presents a client secret as a bearer token to a server
  that logs its `Authorization` headers.
- Made the scheme configuration rather than inference. `mcp_servers`
  gains `auth_scheme`, `auth_name`, `token_endpoint`, and
  `token_scopes`; the scheme is a closed set of five — `none`,
  `bearer`, `header`, `oauth2_client`, and `env` — and it lives in
  the row rather than inside the secret, because validating a row
  should not require dereferencing one, secrets rotate and protocols
  do not, and the scheme is named in operator-facing errors and in
  `mcp.server.disconnected`.
- Moved the checking to write time. A scheme outside the five, a
  scheme on the wrong transport, `header` naming `Authorization`,
  `env` naming a tier-0 variable, or an `oauth2_client` token
  endpoint the egress allowlist does not permit are configuration
  errors a human sees before anything is dialled. The tier-0 list is
  read from [sandbox-isolation.md](plan/sandbox-isolation.md)'s
  definition rather than copied.
- Bounded the re-authentication path and routed it through the
  recovery table already in the spec. One re-authentication per
  server per session, one retry and only where recovery permits,
  `UNCERTAIN` rather than a retry for a non-idempotent call whose
  watermark is set — a 401 arriving after `mark_effect_sent` says
  nothing about whether the effect landed — then `unavailable` with
  `tool.server_unauthorized`. Expiry is checked when a header is
  built rather than on a timer, and no refresh token is stored,
  because the client-credentials grant is not supposed to issue one.
- Built the stdio child's environment instead of inheriting it: the
  synthesized sandbox tier plus the one declared credential
  variable, with the credential never reaching `argv`. Inheritance
  is the default behaviour of every process-spawning API in the
  standard library, and what it would hand an operator-configured
  third-party process is the worker's database URL and every
  provider key.
- Deferred the user-delegated OAuth flows and said so in the
  vocabulary. A server that needs an authorization-code redirect
  fails to connect with `tool.auth_unsupported`; the unlock is an
  interactive authorization surface on the HTTP API, which is past
  0.1.
- Added three gates, all Milestone 8, dividing by what each needs in
  order to run: `gate.tool.mcp_auth_config` over the validator
  alone, `gate.tool.mcp_reauth_bounded` against a server that
  returns 401 on demand, and `gate.tool.mcp_stdio_env_built` against
  a child process whose environment can be read back. The census is
  now **one hundred and sixty registry entries**, eighty-seven of
  them cases, and Milestone 8 carries seventeen.
- Changed Milestone 8's verdict in [readiness.md](plan/readiness.md)
  from ready with named gaps to **ready**. One named gap remains in
  the corpus: knowledge documents at Milestone 9.
- Fixed four sentences that had fallen behind what they describe.
  `tool-system.md` said "three different things" over a four-row
  availability table; `evaluation-harness.md` said "seventy-one of
  the hundred and fifty-six declared gates" and `milestone-map.md`
  said "one hundred and fifty declared across thirteen specs, one
  hundred and fifty-six registry entries", both stale from before
  the Milestone 7 gate was added; and the map's third open question
  still said the MCP half registers no invariant of its own, which
  stopped being true two passes ago.

## 2026-07-28 — Milestone 7's history predicate is closed

- Gave history selection a predicate in
  [context-engine.md](plan/context-engine.md), which the readiness
  review named as Milestone 7's surviving shortfall and said to close
  first. The yield order and the 8,000-token floor were already
  specified; what decided that a given turn was in or out was not.
- Split it into the two selections it actually is. Seeding a run
  reads the session-history projection; assembling a request reads
  the run's checkpoint. The checkpoint is closed and ordered, so
  assembly was always the easier half — the projection is a live read
  model that advances on a timer, and reading it for "the session's
  history" returns whatever has been applied at the moment of the
  call.
- Pinned the seed to a log prefix. `seed_checkpoint` reads history
  strictly below `runs.seed_event_sequence`, the session sequence of
  the `user.message.created` event the run answers, written in the
  transaction that already allocates it. `seed_checkpoint` has two
  call sites — run creation and the rebuild forced when a run's
  checkpoints are deleted — and hours can separate them. A live read
  would have made the second seed disagree with the first, which is
  the failure the Milestone 2 dispensability gate exists to detect.
- Made the retained set a contiguous suffix: one cut index, a
  backward scan against `budget.history_tokens`, floored at
  `replaced_through_sequence`, moved later past any tool pair it
  would split, taken after the never-yield items are subtracted.
  `select_history` returns the index rather than the list, so
  contiguity is carried by the return type.
- Ruled out a relevance ranking over past turns, and said why. It
  produces a transcript with holes that a model reads as a
  conversation in which the missing thing never happened, and it is a
  second retrieval system beside in-turn recall — after which a
  turn that should have been present and was not is ambiguous between
  a selection defect and a ranking miss. History is recency; recall
  is relevance.
- Required `TokenEstimator.estimate` to be a pure function of its
  arguments. Approximate was always allowed and still is; an
  estimator that answers differently on two identical calls moves the
  cut, which is the whole failure. A cache may change how long it
  takes and never what it returns.
- Registered one gate, `gate.context.history_cut` at Milestone 7,
  property-tested over generated item lists. The seeding half gets no
  gate, because the dispensability gate already asserts the property
  and only ever could once the cut was fixed. The census moves to one
  hundred and fifty-seven registry entries from one hundred and
  fifty-one declarations across the thirteen specs, with Milestone 7
  at seven and non-case gates at seventy-two.
- Corrected a numbering defect the skills specification left behind:
  the compaction section said the summary "sits at position 6 in the
  assembly order" when rows 5 and 9 had pushed it to 7. Position 6 is
  the session-open memory snapshot.
- Changed Milestone 7's readiness verdict to ready. Two milestones
  carry named gaps: 8 and 9.

## 2026-07-28 — Milestone 4's second gap is closed

- Designed the principal scope vocabulary in
  [policy-and-approvals.md](plan/policy-and-approvals.md), which the
  readiness review named as the second of Milestone 4's two gaps. It
  is one closed set of fourteen strings: the nine
  [http-api-and-streaming.md](plan/http-api-and-streaming.md) already
  enumerates and five more that appear as `ToolSpec.required_scopes`
  on the builtin roster. One namespace and not two, because
  `artifact.read` and `artifact.write` are two actions on one
  resource, and because `skill.write` was already an API scope
  checked by the policy engine rather than by a route.
- Answered what a scope is. An opaque string compared by exact match,
  the check a set difference and all-of, with no hierarchy, no
  wildcard, and no prefix rule — so `run.write` does not satisfy
  `run.read`, and a tool that means both declares both. A failure is
  `AuthorizationError` and the denial reason `policy.scope.missing`.
- Reserved `mcp` as a first segment. MCP `required_scopes` are
  operator-declared, so a list closed against them is a list the
  operator routes around; a declared scope is legal only if it is one
  of the fourteen or its first segment is `mcp` and its second is the
  server id. What that blocks by construction is an operator
  declaring that a remote filesystem-write tool requires
  `session.write` — a line that reads as a restriction and grants.
- Made the scope denial the one denial that names something. It names
  the scopes the action required and the principal lacks, never the
  ones the principal holds, because a model cannot climb toward a
  scope it cannot grant itself and the sentence is for the human
  reading the transcript. The held set stays withheld, being a map of
  the surface still worth probing.
- Stamped the scope set on the run. A new `runs.principal_scopes`
  `JSONB` column is written at submission and
  `PrincipalResolver.for_run` reads it rather than a principal table,
  because a worker holds no credential and re-deriving would make the
  runtime loop's *"takes effect on the next run"* depend on queue
  latency rather than on submission order.
  [ADR-0032](adr/0032-trajectory-export-redaction-and-consent.md)
  chose the same shape for the consent stamp.
- Recorded that `Principal.roles` is populated for audit and read by
  nothing in 0.1, and that `AUTH_MODE=dev` binds all fourteen
  first-class scopes and no `mcp.` scope, so a developer is never
  blocked by authorization on what the platform ships and always
  blocked by it on what a server they just connected declares.
- Fixed a route-table defect: `GET /v1/approvals/{id}/resolve` is
  `POST`. The same document's request body, the policy specification,
  and `ApprovalService.resolve` all describe a state change.
- Registered three gates, all at Milestone 4:
  `gate.policy.scope_grammar`, `gate.policy.scope_match`, and
  `gate.policy.scope_stamped`. The census is one hundred and
  fifty-six registry entries from one hundred and fifty-nine
  declarations; Milestone 4 goes from nineteen to twenty-two, the
  cumulative column reaches one hundred and fifty-six, and the
  non-case count reaches seventy-one.
- Closed Milestone 4 in [readiness.md](plan/readiness.md). Both named
  gaps are gone and its verdict changes from *"ready with named
  gaps"* to ready. The `ApprovalService` half of this gap had already
  closed on its own: the API specification gave it a three-method
  Protocol after the review was written.

## 2026-07-28 — Milestone 4's first gap is closed

- Designed the four remaining builtin tools in
  [builtin-tools.md](plan/builtin-tools.md) — the three `workspace.`
  ones and `demo.external_write` — which the readiness review named as
  the first of Milestone 4's two gaps. The section that deferred them
  is now *"The two tools this document does not design"*, and both of
  those are Milestone 6.
- Established that no `workspace.` tool resolves a path. All three
  hand the caller's string to `WorkspaceHandle` and let the execution
  service resolve it on its own side of the boundary, so there is one
  containment rule rather than three. `gate.builtin.handle_only`
  asserts it structurally: the three modules import no `os`,
  `os.path`, `pathlib`, `shutil`, or `glob`, call no `open`, and reach
  the filesystem only through `ToolExecutionContext.workspace`.
- Fixed the encoding rules — UTF-8 strict, a NUL byte as the binary
  test, an incremental decoder so a character split across a chunk
  boundary is not read as a malformed one — the SHA-256 checksum over
  the encoded bytes, a listing capped at a thousand entries and
  ordered so that a truncated one is a prefix of the full one, six
  JSON schemas, and four reason codes.
- Gave `WorkspaceHandle` a `provenance` method and a
  `WorkspaceProvenance` enum in
  [sandbox-isolation.md](plan/sandbox-isolation.md). `write` records
  `TOOL_WRITTEN` in the same operation that writes the bytes, so
  `workspace.read_text` can decide between `INTERNAL_TOOL` and
  `EXTERNAL_UNTRUSTED` without a database session
  `ToolExecutionContext` deliberately does not carry.
  `SANDBOX_WRITTEN` is defined two milestones before anything can
  produce it, so the Milestone 6 implementer inherits the answer
  rather than choosing it.
- Declined to give the reader a size-limit failure of its own. A large
  file is a large result and the pipeline's excerpt-and-artifactize
  step already owns that; a second ceiling is a second truncation
  policy to keep in agreement with the first.
- Registered six gates, all at Milestone 4:
  `gate.builtin.handle_only`, `gate.builtin.text_only`,
  `gate.builtin.write_idempotent`, `gate.builtin.listing_stable`,
  `gate.builtin.provenance`, and `gate.builtin.demo_records`. The
  census is one hundred and fifty-three registry entries from one
  hundred and fifty-six declarations; Milestone 4 goes from thirteen
  to nineteen and the cumulative column reaches one hundred and
  fifty-three.
- Corrected a readiness finding that had gone stale. The review said
  the acceptance criterion *"Path traversal is rejected"* stood on an
  algorithm no document contained; `sandbox-isolation.md` was written
  afterwards and contains it. The finding is re-tensed and a *"What
  closed it"* subsection added, and two citations into
  `builtin-tools.md:909` — both pointing at a `sandbox.run_command`
  milestone error corrected long ago — are repointed to the line that
  now carries the corrected statement.

## 2026-07-28 — Milestone 3's three gaps are closed

- Closed the readiness review's three Milestone 3 gaps, each in a
  document that already owned the subject rather than in a fourteenth
  specification. Recorded as
  [ADR-0032](adr/0032-trajectory-export-redaction-and-consent.md).
- Made `provider_metadata` a closed set in
  [model-gateway.md](plan/model-gateway.md): a declared field list, a
  persisted column on `model_calls`, and exactly two readers — the
  persistence adapter's flattening function and the span builder. An
  adapter writing an undeclared key fails
  `gate.model.metadata_closed`.
- Gave the declarative provider profile a document schema in the same
  specification: fields, required set, a rule table the loader
  enforces, and a stated answer for a profile claiming a capability
  its adapter does not have. `gate.model.profile_valid` is a corpus
  gate, because one invalid profile proves only the rule it broke and
  the rule table has a row for each.
- Specified the trajectory export in
  [event-log-and-persistence.md](plan/event-log-and-persistence.md),
  which had the log, the projections, the schema, and the
  `gate.event.*` area already. One versioned JSON document in the
  `messages` shape, written to the artifact store under a new
  `TRAJECTORY_EXPORT` origin, with the seven excluded field families
  tabulated and a reason for each.
- Made redaction three stages that fail closed: structural exclusion,
  pattern replacement reusing the committed-secret scanner's five
  rule families and the log processor's key-name families, then a
  verification scan that raises, writes no artifact, and names the
  rule without printing the match. It does not redact a second time,
  because a second pass hides the gap in the first and ships the
  artifact anyway.
- Designed the consent record the corpus had asserted four times and
  never defined. A grant is evaluated at run start and stamped on the
  run; a withdrawal reaches every run and expires every artifact
  already produced, through `expires_at` and the sweeper that already
  runs. Two gates, `gate.event.export_redacted` and
  `gate.event.export_consent`, both Milestone 3.
- Made `export` a fourth reserved word after `agent run` rather than
  a thirteenth top-level command, on the precedent
  [evaluation-harness.md](plan/evaluation-harness.md) set with four
  `agent eval` subcommands. The CLI still has twelve commands, and
  [bootstrap-and-composition.md](plan/bootstrap-and-composition.md)'s
  heading now names the rule instead of the count.
- Fixed two defects found in passing.
  [sandbox-isolation.md](plan/sandbox-isolation.md) used an
  undeclared `TrustLabel` at two sites where the declared type is
  `TrustLevel`, and `ArtifactOrigin` had no member for an export;
  `TRAJECTORY_EXPORT` is the fifth origin and the only one whose
  contents are a function of a whole run.
- Re-derived the census rather than editing it. Four new gates, all
  Milestone 3: one hundred and forty-one declared across thirteen
  specs, one hundred and fifty declarations, one hundred and
  forty-seven registry entries. Kinds move to seventy-nine case,
  seventeen property, eight corpus, forty-three structural. Milestone
  3's row moves from eleven to fifteen and every cumulative below it
  by four. Nine live arithmetic locations updated across
  [milestone-map.md](plan/milestone-map.md),
  [evaluation-harness.md](plan/evaluation-harness.md),
  [readiness.md](plan/readiness.md), and
  [engineering-plan.md](plan/engineering-plan.md).
- Moved Milestone 3's verdict from *"ready with named gaps"* to
  *"ready"*, and corrected an inversion the review carried: the
  export is the production half of Section 31 and the harness's
  conversion is the consumption half, not the other way round.

## 2026-07-28 — Every cited line is now a checked citation

- Converted thirty-five line references that the citation checker
  could not see into the form it can. *"line 1408"*, *"lines 659 to
  661"*, and *"`tool-system.md` 1102-1149"* become
  `engineering-plan.md:1424`, `context-engine.md:680-682`, and
  `tool-system.md:1102-1149`. Twenty-eight patches across
  [model-gateway.md](plan/model-gateway.md),
  [readiness.md](plan/readiness.md),
  [runtime-loop.md](plan/runtime-loop.md), and
  [skills.md](plan/skills.md).
- Re-resolved every one of them by content against the current file
  rather than by arithmetic. Most had drifted; two by more than
  eighty lines. The forms the checker could not see were the only
  ones that could rot, because the form it can see is repaired on
  every run.
- Extended `scripts/check_citations.py` with a `check_bare_references`
  pass that fails `make docs-check` on a line named in prose or on a
  `file.md NNN` form with a space where the colon belongs. Both match
  across a single line break and neither matches across a blank line,
  because a reference may wrap and a paragraph boundary is not one.
- Added `docs/adr/*.md` to the checker's target globs, last, so that
  `index.md` still resolves to `docs/index.md`. Three ADR-targeted
  citations exist as a result.
- Corrected an ADR filename that no file has ever had.
  [skills.md](plan/skills.md) cited
  `docs/adr/0005-two-stage-policy-and-approval-model.md`; the ADR it
  means is `0005-deterministic-policy-engine.md`, and its lines 10
  and 141 both still say what the sentence needs them to say.
- Grew the ledger from thirty-three citations to sixty-five, and
  added the rule to `AGENTS.md`: write `file.md:LINE`, never a line
  named in prose.

## 2026-07-27 — The ORM surface and the migration conventions

- Closed both partial bullets the readiness review named against
  Milestone 2, inside
  [event-log-and-persistence.md](plan/event-log-and-persistence.md)
  rather than in a twentieth specification, and recorded the reasoning
  as [ADR-0031](adr/0031-persistence-authoring.md). That document
  already owns the schema and the `gate.event.*` area; a new spec
  would have needed a fourteenth gate area or a shared one.
- Added *"The ORM surface"*. Row classes are separate declarative
  types confined to `adapters/persistence/`, translation is two
  hand-written functions per table in a `mappers.py` beside them, a
  repository is constructed with a live session and never commits, and
  every repository method returns a domain type, a `domain` read
  model, a scalar, or `None` under a concrete annotation. The shape is
  forced rather than chosen: declarative mapping of a domain type
  fails rule 1 on the import walk, and imperative mapping fails rule 7
  silently because the domain object becomes the ORM object.
- Added *"Authoring migrations"*. One head and no merge revisions;
  `<revision>_<slug>.py` names that carry no order because ordering
  lives in `down_revision`; structure and data in separate revisions;
  autogenerate as a draft kept honest by an empty-diff round trip;
  lock-taking DDL alone in a non-transactional revision; a revision
  may add to `events` and may never rewrite one; and
  `EXPECTED_REVISION` as a module constant rather than a head computed
  at runtime, which is the mechanism ADR-0024 decision 6 required and
  left open.
- Added five hard gates. `gate.structure.migration_graph` registers at
  Milestone 0, because the empty Alembic migration that milestone
  already requires is a graph and a walk that begins after a dozen
  revisions has already missed the branch it exists to prevent.
  `gate.event.migration_clean`, `gate.event.migration_stepwise`,
  `gate.event.revision_pinned`, and `gate.structure.orm_confined`
  register at Milestone 2. Four of the five observe Section 24
  criteria that were conditions of every milestone with nothing
  evaluating them.
- Re-derived every affected count. One hundred and forty-three
  registry entries from one hundred and forty-six declarations, one
  hundred and thirty-seven of them across thirteen specifications; the
  kind split becomes seventy-seven case, seventeen property, seven
  corpus, and forty-two structural; the per-milestone census becomes
  13, 28, 16, 11, 13, 11, 11, 6, 14, 14, 6. Forty-one gates are green
  before Milestone 2, thirteen of them against a repository with no
  agent in it.
- Milestone 2's verdict row moves from *"Migration authoring
  conventions"* to *"Nothing"*, which makes Milestones 0 through 2 the
  first run of three consecutive milestones ready with nothing
  outstanding.
- Corrected three mis-numbered cross-references found while grounding.
  `bootstrap-and-composition.md` cited Section 3 for the
  `AsyncSession` unit-of-work rule, which is Section 2.2;
  `model-gateway.md` cited Section 3 for the import-boundary tests,
  which Section 5 requires; and the plan's Milestone 0 pointer
  paragraph named eleven registry entries and one plan-owned gate,
  which are thirteen and two.
- Replaced two bare line numbers in a [skills.md](plan/skills.md)
  finding paragraph with a checked citation. The citation checker only
  sees `file.md:NNN`, so this pass updated the backticked number and
  left the bare one behind, in the one paragraph in the corpus that is
  about citation drift.

## 2026-07-27 — The verdict table, re-derived

- Corrected Milestone 8's row in the verdict table of
  [readiness.md](plan/readiness.md) from ten registry entries to
  fourteen, and its named gap from *"MCP auth scheme; the mock
  server"* to *"MCP auth scheme"*. The four MCP gates added on the
  previous pass registered at Milestone 8, and the column counts
  registry entries whose `milestone` field names that milestone, so
  the number was wrong the moment they were written. The Milestone 8
  section four hundred lines below already said fourteen and named
  only the auth gap — a live document disagreeing with itself.
- Re-derived every row of that table from the registry rather than
  reading it. The other ten agree. The totals reconcile three ways:
  one hundred and thirty-eight registry entries from one hundred and
  forty-one declarations across thirteen specifications, the
  per-milestone census, and the kind split of seventy-four case,
  seventeen property, seven corpus, and forty structural.
- Did not add a mechanical check for this one, and the reason is
  recorded rather than assumed. `gate.harness.census_derived` already
  requires a test that computes the census from the registry and
  compares it to the written table, and it is a Milestone 0 gate with
  a home in the evaluation harness. Writing it into
  `scripts/check_docs.py` now would implement a milestone gate ahead
  of its milestone, against the standing constraint. The open
  question — widen that gate to cover the verdict table, or delete
  the column and send the reader to the census — is in
  [questions-for-review.md](status/questions-for-review.md).

## 2026-07-27 — Harness case gaps, MCP gates, and citation integrity

- Closed the three structural gaps the milestone map's census made
  visible. Build step 9 of the tool system — the MCP adapter — was
  the only build step in any specification with no gate observing
  it, and Milestone 8's MCP half carried none of its own. It now
  carries four: `gate.tool.mcp_pipeline_parity`,
  `gate.tool.mcp_disconnect`, `gate.tool.mcp_sdk_confined`, and
  `gate.harness.mcp_no_socket`. Each asserts that the widened
  surface is the same surface — no path around the fourteen-step
  pipeline, no failure outside the outcome vocabulary, no SDK type
  leaking upward.
- Added harness cases 28 through 31, for the long-session, MCP, and
  memory-recall gates that had no case behind them, and gave the
  case schema `arms`, `carry`, and `delta`. Case 27 had described
  "runs one task twice and compares" in prose since it was written,
  with no schema that could express it; two memory gates and Section
  30.5's rollout criterion are stated the same way. The assertion
  vocabulary gains a fifth type, cross-arm metric relation, and
  ADR-0022's "gain four" is left as the record it is.
- Designed the scripted MCP server as a fourth fixture kind. The
  plan's *"Mock MCP server tests"* implement bullet had no design
  under it. The fixture is authored YAML, loaded at collection time,
  and opens no socket and starts no subprocess.
- Corrected the gate arithmetic. One hundred and thirty-eight
  registry entries from one hundred and forty-one declarations, and
  a kind split of seventy-four case, seventeen property, seven
  corpus, and forty structural — derived twice, once from the
  census and once from the harness's own kind table, and reconciled.
  The claim that *"more than half"* of declared gates are not case
  gates was false when written and is replaced with the count.
- Added `scripts/check_citations.py`, a generated ledger at
  `docs/status/citation-ledger.yaml`, and a `make citations-fix`
  target. The specifications cite each other by line number, and an
  insertion above a cited line moves it silently. A sweep found nine
  wrong citations across five documents, two of them created by this
  session's own edits; all nine are corrected. `make docs-check` now
  fails when a citation no longer holds the text it was recorded
  against, and `make citations-fix` repoints one whose text has
  merely moved. A citation whose text is gone or now ambiguous is
  reported rather than guessed.

## 2026-07-27 — The skill package, the catalog, and authoring

- Added `docs/plan/skills.md`, the expansion of Section 30 and of
  Milestone 8's skills half. The readiness review counted eleven
  inbound references to Section 30 and no expansion under it, and
  called it the largest undesigned area in the corpus. Recorded as
  ADR-0030.
- Corrected the readiness verdict it was written against. Skills did
  not have "no design at all": `tool-system.md:1102-1149` is
  forty-eight lines of real design that settles the metadata
  boundary, the trust label on skill content, the `required_tools`
  check, and the rule that a skill's script is not a tool. This
  document was written to fit inside that section rather than on top
  of it, and the verdict is corrected where it is stated.
- Declared seven types the corpus had never named — `SkillSource`,
  `SkillStatus`, `SkillManifest`, `SkillRevision`, `SkillRef`,
  `SkillPin`, and `CatalogEntry` — with two ports,
  `SkillRepository` and `SkillPackageStore`, and two tables. Nothing
  referenced-and-undeclared was resolved here; unlike the sandbox
  work, these are named for the first time.
- Pinned the catalog at session open. A skill published mid-session
  cannot change what a run already sees, which is what lets the
  byte-stable prefix survive a publish and gives rollback the same
  shape it has for `AgentSpec`.
- Reclassified `skill_manage` from a control tool to a capability
  tool at `risk: HIGH` and `CONDITIONALLY_IDEMPOTENT`, requiring the
  `skill.write` scope and denied when `origin_trust` is below
  `USER`. Section 30.2 calls it a control tool; `tool-system.md`
  draws the control-tool line at durable state that outlives the
  run, and a skill revision is durable tenant state. Three lines of
  `tool-system.md` change and the control-tool table stays at four
  entries.
- Moved the context prefix ceiling from 13,500 to 15,000 tokens and
  added two classes to the assembly order: a pinned catalog capped
  at twenty entries and 1,500 tokens, and loaded skill bodies capped
  at two and 6,000 tokens. A third load fails rather than evicting
  the first, and a loaded body is sticky for the session.
- Added harness case 27, a Milestone 8 case that asserts a skill
  changes the outcome: a second run succeeds where the first fails,
  the first run's prefix contains no part of the body, and the two
  runs' policy dispositions do not differ. Section 30.5 asks for
  this evidence and the twenty-six-case table never carried it. The
  threshold it should be measured against is left open.
- Declared sixteen gates in a new thirteenth registry area, `skill`.
  Registry entries go from one hundred and eighteen to one hundred
  and thirty-four and declarations from one hundred and twenty-one
  to one hundred and thirty-seven. Milestone 8 goes from zero gates
  to ten and Milestone 10 from zero to six — the last two zeros
  belonging to milestones with work in them, and the first gates any
  document has declared at Milestone 10. Case gates go from
  fifty-nine to seventy-one, property from sixteen to seventeen,
  corpus from six to seven, and structural from thirty-seven to
  thirty-nine.
- Widened the gate token grammar from `**M<digit>.**` to
  `**M<number>.**`. Milestone 10 has two digits, and the docs check
  the map specifies would not have parsed it.
- Added `skill.write` to the API scope vocabulary. It appears in no
  route row because the policy engine checks it on a tool call, not
  at the boundary. There is no `skill.read`: reading the catalog is
  what a session already does at open.
- Updated the readiness review, the milestone map, the harness, the
  context engine, the tool system, the policy spec, the API spec,
  the composition root, `AGENTS.md`, `CLAUDE.md`, the toolchain
  document, and `project-state.yaml`. Milestone 8 moves from *split*
  to *ready with named gaps*, and Milestones 0 through 9 are now
  implementable from the documentation as it stands.
- Corrected two citation errors that propagate. The version-pinning
  acceptance criterion is at `engineering-plan.md` line 2690, not
  2684. And the policy-and-approval gating requirement is Section
  30.3, not Section 30.4, which is loading and lifecycle —
  `policy-and-approvals.md` is corrected; ADR-0005 and the questions
  file keep theirs, per the rule that a record states what was true
  when it was written.

## 2026-07-27 — Isolated execution, egress, and the artifact store

- Added `docs/plan/sandbox-isolation.md`, the expansion of the last
  unexpanded plan section whose failure mode is a trust boundary rather
  than a missing feature. It expands Sections 18 and 28 and changes
  neither, and is recorded as ADR-0029. Milestone 6 was the only
  milestone in the corpus with no specification, no gates, and eight
  implement bullets with no design outside the plan.
- Declared the eight types the corpus named and never defined —
  `EnvironmentSpec`, `ResourceLimits`, `EnvironmentHandle`,
  `ExecutionCommand`, `ExecutionResult`, `FileChange`,
  `ArtifactMetadata`, and `ArtifactRef` — together with
  `WorkspaceHandle`, `ArtifactWriter`, and `CredentialResolver` from
  `ToolExecutionContext`, and `KillReason`, which `ExecutionResult`
  needs and nothing had. The API specification had named
  `ArtifactMetadata` and `ArtifactRef` as the last two
  referenced-and-undeclared types in the corpus; there are now none.
- Gave the egress allowlist a grammar, an owner, and two enforcement
  points. `tool-system.md` has depended by name on *"the egress
  allowlist the sandbox spec establishes"* since before there was a
  sandbox spec. The grammar has no open mode and no IP destination
  form, the proxy resolves the name itself and dials the address it
  resolved, and a fixed private-range denylist runs first and cannot be
  waived by an allowlist entry.
- Settled workspace lifetime with a rule rather than a mechanism: the
  workspace is a cache held for a worker's lease on a run, not state
  held for the run's logical lifetime. Cleanup and crash-resume become
  the same operation, and anything that must survive is an artifact.
- Corrected `builtin-tools.md`, which placed `sandbox.run_command` at
  Milestone 5 and twice called Milestone 5 the sandbox milestone.
  Section 21 names Milestone 5 *"HTTP API and SSE"* and Milestone 6
  *"Isolated execution and artifacts"*, and Milestone 6's implement
  list names the tool. Five places are corrected; the constraint the
  document was defending — that `artifact.export` and
  `sandbox.run_command` must not merge into one tool — survives as a
  constraint rather than as a milestone argument.
- Added harness case 26, a Milestone 6 security case for the container
  escape Section 28.7 has required since version 2.0 and the
  twenty-five-case table never carried. None of the twenty-five is
  renumbered, and the rule that no case is ever renumbered is now
  written down.
- Declared thirteen gates in a new twelfth registry area, `sandbox`.
  Registry entries go from one hundred and five to one hundred and
  eighteen and declarations from one hundred and eight to one hundred
  and twenty-one. Milestone 1 goes from twenty-seven gates to
  twenty-eight, Milestone 4 from twelve to thirteen, and Milestone 6
  from zero to eleven — Milestone 8 is now the only zero belonging to a
  milestone that does work. Case gates go from fifty-one to fifty-nine
  and structural from thirty-three to thirty-seven. The milestone map,
  the harness gate table, and the readiness review are updated; the
  ADRs are not, per the rule that an ADR's arithmetic records what was
  true when it was decided.
- Updated the readiness review: Milestone 6 moves from *not ready* to
  *ready*, the four plan sections no specification expands become
  three, two of the four reported conflicts are marked resolved, and
  two of the five open questions are answered.
- Corrected two stale counts in the engineering plan's routing
  paragraphs that predate this work and had missed the secret scanner's
  registration.

## 2026-07-27 — The secret scanner joins the gate registry

- Registered the secret scanner as `gate.structure.no_committed_secrets`
  at Milestone 0. `bootstrap-and-composition.md` specified it in full —
  five rule families, an allowlist that requires a reason, a report that
  never prints the match — and gave it a gate identifier, but the
  identifier's area was `security`, which the identifier grammar in
  `milestone-map.md` does not define, and no registry entry existed. A
  check that fails the build and sits outside the registry is the drift
  the registry exists to prevent, and it was there before any code was
  written.
- Corrected the identifier's area to `structure`. That area is defined
  as the structural statements about the repository that no single
  subject spec owns, which is what a repository-wide scan is. The
  milestone map already said `structure` held three gates while only two
  existed; it now names all three.
- Gave the gate the engineering plan as its owner rather than
  `bootstrap-and-composition.md`, because the plan's Milestone 0
  acceptance criteria already declare it in the same breath as the
  import-boundary walk. `bootstrap-and-composition.md` still declares no
  gates of its own; it supplies the mechanism, not the requirement.
- Registry entries go from one hundred and four to one hundred and five
  and declarations from one hundred and seven to one hundred and eight.
  Milestone 0 goes from eleven gates to twelve and every cumulative
  figure after it rises by one. Structural gates go from thirty-two to
  thirty-three. The milestone map, the harness gate table, and the
  readiness review are updated; ADR-0027 and ADR-0028 are not, per the
  rule that an ADR's arithmetic is a record of what was true when it was
  decided.
- Corrected a stale count of my own: the milestone map's gate 5 required
  a gate identifier's area to be *one of the ten*, which stopped being
  true when the API specification added `api` as an eleventh. The
  grammar block listed eleven and the gate that enforces it said ten.

## 2026-07-27 — The HTTP API, the event stream, and the error vocabulary

- Added `docs/plan/http-api-and-streaming.md`, the specification the readiness
  review named as the single most valuable document not yet written. It expands
  Section 16 of the engineering plan, the three approval routes in
  `policy-and-approvals.md`, and `POST /v1/runs/{id}/input` from ADR-0009 into
  one contract: thirteen routes, each with a request shape, a response shape, a
  status code, a required scope, and an error mapping.
- Wrote response bodies for the twelve routes that had none. Only the session
  created by `POST /v1/sessions` had one anywhere in the corpus.
- Derived the wire error vocabulary from the error taxonomy that already exists
  rather than inventing a second one. Section 16's single worked example,
  `tool_validation_error`, is `ToolValidationError` snake-cased; applying that
  convention to the thirty-one classes produces the code list, and four
  API-specific codes cover conditions that have no domain class. Four classes
  deliberately never cross the boundary, and an unmapped class is
  `internal_error` and 500.
- Specified authentication at what it produces — a `Principal` — and not only
  at what it refuses. No handler reads a tenant from a request. Scopes are
  exact-match strings over a closed dotted vocabulary, with no hierarchy in
  which `run.write` implies `run.read`. A resource in another tenant is 404,
  never 403, and scope is checked before tenancy so that no principal can probe
  for existence by watching 403 become 404.
- Specified the consumer side of the event stream. Transient frames carry no
  `id`, because the EventSource specification advances a client's last-event-ID
  only on a frame that has one, and a synthetic id on a token delta silently
  corrupts every later reconnect. Replay subscribes before it reads, buffers
  arrivals, reads the persisted prefix, and drains the buffer by sequence,
  which is what makes it gapless and duplicate-free.
- Closed the cross-process cancel path. The endpoint writes
  `runs.cancel_requested_at` and returns 202 for a `RUNNING` run, and
  transitions `QUEUED` and both `WAITING_*` states directly to `CANCELLED` with
  200 — which is safe only because those states hold no lease.
- Separated the two mechanisms called "idempotency key": the HTTP header on
  submission, and `tool_invocations.idempotency_key`. Different scopes,
  different tables, different milestones, one unfortunate name.
- Declared `SessionStatus`, which Section 5 references and no document
  declared. Uppercase, to match `RunStatus` and the guarded updates in the DDL.
  Section 16's lowercase sample is read as illustrative and Section 16 is not
  edited; the disagreement is recorded as an open question.
- Added one route, `GET /v1/sessions/{session_id}`, so that a client
  reconnecting with only a session identifier can learn the session's status
  and find its active run.
- Recorded nine contradictions the expansion found, in the document's own
  `Contradictions resolved` table. One of them narrows the readiness review:
  the cross-process cancel path was not unspecified, only its API half was.
- Added `docs/adr/0028-http-api-and-streaming.md` — seventeen decisions, eight
  consequences, fourteen alternatives considered.
- Declared ten hard gates, all at Milestone 5, taking that milestone from one
  gate to eleven and adding an eleventh area, `api`, to the registry. Registry
  entries go from ninety-four to one hundred and four. Updated the milestone
  map's area grammar, gate table, and census, the harness's gate table, and the
  three counts in the engineering plan. ADR-0027 is not updated, because its
  arithmetic is a record of what was true when it was decided.
- Updated `docs/plan/readiness.md`: Milestone 5 moves from plan-only to ready.
  The original finding is kept rather than deleted, because it is the evidence
  for why the document was written and the record of what writing it turned up.

## 2026-07-25 — The readiness review

- Added `docs/plan/readiness.md`, which traces every `#### Implement` bullet,
  acceptance criterion, and milestone subsection in the engineering plan to
  the document that designs it, and states a verdict per milestone. It owns
  no requirement and resolves no conflict.
- The verdict: Milestones 0 through 4 are implementable from the corpus
  alone; Milestone 5 is implementable from the engineering plan but from no
  detailed-design specification; Milestones 6 through 10 are not
  implementable without design work that has not been done.
- The gate census in `milestone-map.md` was used as an independent check on
  every verdict. It was derived mechanically before the review began and it
  agrees with the review everywhere: Milestone 5 registers one gate and
  Milestone 6 registers none, which are the two milestones the tracing found
  least covered.
- Named the two documents that do not exist and should: an API specification
  expanding Section 16, and a sandbox specification expanding Section 28.
  `tool-system.md:977` already depends on the second by name.
- Named the third-largest gap: skills have no design at all. `SKILL.md`
  appears only in the engineering plan and ADR-0013, and the acceptance
  criterion "A selected skill is version-pinned in the run" has nothing
  behind it, while Section 30 is referenced from eleven places as though the
  mechanism were settled.
- Recorded the four plan sections no specification expands — 28 sandbox, 29
  multi-device, 30 self-improving skills, and 31 trajectory export, the last
  of which is fully designed on the consuming side by the evaluation harness
  and not at all on the producing side.
- Recorded that the twenty-five initial evaluation cases carry milestones 1,
  2, 4, 5, and 6 and no others, so Milestones 7, 8, and 9 have acceptance
  criteria with no case backing, and that two criteria — the container-escape
  red-team test and path-traversal rejection — stand on algorithms no
  document specifies.
- Reported four milestone conflicts without resolving any: `sandbox.run_command`
  at Milestone 5 or 6, usage token classes at Milestone 2 or 3, whether the
  HTTP `Idempotency-Key` and the Milestone 1 idempotency port are one
  mechanism, and the container-escape test the case table does not contain.
- Wired the document into the nav and recorded six judgment calls in
  `docs/status/questions-for-review.md`, three of them carrying questions.
- Added a `readiness` block to `docs/status/project-state.yaml` recording which
  milestones the corpus covers and which three documents must exist before
  coding reaches Milestones 5, 6, and 8. The block records coverage; it does not
  authorize work, and `authorized_milestones` is unchanged at 0 and 1. Set
  `last_reviewed_commit`, which had been null since the file was created.
- Rewrote the required reading order in `AGENTS.md` to name the detailed-design
  documents, the milestone map, and the readiness review, and added a routing
  table from subject to document — fourteen rows, plus the statement that the
  HTTP API, sandbox isolation, and skills have no such document. Added a scope
  rule: do not begin an undesigned milestone by inventing the missing design.
- Pointed `CLAUDE.md` and the project-state overview page at the readiness
  review, so an agent checks what is known to be missing before concluding that
  something is missing.

## 2026-07-25 — Gate milestones and the milestone map

- Added `docs/plan/milestone-map.md`, which decides when each stated
  requirement must hold and states no requirement of its own. Recorded as
  ADR-0027. Registry rule 1's docs check is a Milestone 0 deliverable and it
  could not be written against the corpus: the gate section was spelled three
  ways, gates were declared two ways, `milestone` was absent from nine of the
  ten declaring specs, and three gates were declared twice.
- Assigned a milestone to every declared gate. **Eighty-nine** across ten
  specs, one more the engineering plan declares, and seven the map declares
  over the corpus: ninety-seven declarations, **ninety-four registry entries**
  once the three aliases are subtracted. The rule that produced every
  assignment is stated so the next gate added has an answer before the
  argument starts — *a gate lands at the milestone that builds the last thing
  it observes* — with the deliberate exception that a structural check
  vacuously true of an empty repository is registered at Milestone 0.
- Unified the declaration form across ten specs: one heading (`## Hard
  gates`), one form (numbered items with a bolded lead), one suffix
  (`**M<n>.**`). Five headings renamed, four bullet lists converted, and
  seventy-five milestone tokens added. No sentence stating a requirement
  changed.
- Separated gates from tracked metrics. Nine bullets carrying eleven metrics
  moved into new `## Tracked metrics` siblings in five specs, leaving fourteen
  gates in the two lists that had interleaved them. The split was made on the
  specs' own words, not on a judgment about which items sounded gate-like.
- Gave each of the three double-declared gates one owner and an explicit
  alias. The import-boundary walk is owned by the engineering plan's Milestone
  0 — the one registry entry whose owner is not a detailed-design spec —
  and transaction hygiene and checkpoint dispensability are owned by
  `event-log-and-persistence.md`, with `tool-system.md` and `runtime-loop.md`
  restating them.
- Resolved three placements the specs contradicted each other on: the
  idempotency port is Milestone 1 and its unique index is Milestone 2, with
  the in-memory adapter declaring the gap rather than simulating a race;
  Milestone 1 cancellation is a lazily evaluated deadline plus a `SIGINT`
  handler, with `CancelReason` split by dependency across Milestones 1, 2, and
  5; and the context engine's determinism gate moves to Milestone 1, where
  ADR-0024 had already scheduled its subject.
- Added `optional` to the registry for exactly one gate — the model gateway's
  live vendor smoke test — bounded in writing to external credentials.
- Corrected the evaluation harness's own gate table. Its counts covered six
  specs when six existed; it gains rows for the engineering plan and the map,
  and its memory-formation count of seven becomes five. **More than half** of
  the declared gates are not case gates.
- Two findings reported rather than fixed: **Milestones 6 and 8 add no
  gates**, and **thirty-eight of ninety-four gates are green before Milestone
  2**, eleven of them against a repository with no agent in it. The second is
  the argument for building the in-memory tier as real adapters.
- Wired the cross-references: Section 20's stale count corrected, and new
  paragraphs under Section 21, Milestone 0, and Milestone 1. No deliverable or
  acceptance criterion in the plan changed.

## 2026-07-25 — The builtin tools

- Added `docs/plan/builtin-tools.md`, the design for the two tools Milestone 1
  cannot be written without and the classification for the six that arrive
  later. Recorded as ADR-0026. `math.calculate` had five bullets, two of which
  said what not to do; `system.current_time` had four, one of which said
  "Deterministic" about a tool that reads a clock.
- Reconciled the roster. Section 8.1 ends with seven namespaced names and
  Section 8.2 specifies six tools — `demo.external_write` is in 8.2 and not
  8.1, `artifact.export` is in 8.1 and specified nowhere. Read as two rosters
  they contradict; read as a naming convention illustrated by example and a
  list of what the early milestones build, they do not. `tool-system.md`'s
  domain partition already reserves `demo`, which settles which reading was
  intended. The roster is the union: **eight tools**.
- Placed `artifact.export` at **Milestone 6**, the one tool the plan assigns
  to no milestone, with the two rejected alternatives and their reasons.
- Gave every one of the eight a value for every `ToolSpec` field —
  side-effect class, risk, idempotency, trust label, scopes, timeout, output
  ceiling, parallelism, kind, source, and execution target. Hard gate 1
  refuses to start without `output_trust` on every registered spec, and not
  one builtin in the corpus declared one.
- Specified `math.calculate` completely: an eight-production grammar, a
  hand-written tokenizer and precedence-climbing parser rather than an
  allowlist over `ast.parse`, `decimal.Decimal` at fifty significant digits
  with `ROUND_HALF_EVEN`, the operator set with `^` and `**` unified and `//`
  and `%` flooring rather than truncating, ten functions and two constants,
  four bounds, eight reason codes with their checked-in messages, and both
  JSON schemas.
- Made `9**9**9` a failure rather than an outage, and made it one without a
  check in the evaluator: `Emax` and `Emin` at ten thousand with `Overflow`
  and `Underflow` trapped means the decimal context refuses from the exponents
  before a digit is computed.
- Specified `system.current_time` completely: IANA names only, defaulting to
  `UTC` rather than the host's zone, resolved through `zoneinfo` with `tzdata`
  as a declared dependency, five output fields, and an ISO string that always
  carries a numeric offset so it is byte-stable under a fixed clock.
- Relocated Section 8.2's determinism claim to where it can be true. The tool
  is a pure function of `Clock.now()`, the timezone argument, and the timezone
  database; determinism is a property of the port, not of the tool. An
  implementation calling `datetime.now(UTC)` satisfies all four of Section
  8.2's bullets and is untestable.
- Closed an open declaration in `runtime-loop.md`: **`Clock.now()` returns an
  aware `datetime` in UTC**, asserted in the port's contract suite. Every
  consumer so far compared two values from the same clock, so the ambiguity
  was harmless until a consumer converted between zones.
- Closed a hole in hard gate 2. It forbids trust above `EXTERNAL_UNTRUSTED`
  for tools whose `source` is `mcp`, `device`, or `sandbox`;
  `sandbox.run_command` is a builtin with `target_kind = sandbox`, so the gate
  as written did not reach the one builtin that returns bytes produced by code
  we did not write. Restated over both fields.
- Established the failure-message rule for builtins: the reason code carries
  the diagnosis, the message carries the remedy and the supported set, and
  neither carries the input. Hard gate 4's message table is shared with MCP
  tools whose failure text is attacker-controlled, and a table with one
  interpolating entry has no invariant left.
- Specified registration as seven ordered refusals in the composition root's
  freeze phase, pure and testable with nothing else constructed.
- Added nine hard gates, four of them adversarial: a property test that no
  generated expression escapes the eight reason codes, timing assertions on
  `9**9**9` and its neighbours, a differential test pinning `//` and `%`
  against Python's integer operators, and `0.1 + 0.2` returning exactly `0.3`,
  which is the entire argument for `Decimal` in one line.
- Added four additive cross-reference paragraphs to the engineering plan
  (Sections 8 and 8.2, Milestones 1 and 4) and twelve entries to
  `questions-for-review.md`. No requirement was rewritten.

## 2026-07-25 — The development toolchain

- Added `docs/plan/development-toolchain.md`, the design for the nine Milestone
  0 deliverables that were one line each. Recorded as ADR-0025. "Makefile" was
  one word; "Docker Compose with PostgreSQL" named no version, port, volume, or
  database; "CI pipeline" named no workflow file; "Structured logging bootstrap"
  occurred once in the entire documentation set.
- Resolved the collision between **"CI executes `make check`"** and the
  evaluation harness's **four CI jobs**, one of which needs PostgreSQL and one
  of which needs a provider credential. `make check` is `lint typecheck
  test-fast`, `test-fast` is `test-static` followed by `test-contract`, and
  those two are CI jobs 1 and 2 exactly — a partition, not a duplication. CI
  then runs the two jobs that need resources. Both corpus statements are true
  afterward and neither was weakened.
- Resolved the second collision underneath it: Section 21 requires both `make
  test` and `make check`, and if `check` contained `test` it would inherit the
  database requirement and fail on a fresh checkout, contradicting Section 24's
  definition-of-done item that `make check` succeeds. `check` depends on
  `test-fast`; `test` stays the broader local target it reads as.
- Fixed **the governing rule**: CI runs no command the Makefile does not define.
  The workflow file is a schedule and an environment, not a second definition of
  what the project checks. Six targets were added to Section 21's eight for that
  reason alone, and each exists because a CI job invokes it.
- Specified **every Makefile target body**: what all fourteen run, why `db-up`
  polls the compose healthcheck rather than sleeping, and why `db-up` and
  `migrate` stay two commands — Section 25 documents them separately and
  ADR-0024 forbids migrating from the composition root.
- Specified **the compose file**: `postgres:16-alpine` pinned rather than
  floated because the persistence layer depends on `FOR UPDATE SKIP LOCKED`
  semantics, one service, named volume, `pg_isready` healthcheck, and
  credentials that live in `.env.example` and pass the secret scanner by an
  allowlist entry carrying a prose reason.
- Specified **the CI workflow**: one file, four jobs matching the harness, one
  Python version rather than a matrix, `uv` cache keyed on the committed
  lockfile, and a live job that runs on schedule and manual dispatch only —
  never on a pull request, because a fork cannot hold the credential.
  `mkdocs build --strict` runs inside job 1 rather than in a job of its own.
- Specified **structured logging**: structlog, configured in phase 1 of the
  composition root, two renderers keyed on deployment mode, Section 19's eight
  fields bound as context variables at four named points, and `trace_id` read
  from the active span rather than threaded through call sites. Redaction is a
  processor in the chain rather than a convention, with content keys truncated
  to 200 characters instead of dropped.
- Mapped **each of the six test directories to a marker**, which is the piece
  that was missing: the harness had already named `resilience` as the sixth
  category, but nothing said how a category is selected at the command line.
  Three of the six carry `integration` because all three need a database.
- Reconciled Milestone 1's **"Deterministic tests"** with the harness's **"cases
  1 through 11"** as one deliverable. Reading them as two is how a milestone
  acquires a second, informal test framework beside the specified one.
- Resolved **"Initial ADRs"**, which had three defensible readings — the six
  filenames in the Section 4 tree, the eleven a note defers to their milestones,
  or the twenty-five that now exist. Milestone 0 carries the accepted set
  forward whole and authors nothing new; numbering continues from the highest
  number carried over.
- Placed **the Milestone 0 egress block**, previously attributed to that
  milestone only by a non-authoritative status document. It is an autouse pytest
  fixture of about thirty lines — no firewall, no container network policy — and
  it is what turns the harness's "runs without an API key" claim into a test.
- Placed **`docs/security.md` at Milestone 0**. No milestone claimed it, but
  Section 24 requires security implications to be documented for every
  milestone, and Milestone 0 already ships two security controls. This adds no
  deliverable; it identifies where an existing item lands.
- Left **`docs-manifest.yaml` at four sources** and recorded why. Widening the
  single-file HTML publication to the full corpus requires per-document anchor
  prefixing in `scripts/build_docs.py` first: thirty-seven documents share
  heading names like "Decisions", and the anchor generator resolves duplicates
  to the first occurrence, so a naive widening produces cross-references that
  silently point at the wrong document.
- Added six additive cross-reference paragraphs to the canonical plan (Sections
  19, 20.4, 22, 24, 25, and Milestone 0), wired the new spec and ADR into the
  MkDocs nav and the ADR index, and recorded twelve judgment calls in
  `docs/status/questions-for-review.md`.

## 2026-07-25 — Bootstrap, configuration, and the composition root

- Added `docs/plan/bootstrap-and-composition.md`, the design for the process
  that constructs everything the other nine specs describe. Recorded as
  ADR-0024. Nine specifications described nine mechanisms; none described the
  interval before any of them runs.
- Gave **configuration a shape**. The corpus declares 106 knobs and the plan
  names three environment variables. A value is an environment variable if and
  only if it differs between two deployments of the same revision and cannot be
  committed — which leaves eight fields in `Settings` and puts the rest in six
  committed YAML files, each beside the package that reads it, with an optional
  operator overlay directory merged over them.
- Fixed the reading of "New configuration appears in `.env.example`" that would
  have made all 106 knobs environment variables. That reading contradicts
  `policy_version`, which is a hash of the files the rules came from: an
  environment variable that changed an effective rule would leave the hash
  untouched, turning the audit trail from stale into false. **The environment
  never overrides a file; it is interpolated into one at named points**,
  generalizing the `model: ${OPENAI_MODEL}` form Section 10.5 already uses.
- Introduced `DATABASE_URL`, which no document named, for the PostgreSQL
  instance the plan makes the source of truth.
- Assembled **startup order**, stated seventeen times across nine documents and
  composed nowhere. Five phases, ordered by what each may touch: refuse
  (settings only), determinism (`Clock` and `IdFactory`, before anything reads
  ambient time), resources (a session factory, never a session), freeze (every
  versioned asset loaded, hashed, and made immutable, hardline first), and wire.
  All seventeen constraints land in one of the five, and the three that read
  like startup constraints and are not — migrations, provider pinning, contract
  coverage — are named as such.
- Specified `build` as one async context manager whose `Composition` exposes
  application services and nothing else. No adapter, repository, or session
  factory is reachable from an entry point, which is how ADR-0023's reservation
  of `RunRepository.transition` to `runtime/executor.py` survives a second one.
- Gave **Milestone 1's three bodiless bullets** bodies. "In-memory
  repositories" is five adapters in `adapters/persistence/memory.py`, run
  against the same contract suites as their PostgreSQL counterparts rather than
  living under `tests/` as doubles; there is no in-memory `RunQueue`, because
  that port's entire content is `FOR UPDATE SKIP LOCKED` and lease fencing.
  "Inline run dispatcher" is a one-method `RunDispatcher` whose postcondition
  both adapters satisfy. "Minimal context builder" is context-engine
  build-sequence step 1, with its stability test stated as two assertions
  rather than one.
- Resolved **two milestone conflicts** by separating two words each. An event
  *repository* is Milestone 1 and append-only event *storage* is Milestone 2 —
  one port, two implementations. The transaction-hygiene *check* is a Milestone
  0 deliverable and the *gate* is a Milestone 2 criterion, because Milestone 0
  has no database code to walk.
- Completed the **Section 17 CLI contract**: arguments, options, stdout versus
  stderr, six exit codes, and the milestone each command first works at. `get`,
  `events`, and `cancel` are reserved words after `agent run`, with `--` as the
  escape, which is what makes `agent run "get the weather"` decidable.
- Specified the **secret scanner** dependency rule 12 and Milestone 0 both name
  and neither describes: five rule families, a report that never prints what it
  matched, an allowlist whose entries require prose, and `.env.example` scanned
  rather than exempted.
- Added four static checks to the Milestone 0 import-boundary walk, each true
  of an empty repository and still true as it fills: `bootstrap` is imported
  only by the three entry points, no module outside `bootstrap.py` instantiates
  an adapter, no module outside `adapters/determinism.py` reads ambient time or
  generates an identifier, and no `AsyncSession` exists at module scope.
- Annotated the Section 4 tree rather than redrawing it: eleven files added,
  one name retired. `runtime/engine.py` becomes `loop.py`, `executor.py`, and
  `supervisor.py`; `ports/` gains `context.py`, `memory.py`, and
  `determinism.py`; `adapters/models/` gains the Anthropic, OpenAI-chat, and
  local-endpoint adapters ADR-0002 and ADR-0012 require and the tree never
  listed.
- Recorded that **`ModelProvider` was declared twice** with incompatible
  shapes, in Section 7 and in `model-gateway.md`, and made the gateway's
  version canonical. Section 7's is annotated as superseded and left in place,
  the same treatment `RunRepository.claim_next` received.
- Fixed a rendering defect in **Section 5**: the Pydantic note between rules 1
  and 2 split the ordered list, so the fourteen dependency rules rendered as
  1 and then 1 through 13 — meaning every reference to "rule 14" in the
  corpus pointed at a line the reader saw numbered 13. The note is now
  indented as continuation text under rule 1, which is the rule it is about.
  No rule text changed. A sweep of all forty-one built pages found no other
  interrupted list.
- Sixteen further judgment calls are recorded with their reasoning and reversal
  cost in [questions-for-review.md](status/questions-for-review.md).

No product implementation was performed.

## 2026-07-25 — Cross-document defect sweep before the readiness review

- Read the nine detailed-design specs against each other rather than against
  the plan, looking only for places where two documents state the same fact
  differently or where one names something no document declares. **Ten defects
  were found and all ten are fixed.** Each is recorded with its reasoning in
  [questions-for-review.md](status/questions-for-review.md).
- Declared **`RunStatus`** — seven members — which five documents referenced and
  none defined. A run blocked on a child waits in `WAITING_FOR_USER` with a
  suspension record naming the child, rather than in an eighth state that only
  the suspension record could distinguish.
- Added the four **`runs` columns** the domain model always implied and the
  schema never carried: `tenant_id`, `agent_id`, `agent_version`, and a nullable
  `deadline_at` with a partial index restricted to the three live statuses.
- Resolved **`tool_invocations.origin_trust`**, declared nullable by
  `tool-system.md` and `NOT NULL` by `policy-and-approvals.md`, in favour of
  `NOT NULL`. A call the runtime issues itself carries `PLATFORM`. A nullable
  column would let an authorization record say "policy did not compute this",
  which is the one thing it must never be able to say.
- Renamed the classification column to **`idempotency_class`**, because the same
  table already carries `idempotency_key` and crash recovery reads both — one to
  decide whether replay is safe, the other to decide whether it is the same call.
- Corrected the **step identity** claim: `step_number` is canonical on
  `model_calls`, not on `tool_invocations`, because a step that proposes no tool
  calls writes no invocation row and would therefore be skipped by any numbering
  taken from that table.
- Completed the **event catalog** at fifty-one types. It had gone stale at the
  twenty-four of Section 6.8 while five later specs added to it, including seven
  MCP and bridge events and six memory events. Stating the total makes the next
  addition visible.
- Fixed three assertions that name things which do not exist: the harness case
  `approval_granted_resumes_run` now asserts `run.queued` and
  `approval.resolved`, and the gate id standardises on the registry spelling
  `gate.policy.prompt_is_not_authorization`.
- Recorded, without deleting them, that **`RunRepository.claim_next` and
  `heartbeat` are superseded** by `RunQueue.claim` and `RunQueue.heartbeat`. The
  signatures stay in Section 7 as the record of what was replaced.
- Corrected two cross-references in `model-gateway.md` that cited Section 10.3
  for the normalized request; it is Section 10.1.
- Brought every fenced code block in the corpus within the rendered code column.
  Verified by measuring `scrollWidth` against `clientWidth` for every `pre` on
  all thirty-nine built pages rather than by counting characters: **zero blocks
  overflow.** The first-line allowance was re-measured too — the copy control in
  the current Material release sits in its own navigation bar, so a first line
  has the same budget as any other line, not a shorter one.

No product implementation was performed.

## 2026-07-25 — The runtime loop, the step, and the single terminal writer

- Added `docs/plan/runtime-loop.md`, the design for Sections 12 and 13 and the
  loop-facing halves of 6.4, 6.5, 6.9, 14.1, 14.2, 16, 19, 26, and 27. Recorded
  as ADR-0023. This is the last of the eight specs and the seam the other seven
  are called from.
- Established the finding that drives the document: **Section 12.1's forty-two
  lines name eleven callables and not one of them is a declared port.** Section
  7 declares eight port Protocols; the only one the loop touches is
  `context_builder.build`. Each of the other eleven names is a seam where two
  specifications' requirements meet, and at most of them the two disagree.
- Found three places where the loop as written **cannot do what another document
  requires of it.** It cannot resolve its own agent, because
  `engineering-plan.md:1408`, as the plan then stood, reads
  `agents.get_version(run.agent_id, run.agent_version)` and Section 6.3 puts
  both fields on `Session`. It cannot suspend, because
  `engineering-plan.md:1437`, again as it then stood, returns bare
  while Section 27.2 requires the lease released, a checkpoint written, and an
  event emitted — so a run paused for approval holds its lease until expiry, at
  which point the queue hands it to a second worker. And it cannot compact,
  because Section 11.4 requires compaction, Milestone 7 gates it, and no
  pressure measurement or compactor call exists anywhere in the corpus.
- Split the runtime into a **`run_loop` that computes a typed `RunOutcome`** and
  a **`finalize` that performs every terminal action once**. The suspension
  defect is not fixed by adding a `finally` to the flat loop, which leaves the
  next `return` free to reopen it; it is fixed by moving the ending somewhere a
  `return` cannot skip. A structural gate asserts that `RunRepository.transition`
  and `RunQueue.release` are reachable from exactly one module.
- Defined **`Step`** as a runtime value object with a persisted identity — one
  additive column, `model_calls.step_number` — rather than as a counter or a
  table. Steps that produce no tool calls are currently invisible; they are the
  ones a terminal turn is made of.
- Added **nine fields to `Run`**, six of which the event-log spec already
  introduced as columns and never reflected in the domain model, and three of
  which — `agent_id`, `agent_version`, `deadline_at` — are denormalized so the
  run is self-describing and the deadline is indexable by a sweep.
- Gave **compaction a call site**: `build_with_pressure`, which measures, invokes
  the compactor if the body will not fit, adopts the checkpoint it returns, and
  measures again, capped at two per step with `ContextOverflow` permanent on the
  third. `build()` stays pure, which is what the byte-identical rebuild on the
  step-retry path depends on.
- Resolved **budget** into three scopes — run, step, attempt — and ruled that
  Section 6.5's "after" means *record*: usage is recorded and the limit is
  evaluated in one transaction, because splitting them opens a window in which
  the run is over budget and nothing knows.
- Made the **heartbeat a supervisor task** rather than a statement in the loop,
  running at a third of the lease interval and watching three things that are
  all "has the outside world changed its mind": the lease, the deadline, and a
  cancellation request. `heartbeat` returns `bool`; `False` means fenced. Every
  non-append write the loop makes is guarded by
  `WHERE lease_epoch = :lease_epoch`.
- Specified **cancellation** as one token per run with six observation points
  shared by the loop, the tool executor, and the sandbox, and one rule about
  effects: a cancellation observed after a call's `effect_sent_at` watermark is
  set completes the disposition rather than abandoning a half-sent side effect.
- Resolved the **checkpoint's two descriptions** as two types rather than a
  contradiction: Section 6.9's inline `conversation` is what the repository
  returns, and event references and deltas are what it persists. Added the six
  triggers, the `full` rule the call site can evaluate, `checkpoints.full` and
  `checkpoints.base_version`, and `seed_checkpoint` as a function with two call
  sites so a run whose checkpoints are all deleted still reaches the same
  terminal state.
- Specified the **resume ladder**: resumption is a cold process start and a warm
  pipeline entry. The worker claims a `QUEUED` run from scratch; the pipeline
  re-enters at step 6 for each pending call, so a call whose watermark was set
  is not re-executed. `run.resumed` is emitted whenever the execution did not
  start the run, which covers all four paths without enumerating them.
- Ruled that an **empty terminal model turn is a failed step**, retried and
  failing the run with `EmptyModelTurn` on exhaustion, rather than a completed
  run whose final message is empty.
- Added **`FailureReason`**, fourteen values, so a `FAILED` run distinguishes a
  provider outage from an exhausted budget from a policy denial, and named the
  owner of every retry: the adapter's transport retries, the gateway's attempt
  loop, and the runtime's step retry, split on `stream_had_output`.
- Listed and resolved **twenty cross-document contradictions** by number, naming
  the losing side in each. Three needed argument: `RunUsage` has five token
  classes and the event log's "unchanged in shape" is about the rollup
  relationship; Section 6.8's `tool.call.*` names are canonical and the
  evaluation-harness case asserting `tool.proposed` / `tool.authorized` /
  `tool.succeeded` is corrected in that document; and Section 27.5's "reject or
  queue" resolves to reject with HTTP 409, because ADR-0004's partial unique
  index makes queueing impossible at the database level.
- Added fourteen hard gates, twenty-five decisions, the events consolidation
  that introduces no new event type and assigns owners to `run.claimed` and the
  two `run.waiting_*` events, the three-role deployment shape with the sweeps
  under advisory locks, and the transaction boundaries of every write the loop
  makes.
- Wired the nav, the ADR index, and additive cross-references from Sections 12,
  13, and 27 and from Milestones 1, 2, 4, 5, and 7. No requirement was
  rewritten: Section 13's retry table keeps every row and its retryability, the
  Section 8.3 pipeline steps are not reordered, Section 7's `build()` signature
  is unchanged, and 27.1's definitions — including that a turn has no domain
  object — stand.
- Recorded further decisions in `docs/status/questions-for-review.md` and six
  open questions in the spec, of which the two with the highest reversal cost
  are whether cancellation ships in three milestone slices and whether a
  child-run wait deserves its own run status.

No product implementation was performed.

## 2026-07-25 — The evaluation harness, the gate mechanism, and ADR-0001

- Added `docs/plan/evaluation-harness.md`, the design for Section 20 and for the
  gate sections the seven sibling specs close with, plus the parts of Sections
  3, 4, 10.3, 19, 21, 22, and 31 they depend on. Recorded as ADR-0022, with the
  boundary-enforcement half recorded as ADR-0001.
- Added `docs/adr/0001-modular-monolith.md`, closing the numbering gap the ADR
  index had been apologizing for. Section 4's repository layout names the file
  and twenty ADRs referred to a decision documented nowhere. It records the
  modular monolith as accepted, defines replaceability as a port with a contract
  suite rather than as a service boundary, and resolves Section 5's *"where
  practical"* rule by rule: eight of the fourteen dependency rules by import
  graph, four by other static checks, one by the secret scanner, one by
  adapter registration, and two residues named as not mechanically checkable
  with their compensating controls.
- Established that **Section 20's harness cannot run the gates six specs assign
  to it.** The specs declare roughly forty-nine hard gates and the policy spec
  names Section 20 as their enforcer; Section 20's sixteen assertion types can
  express perhaps eight. Sorting the declared gates by kind gives the finding
  that drives the document: **roughly a third of them are not case gates**, so a
  case-only harness reports a green build with a third of the plan's stated
  invariants unchecked.
- Defined a **hard gate** as a named, executable, milestone-attached condition
  that fails the build, and gave gates a checked-in **registry** — one YAML file
  per spec area, carrying the identifier, milestone, kind, a verified link back
  to the declaring document, the prose statement, and the check that implements
  it. A gate declared in a spec but absent from the registry fails the docs
  build; a gate at or past its milestone may not skip.
- Added the three gate kinds the case format cannot express: **property** gates
  with a generator, a predicate, a minimum trial count, and a recorded failing
  seed; **corpus** gates with a `minimum_members` floor so they cannot pass
  vacuously; and **structural** gates that never run the agent. The last two
  need no runtime and are Milestone 0 work.
- Defined **"deterministic"**, which the plan asserts and never defines, by
  naming its seven sources and their treatments: the clock, identifiers, and
  model output are pinned behind ports; batch concurrency and database row
  order are ordered by explicit keys; retry timing is bounded; hash iteration
  order is accepted and never asserted on. Stated the limits as plainly — the
  runtime is not deterministic, payloads are not byte-identical, and the
  deterministic scheduler proves the parallel path produces the right result,
  not that it is race-free.
- Resolved `model_fixture`, which Section 20.1 names with a bare string and
  nothing defines: it resolves to `evals/fixtures/models/NAME.yaml`, validated
  at collection against the current `FakeModelScript` shape rather than at run
  time. Recording is an explicit command and never a side effect of running the
  suite.
- Added **`interventions`** to the case schema — approve, deny, cancel, kill the
  worker, answer, disconnect — without which the eight Section 20.3 cases that
  involve approval, restart, cancellation, or disconnect are unwritable. Every
  field Section 20.1 already had is unchanged.
- Made **"no unauthorized side effects"** decidable. It was the last assertion
  type in Section 20.2 and it asked the harness to prove a negative about a
  system it does not control. It is now asserted against ADR-0021's
  `tool_invocations.effect_sent_at`: an empty `expected.effects` list means no
  invocation in the run set a watermark, and it is the default.
- Ruled that **there is no test mode.** No environment variable, no flag, no
  `if settings.testing` branch in the policy engine, the approval service, or
  the tool executor. Evaluation identity is data — a `tenant_eval` tenant under
  ordinary row-level security, named principals with real scopes, and policy
  profile files loaded by the same loader and subject to the same totality gate.
  An `approve` intervention calls the real approval service as a second
  principal rather than setting a status, and the structural form of the rule is
  that no module under `agent_core` outside `agent_core.evals` may import
  `agent_core.evals`.
- Bound **contract suites to ports rather than to implementations**, so the
  model gateway's second gate — the same contract passing against fake,
  recorded, OpenAI, Anthropic, and `chat_completions` — is one configuration
  line per adapter instead of five files that drift. A port with no contract
  module, or an implementation not registered against its port's contract, fails
  the build.
- Named **`resilience`** as the sixth test category. Section 20.4 lists five;
  Section 4's layout has six directories and two specs already place tests in
  the sixth. Added the routing rule that keeps "integration" from becoming the
  drawer everything slow ends up in, and stated that eval cases are not a
  seventh category.
- Gave every one of Section 20.3's twenty-five cases an **earliest milestone**
  and a statement of what only that case proves. Ten are writable in Milestone
  1, which is what makes "build evaluations before advanced features" a schedule
  rather than an aspiration. Split case 18 into **18a** and **18b** around the
  effect watermark, since only the second — asserting that the model is told the
  outcome is unknown rather than that the call failed — has a safety
  consequence.
- Specified the **capability track's** governance: scenarios have no assertions
  and default to five repeats; a judge is a model, prompt, and rubric versioned
  as one unit and pinned to a provider version, replaced only alongside a bridge
  run, never shown the rubric's weights, and never reusing an identifier after
  deprecation; cross-version score comparison is refused by the tooling. A
  regression is a distribution change — floor drops and policy failures block a
  release, mean drops inside a measured noise band do not — and **a capability
  improvement that increases policy failures is a regression.**
- Ruled that a scenario hitting a cost ceiling is **excluded from the score
  distribution rather than scored zero**, since it conflates "we stopped paying"
  with "the agent failed" in the direction that corrupts the distribution most.
- Designed the **trajectory-to-case conversion** that Section 31.3 asserts and
  nothing specified, and stated its lossy boundary field by field. Recorded tool
  results are replayed rather than re-executed; timestamps, identifiers, usage,
  and cost are discarded; redaction happens at export so there is one place the
  rule lives; and a converted case is marked `source: trajectory` and stays out
  of the blocking suite until a person writes its assertions.
- Added a **flake policy with an expiry**: retry once with the retry rate
  reported even while green, quarantine on a second failure within thirty days,
  and automatic release after fourteen days whether or not the test was fixed.
  Gates may never be quarantined.
- Added ten hard gates of the harness's own, ten build-order steps with the
  first two in Milestone 0, six metrics, four event families, two tables for the
  capability track and none for the deterministic suite, an eight-row
  import-boundary table, and ten failure modes with their mitigations. Gate 7
  asserts the definition of done's eighteenth item by running CI with egress
  blocked rather than by not configuring an API key.
- Wired the nav, the ADR index, and additive cross-references from Sections 2.1,
  5, and 20 and from Milestones 0 and 1. No requirement was rewritten: the
  twenty-five cases stay twenty-five, the sixteen assertion types stay and gain
  four, the capability track stays non-blocking, and Section 5's fourteen
  dependency rules are unchanged.
- Recorded seventeen further decisions in `docs/status/questions-for-review.md`,
  including the ADR renumbering that moves the runtime-loop record to ADR-0023,
  and five open questions in the spec.

No product implementation was performed.

## 2026-07-25 — The tool system, the execution pipeline, and MCP

- Added `docs/plan/tool-system.md`, the design for Sections 7, 8, 12.4, 12.5,
  15's `tool_invocations`, and Milestones 1, 4, 6, and 8: the execution
  pipeline, idempotency and recovery, output limits, control tools, the
  orchestration bridge, and the MCP adapter. Recorded as ADR-0021, constrained
  by ADR-0015, ADR-0008, ADR-0005, ADR-0006, ADR-0002, ADR-0013, and ADR-0020.
- Defined the two types the Section 7 `Tool` port names and nothing in the plan,
  the six sibling specs, or the twenty ADRs declared: **`ToolResult`** with
  `ToolFailure` and `ToolFailureKind`, and **`ToolExecutionContext`** with its
  sixteen fields. A tool returns `ok` and content, never a status, because
  status is the pipeline's judgement and a tool able to claim `denied` could
  launder a denial.
- Ruled that `ToolExecutionContext` carries no database session, no repository,
  no policy engine, and no registry, so Section 12.2's "no transaction across
  tool I/O" is enforced by construction and a tool cannot call a tool.
- Completed `ToolSpec` with `kind`, `target_kind`, `output_trust`, `source`,
  `server_id`, and `deprecated`, and added `ToolKind`, `ToolSource`, and
  `ToolOutcomeStatus`. `output_trust` is **forced** to `EXTERNAL_UNTRUSTED` at
  registration for every MCP, device, and sandbox source, which also resolves
  the plan's two conflicting defaults for `ToolResultItem.trust`.
- Gave the registry a **name grammar and a reserved-domain partition**, and
  named MCP tools `mcp.{server_id}.{normalized_remote_name}` by a deterministic
  five-step normalization. This is the join Milestone 8 needed and lacked:
  without it every discovered MCP tool is an unknown tool and Section 9.2's
  `Unknown tool -> Deny` row makes the milestone non-functional.
- Made the pipeline **fourteen steps, one function, one call site**, preserving
  Section 8.3's ten in order and inserting the four persistence points Section
  12.2 requires. Nothing before step 8 has touched the world; nothing after
  step 10 can undo step 10. The bridge, the device channel, and the MCP adapter
  re-enter this function rather than implementing variants, which is what makes
  ADR-0015's "the bridge is the enforcement point" true rather than aspirational.
- Added **`effect_sent_at`**, a nullable timestamp written immediately before a
  tool's first outbound operation, and turned the event-log spec's undefined
  word *ambiguous* into a fact a recovering worker can read. Section 8.4's rule
  against auto-retrying non-idempotent calls is preserved exactly for calls that
  may have escaped, while a worker that died during argument marshalling is
  retried instead of escalated. The honest limits — that a set watermark proves
  the effect *may* have happened, and that the false-positive case costs one
  human review — are stated in the document rather than left to be discovered.
- Derived the idempotency key from Section 8.4's five inputs plus a version tag
  and `tool_version`, and **excluded `attempt_number`** so a bounded retry
  reuses the key and the external service can deduplicate it.
- Defined where `argument_trust` and `origin_trust` come from, which the policy
  spec consumed and nothing produced. `origin_trust` is the minimum label over
  the request's context items; `argument_trust` defaults to
  `EXTERNAL_UNTRUSTED` and is only ever **raised**, on a verbatim
  sixteen-character match against `USER`-labelled context — a direction chosen
  so the failure mode is an unnecessary approval rather than an injection
  passing as trusted.
- Ruled that large output is **excerpted head-and-tail and artifactized, never
  summarized**, since a summarizer over untrusted tool output is the laundering
  ADR-0020 forbids, and that an excerpt never splits a trust envelope.
- Gave all four non-success outcomes **one six-field shape** with a stable
  `reason_code` and a fixed message, and ruled that external text is data and
  never narration: a remote system's error string is enveloped untrusted
  content, never the outcome `message`.
- Unified the policy spec's repeated-denial breaker and Section 12.5's loop
  detector into **one counter at three thresholds**, adding the rule that an
  invocation which resolved `UNCERTAIN` is never proposed again in the run.
- Defined a **step** as one model call plus the complete disposition of every
  tool call it produced, so a batch shares a `step_number` and a crash-retry
  derives the same keys. Parallelism admits or rejects the whole batch; a mixed
  batch runs sequentially rather than being split, because splitting requires
  the independence inference Section 12.4 warns against.
- Made control tools a declared kind that runs the full pipeline and suspends
  through a nullable marker rather than a new status, so no consumer of
  `tool_invocations.status` learns about suspension and the lease reaper
  excludes them by predicate.
- Specified the MCP adapter to one principle: **a server does not classify
  itself**. `side_effect`, `risk`, `idempotency`, and `required_scopes` come
  from operator configuration, an unclassified server is maximally restricted,
  tenant-configured servers are HTTP-only through the egress allowlist, and
  stdio servers — child processes in the worker's trust zone — are
  operator-configured only.
- Mapped MCP's other surfaces deliberately rather than naturally: resources
  become one synthetic per-server read tool instead of an automatic context
  source, prompts become read-only untrusted skills that never enter the
  cacheable prefix, and sampling and roots are declined at capability
  negotiation so a server never gets to spend a tenant's model budget.
- Extended ADR-0020's pinned-advertisement rule to MCP catalog changes, so
  `tools/list_changed` is recorded and not applied and an external server cannot
  invalidate a tenant's prompt cache at will.
- Gave the orchestration bridge a synthesized `call_id` from the script hash and
  a bridge-counted ordinal so a replayed script deduplicates, with the
  determinism caveat stated plainly, plus a bounded approval hold and a
  per-turn cap on underlying calls.
- Added fourteen columns to `tool_invocations` and two new tables,
  `mcp_servers` and `mcp_tool_catalog`, the second a history rather than a
  cache. Declined to add a `tool_registry_snapshots` table, since the context
  plan's pin already answers what a session advertised.
- Added seven event families, two span children, five metrics, an import
  boundary table, ten hard gates, and a nine-step build order that places most
  of the work in Milestone 1 rather than Milestone 8.
- Cross-referenced the new document from Section 8, Milestone 1, and Milestone
  8's MCP subsection, additively. No requirement in the plan was rewritten,
  weakened, or reordered.

No product implementation was performed.

## 2026-07-25 — Model gateway and the provider-neutral protocol

- Added `docs/plan/model-gateway.md`, the translation design for Section 10 and
  Milestones 1 and 3: the normalized stream, the two first adapters, routing,
  usage and cost, retries, and reasoning. Recorded as ADR-0002, constrained by
  ADR-0006, ADR-0007, ADR-0010, and ADR-0012.
- Defined the roughly twenty types the plan used at call sites and never
  declared: the five `ConversationItem` members, `ContentPart` with its text,
  image and file variants, `PendingToolCall`, the six streaming event classes,
  `ModelUsage`, `CostSource`, `StopReason`, `ModelAttempt`, the three error
  classes, `FakeModelScript` with `ScriptedTurn` and `ScriptedToolCall`, the
  `UsageRepository` port, and `ResolvedModel` with `ModelCapabilities`,
  `ModelLimits`, and `ModelPricing`.
- Stated the **six invariants of the normalized stream** — contiguous sequence,
  exactly one terminal event, contiguous ordered deltas per item, `call_id` and
  `name` known at tool-item start, advisory usage, and no raw provider error
  text or credentials on any event — and gave them a shared validator, so a
  violation is an adapter defect rather than a caller concern.
- Made **one shared assembler** fold events into turns for every adapter, which
  is what makes the contract suite a controlled comparison: the same code
  produces the turn on every provider, so a difference in the turn is a
  difference in the events.
- Resolved the **nine unfilled cells** of the Section 10.2 mapping table without
  changing any mapping it already states: `server_tool_use` becomes a protocol
  error, `content_block_stop` and `message_stop` are structural, `UsageEvent` is
  advisory so its missing OpenAI source stops mattering, `response.incomplete`
  maps to `MAX_TOKENS` where the cap is the cause, and the five OpenAI lifecycle
  events drive item bookkeeping only.
- Gave in-band `<think>` the mapping table ADR-0012 assumed and Section 10 never
  wrote, plus a streaming scrubber with one-token lookahead and a per-profile
  configurable tag pair, since open models do not agree on the delimiter.
- Gave `cache_creation_input_tokens` the home Section 10.2 said to find for it:
  `cache_write_input_tokens`, a **fifth tracked token class** on both
  `ModelUsage` and `RunUsage`. Made `reasoning_tokens` `None` rather than `0`
  where a provider does not itemize it, and moved the itemization question into
  pricing as `reasoning_priced_separately`.
- Added the **`ModelRouter` port**, turning `model_policy` from a bare string
  into a `ResolvedModel` carrying provider, model, capabilities, limits,
  pricing, and a credential reference. This gives `ModelCapabilities` a
  resolution path and gives the context engine's "8,192 or the model's default"
  output reserve its missing second half.
- Resolved **provider pinning against availability routing** temporally rather
  than architecturally: selection happens once at run start, the pin is absolute
  and persisted for the life of the run, and Milestone 10 routes selection and
  never live runs.
- Split **retry ownership on `stream_had_output`**: the adapter retries only
  before the first event reaches the caller, at most three times; after any
  output it fails and the caller decides. `max_attempts = 3` lives in
  application code, matching the worker's existing figure.
- Added the **model-call timeouts** no document carried — `timeout_seconds` and
  the load-bearing `stream_idle_seconds` — and made cancellation produce
  `StopReason.CANCELLED` on a partial turn rather than an error.
- Bounded the **`PLATFORM` trust default** on `ProviderReasoningItem` with four
  properties that leave the label no consumer able to act on it: the payload is
  never parsed, never rendered as prompt text, never reaches the policy engine,
  and never enters memory or a user-facing renderer. The name is still wrong and
  is raised for review rather than edited.
- Made the gateway enforce **tool call and tool result pairing before sending**,
  which the policy spec's denial-as-tool-result and the context engine's
  compaction atomicity both depended on and neither owned.
- Added `model_calls` and `model_prices` to the schema, one row per attempt and
  an append-only price history, so a three-month-old invoice stays reconcilable
  and per-attempt cost is a query rather than log archaeology. `runs.usage` is
  unchanged in shape and becomes a rollup maintained in the same transaction.
- Made **failed attempts count against budget**, checked before each attempt, so
  a crash-looping run stops for a stated reason instead of being mysteriously
  expensive.
- Added event payloads for `model.request.started` and `model.response.completed`
  that the context engine already consumed and Section 6.8 never specified, plus
  three telemetry attributes for the cached, cache-write, and reasoning token
  classes and a `model.attempt` span.
- Specified ten hard gates, six tracked metrics, and a fourteen-step build order
  in which the stream validator and the contract suite are written **before the
  first real adapter**.
- Wrote ADR-0002 with twenty decisions and seventeen rejected alternatives, and
  wired it and the spec into the navigation, the ADR index, and Section 10,
  Section 15, and Milestones 1 and 3 of the engineering plan.
- Recorded twelve judgment calls in `docs/status/questions-for-review.md`,
  flagging the `PLATFORM` trust default as the one worth reading first.
- No product implementation was performed.

## 2026-07-25 — Policy engine and approval lifecycle spec

- Added `docs/plan/policy-and-approvals.md`, the authorization design for
  Sections 8.3, 8.4, 9, 11.2, 13, and 22 and Milestone 4: classification, the
  deterministic matrix, the hardline layer, and the approval lifecycle. Recorded
  as ADR-0005 and ADR-0006, constrained by ADR-0017.
- Defined the five types the plan named as field types but never declared —
  `SideEffectClass`, `RiskLevel`, `IdempotencyClass`, `ProposedAction`, and
  `ApprovalStatus` — deriving each value from an existing plan statement rather
  than inventing a taxonomy: fifteen side-effect classes against Section 9.2's
  fifteen action categories, four idempotency classes against Section 8.4's four
  crash-recovery bullets.
- Rekeyed Section 9.2's matrix on `SideEffectClass` instead of a prose "action
  category" with no referent, and added a **Condition** column so the three cells
  holding non-enum decision strings resolve: "Allow with restrictions" and "Allow
  only in sandbox" become a guarded `ALLOW`, "Deny initially" becomes `DENY` in
  the `default` profile. **No outcome in the matrix changed.**
- Made the evaluator a **pure function** — `evaluate_deterministic(action,
  principal, run, ruleset)` — with time passed in as `evaluated_at` and never
  read from a clock, so the same inputs produce the same decision on replay.
- Established restrictiveness as a **total order combined by `max`**: `ALLOW` <
  `ALLOW_WITH_MODIFICATIONS` < `REQUIRE_APPROVAL` < `DENY`. Section 9.1's "more
  restrictive wins" was undefined across four decision types; it is now a
  computation. Hardline is not a rank in that order but a short-circuit.
- Located the hardline rules that Section 9.3 required but never placed:
  `src/agent_core/policy/hardline.yaml`, packaged, frozen at import, and
  deliberately **not** behind a port — a port implies substitution, and these
  are the rules that must not be substitutable. Every rule carries a mandatory
  `near_miss` it must permit, so an over-broad pattern fails its own test.
- Defined `policy_version`, which `ContextPlan` already consumed and nothing
  produced, as a **content hash** of the profile plus the hardline file
  (`default@3f2a1c9d4e5b+h7c1e0a92`), making rules version-controlled files
  frozen per process rather than rows in a table.
- Generalized approval beyond tool calls with `ActionKind` (tool call, memory
  write, skill authoring, artifact export), since `approvals.tool_invocation_id`
  being `NOT NULL` made the other three structurally unapprovable.
- Gave `approvals` the `tenant_id` and `principal_id` that Milestone 4's
  cross-tenant rejection criterion requires, three indexes, a unique index for
  one open approval per action, and a **guarded resolution** that is
  first-writer-wins: a second caller agreeing gets 200, a second caller
  disagreeing gets 409, and a cross-tenant caller gets not-found rather than
  forbidden.
- Specified the shape of "denial becomes a structured tool result" as a **field
  allowlist** enforced by test, with a repeated-denial circuit breaker at three,
  so a denial teaches the model to stop without teaching it what to evade.
- Mapped Section 22's three trust tiers onto Section 11.2's seven trust labels,
  with only `PLATFORM`, `TRUSTED_CONFIGURATION`, and `USER` able to authorize.
- Added `GET /v1/approvals` and `GET /v1/approvals/{id}`, without which Section
  17's `agent approval list` had no endpoint to call.
- Added ten hard gates, six tracked metrics, and a twelve-step build order to
  Milestone 4, keeping the advisory layer sequenced after Milestone 6.
- Wrote ADR-0006 as **already amended by ADR-0007**, so the distinction between
  "never persist reasoning" and "may hold provider-opaque continuation in the
  checkpoint for the life of a tool loop" lives in the record rather than in one
  trailing sentence of Section 11.4.
- No product implementation was performed.

## 2026-07-24 — Event log and persistence spec

- Added `docs/plan/event-log-and-persistence.md`, the persistence design for
  Sections 6.8, 6.9, 12.2, 14, and 15 and Milestone 2: the append transaction,
  projections, checkpoints, and the run queue. Recorded as ADR-0003 (amended for
  payload versioning) and ADR-0004.
- Stated the layer's contract as **observation, not durability** — a committed
  event no projection ever observed is, to every consumer, an event that did not
  happen — and wrote the hard gates against that definition.
- Identified a **silent missing-write hazard**: two appends take sequences 5 and
  6, the transaction holding 6 commits first, and a projection polling in that
  window advances past 5 and never sees it. The log stays consistent, `UNIQUE` is
  satisfied, and every rebuild reproduces the loss identically.
- Resolved it by establishing that Section 27.5's **one active run per session is
  load-bearing for projection correctness**, not only for contention, enforced by
  a partial unique index, with snapshot-aware watermarking
  (`pg_snapshot_xmin`) specified as the companion change required if that default
  is ever relaxed.
- Made **sequence gaps legal**: a rolled-back append burns its number, so
  consumers read after a watermark and never wait for a specific next sequence.
- Established `LISTEN`/`NOTIFY` as a **latency hint, never a delivery
  guarantee** — it is transactional, so no outbox is needed, and at-most-once, so
  every consumer polls from a watermark first.
- Added `events.payload_schema_version`, required by Section 6.8 and Milestone 2
  but absent from Section 15, together with **pure, total upcasters** that may
  never invent a value, and made an unknown higher version a hard error.
- Gave projections four properties — deterministic, watermarked, rebuildable,
  never authoritative — with state and cursor written in one transaction, and
  gave **derived events deterministic derivation keys** so rebuilds converge
  instead of multiplying their own output.
- Made checkpoints **deltas against periodic full snapshots** with the
  conversation stored as event references, and made *losing checkpoints costs
  time, not information* an executable test.
- Added claim **priority classes** (interactive, async, maintenance) with
  capacity reserved per class rather than aging, which would make latency depend
  on queue history.
- Made every worker write **fenced by `lease_epoch`**, since lease expiry is a
  guess: a zero-row update means stop, not retry, and `heartbeat` returns `False`
  when fenced rather than raising.
- Specified queue-level retry that the plan lacked entirely — only lease expiry
  requeues, `max_attempts` is 3, `runs.failure` is the dead letter — and added
  the `idempotency_keys` table that Section 16 and Milestone 2 both assume.
- Added seven hard gates and five tracked metrics to Milestone 2, four ports
  (`CheckpointRepository`, `ProjectionCursor`, `Projection`, `RunQueue`), and
  four event types.
- Created `docs/status/questions-for-review.md` recording every decision taken
  without review during the plan-completion run, with its reasoning, alternative,
  and reversal cost.
- No product implementation was performed.

## 2026-07-24 — Context engine spec

- Added `docs/plan/context-engine.md`, the assembly design for Section 11 and
  Milestone 7: the cache boundary, the budget allocator, compaction, trust
  rendering, and the working-state lifecycle. Recorded as ADR-0020.
- Split context into **two regions with one membership rule** — if a value *can*
  differ between two requests in the same session, it is not in the prefix.
  Membership is a property of item type, declared in code and asserted at assembly,
  so the current date cannot reach the prefix by looking like configuration.
- Made prompt stability **enforced rather than assumed**: `prefix_sha256` is
  recorded on every request, and a scripted fifty-turn session crossing midnight
  with a revoked tool, corrected memory, and a forced compaction must yield exactly
  one hash. Added **prefix epochs** for changes that cannot be absorbed, with
  epochs-per-session tracked against a target of 1.0.
- Pinned the tool set at session open and moved revocation to call-time policy
  denial, so a permission change does not rewrite the prefix or leak into cache
  timing.
- Gave `ContextBudget` a sizing rule: **only history scales with the context
  window**; every other class is capped absolutely, because prefix content is
  attention paid on every request. The prefix never yields — a class over its
  ceiling fails the session at open rather than truncating the system prompt.
- Fixed the yield order under pressure as in-turn recall, then tool-result
  truncation to typed pointers, then compaction, and made tool call/result pairs
  **atomic budget units**.
- Separated purity from compression: **`build()` is a pure function and compaction
  is a checkpoint write**, which is what makes retries safe and the byte-stability
  gate meaningful.
- Established that **untrusted content is elided, never paraphrased** — summarization
  is a trust-label laundering vector — with typed pointers retaining label, size,
  and reference, and a summary-depth cap of 2.
- Added the nonced trust envelope with delimiter escaping, and the typed
  `context.update_working_state` control tool with per-field carry rules across turn
  boundaries, bounded lists, and constraints that never evict.
- Handed `established_facts` to memory formation as candidates subject to every
  eligibility gate, giving the write path a second input rather than a bypass.
- Added five hard gates (determinism, prefix stability, budget conformance,
  tool-pair integrity, trust preservation) and four tracked metrics to Milestone 7.
- No product implementation was performed.

## 2026-07-24 — Session snapshot budget decided

- Closed the last retrieval open question. The session-open snapshot is capped by
  **item count first and tokens second**, never by a pure percentage of the context
  window: dilution tracks the absolute number of irrelevant items, so a larger window
  is not a reason for a larger snapshot. The percentage survives only as a ceiling.
- Set the starting `core` budgets: **40 items / 1,500 tokens** for interactive
  sessions, 80 / 3,000 for long-running async runs that amortize one block over many
  requests, and 15 / 500 for child runs — each bounded by 2% of the model's window.
- Reserved roughly two-thirds of the item budget for durable user-model and preference
  beliefs and the remainder for the opening-goal priming set, so project-specific
  beliefs cannot evict the "who am I talking to" layer the snapshot exists to carry.
- Made the number self-correcting rather than fixed: snapshot size should be
  **inversely proportional to retrieval quality** and is expected to shrink as the
  query former and ranker improve. Tuning is driven by two signals already present in
  `RecallTrace` — **snapshot utilization** (shrink below about a quarter) and
  **snapshot misses** (grow when in-turn recall keeps re-fetching snapshot-eligible
  beliefs) — which pull in opposite directions by design.
- Added the `Sizing the snapshot` section to the retrieval spec, recorded as ADR-0019
  decision 17, and rejected three alternatives: percentage-of-window sizing, one budget
  for every session type, and growing the snapshot as memory accumulates.
- Both memory specs now carry no open questions; the temporal entity graph remains
  unspecified.
- No product implementation was performed.

## 2026-07-24 — The recall trace becomes a user-inspectable surface

- Resolved the second retrieval open question: **the `RecallTrace` has two
  consumers** — the operator tuning ranking and the user asking why the agent said
  what it said — and both read the **same record**. Two logs would drift, and the
  one shown to the user is the one that must not be wrong.
- Specified that the trace is **recorded in the render pass, never reconstructed**,
  and bound to the exact rendered bytes by `rendered_sha256`. Re-running retrieval
  later returns a different set; a plausible reconstruction of a turn that never
  happened is worse than no answer.
- Defined what a trace may honestly claim: what was **in context**, with cited
  beliefs marked *used* and the rest *available*. It never claims what the model
  attended to.
- Added the user-safe projection (`RecallTraceView` / `TracedBelief`), which carries
  the statement, when and where it was learned, authority and source episode,
  confidence band, and citation, and excludes arm latencies, scores, candidate ids,
  and policy internals — dropped and blocked items are reported as counts only.
- Sensitivity is filtered by the **minimum of the recall surface's and the viewing
  surface's ceiling**, and retention is **two-tier over one record**: operator fields
  expire on the tuning window, user-safe fields live and die with their session.
- Added a `TraceStore` port, three failure modes (trace disagrees with what the model
  saw, trace as a disclosure path, a rejected belief returning after re-derivation),
  and two hard-gate evals: **trace faithfulness** and **correction durability**.
- Made rejection from a trace a **typed, first-class formation input**: not true
  (retire), was true and has changed (supersede), true elsewhere but not here (lower
  portability and record a negative scope override), and unspecified (flag and
  down-weight, never retire). Added `BeliefRejection` and the `MemoryStore` methods
  `reject` and `outstanding_rejections`.
- Established that **rejections are events that re-derivation replays**, matched by
  content rather than belief id since re-derivation mints new ids, and that
  **rejecting is not deleting** — a deletion keeps only a content-hash tombstone.
- Recorded as ADR-0019 decisions 15 and 16 and ADR-0018 decision 15, with build
  sequences updated in both specs so the trace is written faithfully from the first
  commit and rejections exist before re-derivation can violate them.
- No product implementation was performed.

## 2026-07-24 — Beliefs carry across projects

- Resolved the cross-project open question: **beliefs carry from project to project**
  so the agent learns from every project and environment it works in. Scope is split
  into **isolation boundaries** (tenant, principal, sensitivity — hard SQL predicates,
  unchanged) and **relevance boundaries** (project — a ranking and rendering input).
- Added a **portability** class per belief (`portable` / `contextual` / `local`),
  bounded by `belief_type` at formation and lowerable but never raisable by the
  extractor; carried beliefs render with their origin project and at a reduced
  confidence band, and explicit local overrides outrank them.
- Added **promotion by cross-project corroboration** to the formation spec: a belief
  independently observed in two or more project scopes promotes to `user` scope,
  retains every contributing origin, and emits `memory.promoted`. Recorded as
  ADR-0018 decision 14 and ADR-0019 decision 5.
- Added the **false transfer** failure mode with its defenses, and paired
  **transfer-precision / transfer-lift** evaluation metrics.
- Expanded the two remaining retrieval open questions with their tradeoffs: the
  session snapshot budget is attention-bound rather than cost-bound and should be an
  absolute token cap rather than a pure percentage; user-visible retrieval traces are
  restated in terms of the commitments they impose now (a user-safe projection,
  retention, the sensitivity ceiling, and a user-rejection input into formation).
- No product implementation was performed.

## 2026-07-24 — Memory retrieval & ranking spec

- Added `docs/plan/memory-retrieval-and-ranking.md`, the read-path design for
  long-term memory: the three retrieval moments forced by the prompt-stability
  invariant (frozen session snapshot, in-turn recall, child-run recall), query
  formation from working state, the hard scope filter, multi-arm recall fused by
  reciprocal rank, the explicit ranking function, supersession collapse, the safety
  pass, byte-stable rendering, retrieval traces, and the usage-feedback loop back
  into formation. Recorded as ADR-0019.
- Wired the spec and ADR-0019 into the MkDocs navigation and the ADR index, and
  added read-path pointers from Milestone 9 and the formation spec.
- Three open questions are recorded for decision: the session snapshot token
  budget, whether project-scoped beliefs may surface cross-project, and whether
  retrieval traces become a user-facing surface.
- No product implementation was performed.

## 2026-07-24 — Memory formation & consolidation spec

- Added `docs/plan/memory-formation-and-consolidation.md`, the detailed write-path
  design for long-term memory (formation pipeline, conflict resolution with
  bi-temporal supersession, data model, governance, evaluation, and build
  sequence). Recorded as ADR-0018.
- Wired the spec into the MkDocs navigation and the ADR index, and added a pointer from Milestone 9 in the engineering plan.
- Resolved two design decisions in the spec and ADR-0018: memory formation is **fully autonomous from the start** (safety via deterministic eligibility gates, the untrusted-content write ban, and after-the-fact review), and the **builtin consolidation path is built to parity before any external provider**.
- Resolved the remaining formation questions: a **tiered memory model** (a continuous confidence lifecycle plus an explicit working/episodic/semantic/archival hierarchy), the **user model is a projection** over user-scoped beliefs, and **re-derivation is opt-in** per principal.
- No product implementation was performed.

## 2026-07-20 — Documentation system established

- Archived the source Word document to
  `archive/Modular_General_Purpose_AI_Agent_Engineering_Plan.docx` and recorded
  its SHA-256 checksum in `archive/README.md`. Preserved the prior Word revisions
  (v1.0 through v2.3) under `archive/versions/`; the canonical archived document is
  a copy of v2.3.
- Converted the complete engineering plan to canonical Markdown at
  `docs/plan/engineering-plan.md` (Pandoc `docx` → `gfm`, then deterministic
  cleanup: single level-one title, fenced code blocks with language hints,
  normalized tables, and removal of the static Word table of contents and
  title-page artifacts).
- Relocated three security controls — non-bypassable hardline rules, tiered
  credential scrubbing with fail-closed passthrough, and default-deny pairing for
  untrusted inbound surfaces — from the "Revision summary" list to their correct
  home in Section 22, "Security baseline". No requirement text was changed; only
  placement was corrected. The archived `.docx` retains the original placement.
- Created coding-agent instruction files: `AGENTS.md`, `CLAUDE.md`, and
  `.github/copilot-instructions.md`.
- Created machine-readable project state at `docs/status/project-state.yaml`
  (current milestone: 0) and a concise `docs/plan/current-milestone.md`.
- Wired the existing `docs/adr/` records (ADR-0007 through ADR-0017) into the
  documentation site.
- Created the documentation build system: the MkDocs site (`mkdocs.yml`), a
  single-file HTML build (`docs-manifest.yaml`, `scripts/build_docs.py`),
  documentation validation (`scripts/check_docs.py`), `Makefile` targets, and a
  CI workflow (`.github/workflows/docs.yml`).

No product implementation was performed. Milestone 0 has not been started.
