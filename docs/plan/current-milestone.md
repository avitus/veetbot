---
title: Current Milestone
---

# Current milestone

- **Owner-directed source learning — ADR-0233:** On 2026-10-21 the owner
  authorized governed website learning, tentative-memory reinforcement and
  truthful manual-save reporting. This extends Milestone 21 without changing
  frozen extraction policies or moving gates. Production delivery remains pending.

- **Independent reliability repair — ADR-0205:** The owner authorized safe
  failure diagnostics, bounded recovery, cleanup isolation and scoped reliability
  reporting on `codex/app-reliability`, with its own pull request to `main`.
  This repairs existing app behavior without importing the parallel memory
  retrieval workstream or changing its activation gates. Milestone 15 remains
  incomplete; monitoring activation and production delivery remain separate steps.

- **Milestone 33 — General memory search and recall:** the owner authorized
  the six-stage plan on 2026-10-11 under ADR-0178.
  The checkpoint comparison completed all 1,600 observations on 2026-10-19 and
  failed the original recall, invalid-current-fact and per-case cost thresholds.
  The owner approved the separate bounded rollout in ADR-0220, subject to actual
  human findings, production compatibility, final review/CI and verified rollback.
  This authorization does not mark M33 complete or claim deployment readiness.
  The owner's later 2026-10-20 direction replaces pre-activation synthetic case
  review with a daily check-in on real production use under ADR-0223. The new
  version-three trial requires its own authorized release; recall is not enabled
  by that direction. Missing review expires the trial after 24 hours, within a
  fixed seven-day maximum, with all other safety and release gates preserved.
  [General memory recall](general-memory-recall.md) registers twelve gates for
  cross-category evaluation, relevance before candidate limits, measured semantic
  retrieval, task-aware queries, governed source recovery and release evidence.
  The category-independent candidate repair is implemented; the 400-seed diagnostic
  improves recall from 67% to 68.75%, leaving semantic/implicit-use work open.
  The synthetic semantic comparison reaches 97% top-20 candidate recall;
  the local pgvector index and shared experimental recall path are implemented
  with source-version fences (ADR-0179). Bounded provider admission and persistent
  indexing slices are implemented (ADR-0180). Explicit query preparation now feeds
  the local cache under the same durable budget (ADR-0181). Run-bound shared recall
  now binds saved task state and effective query policy (ADR-0182). Context assembly
  now re-reads current evidence instead of retaining completed recall (ADR-0183).
  Release-bundle validation now recomputes cross-category recall and implicit
  answer lift with exact-version admission and revocation (ADR-0185).
  Source indexing now has default-off configuration and maintenance composition,
  bound to independently reviewed release dependencies (ADR-0187). A verified offline
  CPU adapter now supports restricted inputs without remote disclosure, and local
  run authority plus explicit preparation are composed on both recall paths
  (ADR-0188). Governed source recovery now has shared correction fences, source-copy
  erasure, bounded rendering and default-off composition (ADR-0189). Optional query
  preparation now reserves a durable run budget before provider work (ADR-0190).
  A separately admitted relevance reranker has semantic, lexical and linked-record
  regressions (ADR-0191). The development harness now exercises actual runtime
  context and tool results with gold-blind inputs and exact-value scoring; source
  recovery remains distinct from atomic formation (ADR-0192). Bounded deterministic
  task-context composition is implemented as a default-off ablation (ADR-0193).
  The PostgreSQL harness now uses actual elapsed worker time, normal People formation
  and verified paired formation-response banks under one experiment budget
  (ADR-0194). Sparse development controls complete twenty seeded and twenty
  formation observations; they expose retrieval and labeling failures and do not
  pass the release gates. The owner approved the 400-scenario corpus on
  2026-10-13; explicit factual questions are
  separated from implicit tasks, with incident and per-family/split coverage checked.
  The paired pilot exposed a formation@11 defect: named-person statements were
  rejected by the older owner-subject rule, and separate People claims could merge
  under one subject. Source-validated named subjects and whole-statement comparison
  now have regressions without changing formation@9 or People identity admission.
  Shared rendered recall now caps retained deliberate results plus automatic
  base/delta recall at twenty items and two thousand tokens per request, with
  current-source validation and mandatory corrections (ADR-0195).
  Actual M33 searches now consume the durable run allowance, including sparse
  fallback, automatic deltas and People discovery (ADR-0196). Exhaustion returns
  a limitation rather than a false empty search. The paired PostgreSQL pilot
  completes all five arms: sparse 8/20, hybrid 6/20, reranking 18/20, source recovery
  6/20 and combined 18/20 correct answers. This development comparison predates
  the named-People repair and final budget enforcement; it is not activation evidence.
  Offline publication now requires complete original runtime measurements, settled
  provider journals, matching formation banks and independently supplied reviews.
  It recomputes the canonical observations and refuses source or cost discrepancies;
  actual confirmatory dependency binding and reviewed release inputs remain open.
  Current People query narrowing now preserves prepared encoder input and all hard
  restrictions, with identity filtering before sparse and dense limits (ADR-0197).
  Its shared contracts and PostgreSQL checks pass; comparative release evidence
  remains pending.
  Live development failures remain
  retained, including People-path ranking and incomplete formation. Historical
  semantic revisions now have shared visibility, invalidation and dated People
  contracts plus an actual PostgreSQL tool/reader-context regression (ADR-0200).
  Reviewed held-out formation-to-answer evidence remains open. Runtime assembly now records one bounded combined ordering for
  scoring, with unknown or truncated evidence refusing certification. The six-arm
  task-context pilot completes 120 observations: reader-planned search answers
  16/20 versus reranking with a scripted query at 10/20. All four remaining
  reader-planned failures exclude correctly formed claims through guessed hard
  type filters. The model-facing tool now uses soft type hints (ADR-0198), with
  legacy explicit filters preserved. The frozen pilot predates that repair and
  exceeds the exploratory reader-latency ceiling in two arms.
  A separate PostgreSQL load measurement retains 240 retrieval samples across
  100, 1,000, 10,000 and 100,000 synthetic distractors. Every sample ranks and
  renders the older stronger match; fixed vectors exclude encoder/model costs.
  This measures database/retriever load, not end-to-end release latency.
  The owner approved verified-input release binding (ADR-0199). Its implementation
  preserves original observations and rejects changed evaluated inputs across a
  merge. The confirmatory runner now captures deployment composition, verified
  model assets, actual PostgreSQL schema and frozen controls before held-out task
  parsing. Production startup binds the same schema fingerprint.
  Per-observation confirmation now binds all twelve captured runtime variants and
  independent principal attribution before model work. Confirmation preserves the production
  model pin and chat reasoning settings and verifies the actual checkpoint's reader
  digest; the earlier diagnostic reader mode cannot supply confirmation.
  Collection and publication reject unbound or retagged development rows.
  The owner authorized one USD 100
  aggregate cap for the 4,800-observation confirmation; one journal covers every
  variant and interrupted attempts retain their rows and uncertain reservations.
  The owner approved the exact 400-scenario corpus on 2026-10-13; its retained
  independence receipt binds the original corpus and all scenario identities.
  The production-reader sparse baseline completed twenty development observations
  with six correct answers; it does not certify quality. Native historical-index
  load measurement completed 480 samples through 100,000 competing records, with
  every target ranked first and rendered. The first confirmation stopped at 34 of
  4,800 observations because a required test-fixture correction changes its bound
  inputs. Original records and failed status are retained; aggregate reported and
  held cost is USD 5.45365250. The owner subsequently approved a USD 300 aggregate
  cap and one replacement
  after final review and CI; its USD 290 allocation includes no forgiveness of
  earlier charges. Foundation PR #159 deployed successfully as
  `81062c5b`; PR #158 retains review, exact-head CI and deployment requirements.
  The replacement on reviewed/verified `7214a8df` failed at 4 of 4,800 observations:
  an unused automatic preparation reservation expired before a delayed deliberate
  search. The evaluator now isolates deliberate preparation as well as context;
  production budget enforcement is unchanged. Originals remain retained under
  `20261014-confirmation-v2/`, with aggregate reported and held cost USD 5.62063000.
  V3 subsequently failed at 1,217 observations on `74b14815`; its original archive
  and scratch database remain retained. A mandatory incident returned clarification
  despite rendered evidence, and a diagnostic defect obscured the terminal provider
  failure. Aggregate reported and held cost is USD 55.913546. The owner requested
  completion before merging PR #163 on 2026-10-15. ADR-0206 records the scope and
  diagnostic repair; a new complete run may use at most USD 240 after ledger
  reconciliation, CodeRabbit review and exact-head CI/signing. Old measurements
  cannot certify the changed evaluated inputs.
  The approved recovery now isolates invalid interaction proposals from grounded
  claims, preserves named-subject corrections, discovers unambiguous people through
  governed relationship evidence and shares their context with planned recall.
  Retained evidence validates its identity and temporal dependencies, and People
  retrieval preserves prepared semantic authority (ADR-0208). Two isolated
  evaluation workers share one allowance without changing the observation matrix
  or release thresholds (ADR-0207). Focused regressions pass; combined verification,
  review and exact-head CI must precede new paid collection. Reviewed reliability
  and the independent dreaming release are integrated for existing PR #163, with
  migration ancestry ea3310a1b003 → ea3210a1b00d → ea3310a1b004 → ea3310a1b005.
  The later v5 on `f837ed3c` stopped at 124 observations; its diagnostic identifies
  a provider-key-shaped match in OpenAI encrypted continuation, whose original
  meaning remains unknown. The owner approved the narrow ADR-0210 repair on
  2026-10-16. Standalone secrets and other providers/fields retain rejection;
  original replay bytes stay unchanged. Failed attempts remain immutable.
  Aggregate reported and held cost is USD 88.371792 under the unchanged USD 300
  cap. Source verification and synthetic native load do not establish full quality;
  the repaired source needs fresh review, ordinary CI/signing and full evidence.
  ADR-0216 separately authorizes ordinary signing on the final reviewed
  `reliability` commit with a temporary exact-revision context restriction,
  preserving deferred `dev` work and restoring the original restriction afterward.
  Complete confirmation, independent failure review and release publication
  remain outstanding.
  The later v6 on `16153a5f` stopped at 1,239 observations after a cancelled
  formation call. All originals remain immutable; eight ledgers total
  USD 135.90882425 including uncertain reservations, leaving USD 164.09117575.
  The owner approved checkpointed continuation and a focused production comparison
  (ADR-0212): all 400 scenarios, both paths, formation sparse control versus final
  hybrid, or 1,600 required observations. Reuse requires unchanged runtime inputs,
  verified original rows and banks, and complete accounting. The separately verified collector is collecting the remaining positions with
  append-only checkpoints. The source fixes and current production changes are
  being integrated on `codex/recall-production-release`; complete scoring, actual
  independent human failure review and deployment compatibility remain open.
  No quality threshold or independent human review is waived. Work proceeds independently on `codex/memory-retrieval-evaluation`;
  unrelated development and releases are not dependencies or held by this run.
  The owner explicitly authorized the recall pull request when ready, its review,
  hosted verification and production deployment. The separate hold on pushes to
  `dev` remains active. Optional-channel activation remains evidence-gated.
  M16/M32 frozen controls and unattended dreaming requirements remain unchanged.

- **Milestone 32 — Memory reconsolidation (dreaming):** authorized on
  2026-10-02 as a parallel workstream. [memory-reconsolidation.md](memory-reconsolidation.md)
  and ADR-0169 define twenty-four registered gates. The owner authorized ADR-0174
  on 2026-10-10: daily pending proposals, a private web review page, and explicit
  individual approval applying changes to real memory. This supervised path is
  independent of the unchanged unattended quality gate; no further blinded
  review packet is required for this path. The implementation now includes
  bounded durable inventory and provider execution, lossless equivalence merging,
  extractive summaries, independently supported tentative connections, conflict
  history, source invalidation and erasure, and HTTP/CLI/native owner controls.
  Both stores share the operation contracts. Connections use the existing derived
  memory path and retain every original; they remain inferred at confidence at most
  0.35 and expire no later than thirty days after their latest original evidence.
  The native Synthesis journal exposes summaries, connections, conflicts, supporting
  originals and previewed merge undo. Background maintenance now composes one bounded
  proposal/verification slice behind default-off processing and exact release evidence.
  Removing the evidence stops new work and derived recall while preserving history.
  The frozen offline control still measures retrieval only. A separate live command
  freezes answer-bearing original-only observations before measuring merge-only and
  connection-enabled arms, with three repeats, fixed budgets and all failures retained.
  Publication recomputes the observations and requires matching upstream evidence.
  Two earlier comparisons retain their failure evidence. The owner approved
  ADR-0170 on 2026-10-08. Its versioned offline review contract exports blinded
  packets, requires complete human decisions and recomputes scores. A third,
  contract-bound comparison preserved all facts and had zero false merges, but
  still failed answer lift and development merge-answer nonregression. The owner judged
  all fifteen candidates: ten equivalent and five not equivalent. Recomputed
  precision is 75% on development and 57.14% on holdout, below the unchanged 80%
  floor; recall passes both splits. Equivalence review cannot excuse answer failures.
  The October 9 repair rejects hedged original copies and duplicate-claim connections,
  distinguishes summaries in the proposal/verifier instructions, and preserves the best
  visible member's recall rank after merging. Its new comparison records 14/18/14
  development and 17/17/19 holdout answers out of 36. Merge-answer nonregression passes,
  but 0/5.56-point answer lift still fails; eleven new hypotheses remain unverified.
  The owner has stopped repeated review requests; the completed October 8 review
  and real-data usefulness review stay complete. The optional PostgreSQL summary-reader
  repair followed that run,
  so its diagnostic recording is not evidence for the final source tuple.
  The owner approved ADR-0171 on 2026-10-08 for a separate, isolated evaluation
  using newly confirmed fixture text from their own memories. The isolated runner,
  durable experiment budget and local input/output review are implemented. The
  confirmed four-input experiment completed and the owner judged its accepted summary
  useful on 2026-10-09. That qualitative review is finished; it supplies no comparative
  or activation evidence. Any new experiment still requires exact-text confirmation
  naming the provider/model and USD 0.25 ceiling. Production attribution is unchanged.
  Owner-reviewed production mode is live under ADR-0174 after authorized pull
  requests 156 and 155, clean final-code review, pipeline 1066 verification
  (including macOS and iPhone/iPad journeys), and pipeline 1067 delivery of
  `d053d1d9`. The API, documentation and website identify that release; TestFlight
  upload passed. Daily proposals are enabled in all application roles and only
  individual owner approval applies them to memory. The first automatic batch
  deferred with no proposals or provider calls; its next claim is due October 12
  at 01:30 UTC. The second run also deferred without provider calls. The October 11
  diagnosis found legacy erased People heads with missing reverse attribution;
  their existing source refusal remains intact. ADR-0186 implements persistent
  review sign-in and explicit blocker/deferral reporting, pending delivery.
  Authorized metadata-only recovery found no complete surviving identifiers.
  On October 13 the owner authorized forward-only dreaming (ADR-0202): a durable
  cutover excludes existing memories and source events while allowing new evidence
  through existing safety checks. Focused regression coverage passes; production
  cutover awaits verified delivery. Unattended application stays disabled.
  On October 15 the owner authorized an independent dreaming release and its
  dedicated pull request; supervised delivery does not depend on Milestone 33's
  remaining recall evaluations. Migration ea3210a1b00d follows production
  ea3310a1b003, with later recall migrations rebased before their own release.
  That release subsequently completed through PR #165 at `0c7b0d74`: all ten
  hosted checks and every delivery job in pipeline 1141 passed, including
  TestFlight. The public API, documentation and website identify
  `20261015-185949-0c7b0d7`. The forward cutover activated at
  2026-10-15T19:05:44.185661Z with no blockers, preserving all 469 unknown erased
  heads, five historical run receipts and the existing daily schedule. HTTPS
  review-only sign-in works with thirty-day cookie persistence. Supervised review
  remains enabled and unattended application remains off; this operational
  delivery does not supply the missing comparative quality or independent review.
  The once/dry-run operator
  controls, remaining complete runtime gates and fresh matching comparative
  release evidence remain required; no verified sequential ceiling advances.
- **Active milestone:** Milestone 15 — operational hardening — is the next
  sequential authorized milestone, is specified by
  [operational-hardening.md](operational-hardening.md) with sixteen gates, and
  has not started; its backup tranche has no dependency on the milestones
  before it. Milestone 14 — inbound surfaces and pairing, specified by
  [inbound-surfaces.md](inbound-surfaces.md) with twenty-one gates — completed
  on 2026-10-07. A paired message's run has its own USD 10 budget (ADR-0064
  amendment); the sixteen findings pull request 104 left open were resolved
  and pull request 143 carried them to `main`; the owner's production smoke
  with [the Telegram surface runbook](../telegram-surface-runbook.md) passed
  on 2026-10-06; and pull request 153 shipped the smoke's one finding with
  hosted CI and a clean CodeRabbit review. The completion was recorded on
  2026-10-09. Milestone 13 —
  general-purpose subagents and delegation, specified by
  [subagents-and-delegation.md](subagents-and-delegation.md) with twenty-one
  gates — completed on 2026-08-27 when pull request 72 passed hosted CI and a
  clean CodeRabbit review on its final head; the completion was recorded on
  2026-09-28. Milestone 17 — the memory read API and the native memory
  browser — is specified by
  [memory-read-api-and-browser.md](memory-read-api-and-browser.md) with ten
  gates and completed as a second parallel workstream on 2026-08-24 without
  advancing the verified sequential ceiling.
  Milestone 18 — first-class email integration — is specified by
  [email-integration.md](email-integration.md) with seventeen gates and is a
  third parallel workstream on the same terms, its original shared-file
  touches named in ADR-0071 and its operator-managed multi-account extension
  in ADR-0085. Milestone 19 — conversational schedule creation — is
  specified by [scheduling.md](scheduling.md) with five gates and was a fourth
  parallel workstream, limited to one-time creation through the model; it is
  complete. Milestone 20 — calendar recurrence and conversational schedules —
  is specified by [scheduling.md](scheduling.md) with six gates and was a fifth
  parallel workstream, adding monthly and yearly rules and conversational
  creation for all four recurring calendar kinds; it is complete. Milestone 21 — adaptive
  memory distillation — is specified by
  [adaptive-memory-distillation.md](adaptive-memory-distillation.md) with
  thirty-one gates and is a sixth parallel workstream, adding integrated
  episodes, high-recall direct and hypothesis formation, and evidence-based
  forgetting; ADR-0090 widens its existing source-grounding gate to attributed
  communication without changing the gate count. Milestone 22 — the persona surface and curated belief
  promotion — is specified by [persona-surface.md](persona-surface.md) with
  fourteen gates and was a seventh parallel workstream, adding an owner-edited
  trusted prefix row that a formed belief reaches only through explicit
  owner affirmation; it is complete. Milestone 23 — conversational schedule
  lifecycle — is specified by [scheduling.md](scheduling.md) with twelve gates
  and was an eighth parallel workstream, adding summary-only discovery and
  approval-gated update, pause, resume, and terminal cancellation through the
  existing schedule service; it is complete. ADR-0089 separately authorizes current/recent-history native
  browsing and thirty-day maintenance retention for terminal schedule-owned
  state without adding a milestone or conversational deletion tool.
  Milestone 24 — SMS through the owner's iPhone — is specified by
  [device-channel-and-sms.md](device-channel-and-sms.md) with twelve gates
  and is a ninth parallel workstream on the same terms and is in progress,
  its trigger-catalog and registration-source widenings named in ADR-0081.
  Milestone 25 — the WhatsApp business surface — is specified by
  [whatsapp-surface.md](whatsapp-surface.md) with twelve gates and is a
  tenth parallel workstream, implemented on Milestone 14's ports and in
  progress; its receipt-key generalization of the surface seam is
  named in ADR-0082.
- **Milestone 26 — Client modes and adaptive email experience:** authorized on
  2026-09-11 as a new independent parallel workstream under ADR-0092;
  [email-experience.md](email-experience.md) contains the approved complete
  design and thirty-two gates. Twenty-seven backend and native acceptance checks
  are bound; four private quality gates and integrated release evidence remain
  pending, and the milestone remains in progress. Aggregate automatic
  email budgets are USD 20/day and USD 200/rolling thirty days. ADR-0096 limits
  automatic historical analysis to the latest ninety days for now.
- **Milestone 29 — Chat thread folders:** authorized on 2026-09-16 as a new
  independent parallel workstream under ADR-0102;
  [thread-folders.md](thread-folders.md) declares twelve gates for flat,
  single-parent folders over chat conversations, owner-resolved grouping
  proposals from a maintenance pass, grounded model-assisted grouping with a
  deterministic fallback, scoped routes behind a default-off flag, and the
  native sidebar. ADR-0110 amended it on 2026-09-19 to admit the
  [typed-judgment port](typed-judgment.md), the TypeSafe Jev adapter behind it
  and an optional judgment matcher for add-to-folder proposals, all default
  off. All seventeen gates are bound to executable checks, and the milestone
  completed on 2026-09-21 when pull request 127 followed pull request 118
  through hosted CI and a clean CodeRabbit review; the owner had activated
  folders in production on 2026-09-17.
- **Milestone 30 — Advisory approval layer:** authorized on 2026-09-19 as a new
  independent parallel workstream under ADR-0111, the restrictive-only half of
  roadmap item B8; [policy-and-approvals.md](policy-and-approvals.md) declares
  five further gates for an advisor port, a composite engine that can only
  escalate an allowed web or browser network read, a judgment-backed advisor
  that never denies, a deterministic recovery policy in the tool pipeline, and
  observe before enforce. All five gates are bound to executable checks and
  pull request 127 delivered them with a clean review; enforcement stays off in
  the shipped profile, and threshold calibration keeps the milestone in progress.
- **Milestone 31 — Email unsubscribe assistance:** requested by the owner and
  shaped on 2026-09-19 as a new independent parallel workstream under ADR-0112;
  [email-unsubscribe.md](email-unsubscribe.md) declared twenty gates for a
  header-derived census of bulk senders, the authenticated RFC 8058 one-click
  request sent through a dedicated public-HTTPS egress transport to a
  server-derived destination, `mailto:` unsubscribe, Report spam and sender
  cleanup as fixed label actions, owner-gesture consent for a bounded batch,
  scoped routes behind a default-off flag, two Chat tools, and the native
  Subscriptions view. It is implemented with nineteen gates bound to passing
  checks and reached `main` in pull request 131; its flag was on in production
  by 2026-09-21, ahead of the owner's real-mailbox smoke, and simulator UI
  journeys and the release evidence remain open. On 2026-10-08 ADR-0165
  extended it to twenty-three gates with a suspected-spam flag that never
  touches Gmail and thread-level Report spam and Not spam.
- **Verified gate ceiling:** Milestone 14 (289 gates).
- **Authorized workstreams:** Milestones 13 through 15 in order — general-purpose
  subagents and delegation, inbound surfaces and pairing, operational hardening
  (ADR-0061) — plus Milestone 16 memory evaluation and lifecycle as an
  independently advancing parallel workstream whose gate ceiling cannot move
  ahead of Milestone 15 (ADR-0069); it completed on 2026-08-23. Milestone 17,
  the memory read API and the native memory browser, was authorized on
  2026-08-23 as a second such workstream (ADR-0070) and completed on
  2026-08-24 on the same terms, moving the verified ceiling no further.
  Milestone 18, first-class email integration, was authorized on 2026-08-24
  as a third (ADR-0071), on the same terms again. Milestone 19,
  conversational schedule creation, was authorized on 2026-08-24 as a fourth
  (ADR-0072), and Milestone 20, calendar recurrence and conversational
  schedules, was authorized on 2026-08-27 as a fifth (ADR-0073), each without
  advancing the verified ceiling. Milestone 21, adaptive memory distillation,
  was authorized on 2026-08-31 as a sixth (ADR-0077) on the same terms.
  Milestone 22, the persona surface and curated belief promotion, was
  authorized on 2026-09-01 as a seventh (ADR-0079), again without advancing
  the verified ceiling. Milestone 23, conversational schedule lifecycle, was
  authorized on 2026-09-02 as an eighth (ADR-0080) on the same terms and
  expanded to conversational definition update on 2026-09-04 (ADR-0088).
  The owner authorized ADR-0089's non-milestone schedule history and retention
  extension on 2026-09-10.
  Milestone 24, SMS through the owner's iPhone, was authorized on
  2026-08-26 as a ninth (ADR-0081), and Milestone 25, the WhatsApp
  business surface, on 2026-08-26 as a tenth (ADR-0082), on the same
  terms. Milestones 26 through 31 followed as independent workstreams under
  ADR-0092, ADR-0097, ADR-0100, ADR-0102, ADR-0111, and ADR-0112. Milestones
  19, 20, 22, 23, and 29 have completed; their completion was recorded on
  2026-09-28.
- **Deferred:** New model-routing behavior and everything still listed in the
  engineering plan's roadmap subsection. Milestone 21 removes only adaptive
  memory distillation from that residue and Milestone 22 removes only the
  persona surface. Milestone 32 now admits bounded whole-store consolidation,
  belief merging and grounded connections; learned memory policies, semantic
  retrieval, external memory providers, and the remaining roadmap items stay
  unauthorized.
- **Project status:** Milestones 0 through 13 are complete. Through Milestone
  12, all 247 cumulative gates, the full local and PostgreSQL lanes, Apple
  package and simulator lanes, and hosted CI passed on the candidate head, and
  the completed integration was delivered directly to `dev`; code review is
  reserved for the final merge into `main`. Milestone 13 completed next: the
  delegation domain values and limit derivation, the ledger persistence and
  erasure, delegate.run and its one-transaction materializer, the child-run
  suspension, join, and cancel cascade, the delegation limits block with
  tenant admission and the default-off flag, and case 32 with the tools arm
  overlay are implemented, with all twenty-one gates passing locally. Pull
  request 64 passed all five CircleCI lanes, both Xcode Cloud archives,
  GitGuardian, and a finding-free final-head CodeRabbit review before merging
  to `main`, covering the implementation on that head. The capability-bound
  continuation passes locally. A consent-stamped failed long-research run has
  now been captured, re-exported under the corrected `trajectory@5` projection,
  privacy-reviewed, and admitted as the research capability scenario. Its
  first live baseline attempt produced four `cost_usd` ceiling hits before
  judging at costs from USD 0.861420 through USD 0.9954325; its fifth repeat
  ended in a zero-usage model-stream contract failure. No repeat reached the
  judge, proving that the checked-in USD 0.75 per-repeat ceiling cannot produce
  a score on the configured balanced model. The owner subsequently approved a
  USD 50 evaluation budget, now encoded as USD 5 per repeat, USD 25 per suite,
  and USD 50 per day. The next five-repeat baseline persisted five ceiling
  hits at USD 7.8022225 total; the completed delegation arm also persisted five
  tool-call ceiling hits, zero policy failures, and USD 14.8738995 total. Those
  runs exposed and drove regressions for content-free model diagnostics,
  child-suspension polling across both queue classes, bearer-terminology false
  positives, and the zero-tool judge deadlock. Because delegated child usage
  is correctly additive, the scenario now permits 80 model and 200 tool calls
  while retaining every approved cost ceiling. A scored single-agent
  baseline then completed at scores 0.75, 0.55, 0.75, 0.65, and 0.75, with
  mean 0.69, floor 0.55, population variance 0.0064, zero ceiling hits, zero
  policy failures, and USD 7.855529 cost. It correctly remains
  release-blocked because one repeat is below the rubric's 0.60 floor. The
  provider ledger used USD 42.599510 of the initially approved USD 50
  aggregate cap. The owner subsequently raised the aggregate authorization to
  USD 100, now encoded as the daily ceiling while the USD 5 repeat and USD 25
  suite ceilings remain unchanged. A first scored delegating arm on build
  `877b3ec` then scored 0.75, 0.75, 0.775, 0.65, and 0.35 (mean 0.655,
  floor 0.35) at USD 10.984242 and correctly remained blocked below the
  baseline. Its child ledger drove a governed default-limit and synthesis-
  discipline repair. The exact repaired build `3c8772a-delegating-repair`
  scored 0.65, 0.65, 0.90, 0.65, and 0.75, with mean 0.72, floor 0.65,
  population variance 0.0096, zero ceiling hits, zero policy failures, and USD
  11.104456 cost. Its durable suite event reports `release_blocked: false`, and
  the delegating mean exceeds the baseline's 0.69 as ADR-0063 requires.
  Aggregate provider spend is USD 64.688208 of the USD 100 authorization. The
  failed-trajectory baseline remains the comparison evidence. Pull request 72
  then passed every hosted lane and a finding-free final CodeRabbit review on
  its final head `d9827c0b`, and merged on 2026-08-27 as `f34d021e`, completing
  the milestone. On 2026-09-28 `agent eval gates --milestone 13` confirmed all
  twenty-one delegation gates and the cumulative registry, with the seven
  Milestone 6 sandbox gates passing in the hosted real-Docker lane. Milestone
  14 is implemented and in progress with twenty-one registered gates passing
  locally; Milestone 15 remains authorized and specified with sixteen and has
  not started. Milestone 16, the parallel memory-evaluation
  workstream, has implemented all twenty of its gates, republished the
  provider evidence at `formation@8`, and re-recorded its baseline, and completed on 2026-08-23 when hosted CI and
  the CodeRabbit review loop finished clean on the `dev` to `main` pull
  request (merge `571f6d9`); it does not move the verified gate ceiling. Its live benchmark arm ran three times — the first two failed
  only the absolute incomplete-runs condition, at two of 132 probe arms each,
  after which the harness gained a content-free failure class and one retry per
  probe arm, and the third published
  `evals/capability/memory-benchmark-evidence.192a0161d881837218c0ed125c55a121663f8eda.json`
  with four retried runs and a lift of forty-five — which is milestone evidence
  rather than one of the two completion conditions below. Milestone 17, the
  second parallel workstream, completed on 2026-08-24: all ten gates, the
  Python and PostgreSQL lanes, the native Apple package and simulator lanes,
  hosted CI, GitGuardian, and the final CodeRabbit review passed on the
  dev-to-main pull request (merge `3faa978`), with supplemental end-to-end
  browser coverage later passing the same hosted gates in merge `07c8bdf`.
  Milestone 18, the third parallel workstream, has its first-party Gmail MCP
  package, composition, policy, bootstrap ceremony, and monitoring recipe
  implemented, with all seventeen registered gates passing locally, including
  the four operator-managed multi-account gates added under ADR-0085. The
  extension reached `main` in pull request 94 with one CodeRabbit finding
  unresolved, and the owner's mailbox smokes remain outstanding. Milestone
  19, the fourth parallel workstream, completed with its five registered gates
  once pull requests 64 and 66 passed hosted CI and a clean review. Milestone
  20, the fifth, completed with its six gates on pull request 74. Milestone 22,
  the seventh, completed with its fourteen gates on pull request 82: the
  persona document, its trusted prefix row, curated promotion, the routes, the
  CLI, and the native editor. Milestone 23, the eighth, completed with its
  twelve gates on pull requests 89 and 98. Milestone 29 completed on pull
  requests 118 and 127. Each in-progress
  milestone's remaining work is itemized on the
  [milestones page](../status/milestones.md), which `make docs-check`
  reconciles against the project state.

Milestone 10A adds governed foreground skill authoring and an optional,
non-joining background-review child run. Authoring stays disabled by default;
tenant activation remains blocked until the evaluation threshold in the
[skills design](skills.md#rollout-evidence) passes, and that activation is
roadmap item B1 rather than a Milestone 10 completion condition (ADR-0061).
The provider-assisted memory extractor's evidence is accepted under ADR-0057.
ADR-0068's semantic deterministic repair and retry lifecycle advanced the active
policies to deterministic `formation@5` and provider-assisted `formation@6`, and
Milestone 16's admission of working-state established facts advances them again
to `formation@7` and `formation@8` (ADR-0069). Milestone 16 republished the
provider evidence at `formation@8` on the intended production model and deleted
the superseded `formation@4` artifact, so `auto` again activates provider
assistance for the balanced OpenAI `gpt-5.6-sol` and default-profile tuple and
falls back safely for every other one. Tenant activation remains
separate from Milestone completion under ADR-0061. The machine-readable [project
state](../status/project-state.yaml) records progress and evidence.

Milestone 11 was an independent, logically subsequent milestone because adding
scheduling to Milestone 10 would have changed that milestone's established
completion contract. Its [scheduled-runs design](scheduling.md) defines a
versioned schedule, immutable occurrences, deterministic civil time, bounded
misfires, fresh authorization at firing, and atomic creation of an ordinary
durable run; all twenty-three schedule registry entries name real checks, the
production scheduler is a least-privilege role, and scheduling remains
default-off until explicitly activated. Hosted CI and the final CodeRabbit
review passed on head `90e9142`, advancing the verified ceiling through 11.

Milestones 12 through 15 follow the pattern Milestone 11 set: a detailed-design
document and an ADR land first, register the milestone's gates, and only then
does implementation begin. Milestone 12 implementation is complete; its
[notifications-and-devices.md](notifications-and-devices.md) and ADR-0062 have
landed with twenty `gate.device.*` and `gate.notify.*` entries, and
Milestone 13's [subagents-and-delegation.md](subagents-and-delegation.md) and
ADR-0063 with twenty-one `gate.delegate.*` entries, and Milestone 14's
[inbound-surfaces.md](inbound-surfaces.md) and ADR-0064 with twenty-one
`gate.surface.*` entries, and Milestone 15's
[operational-hardening.md](operational-hardening.md) and ADR-0065 with sixteen
`gate.ops.*` entries; no authorized milestone reports a zero row. Milestone
16's [memory-evaluation-and-lifecycle.md](memory-evaluation-and-lifecycle.md)
and ADR-0069 landed the same way, with twenty further `gate.memory.*`
entries in the existing area, and it never showed a zero row because its
authorization and its specification arrived together. Milestone 17's
[memory-read-api-and-browser.md](memory-read-api-and-browser.md) and ADR-0070
landed on the same day as that milestone's authorization, adding ten more
`gate.memory.*` entries to the same area. Milestone 18's
[email-integration.md](email-integration.md) and ADR-0071 landed the same
way on 2026-08-24, adding thirteen `gate.email.*` entries in a new area of
their own; ADR-0085 adds four more for operator-managed multi-account Gmail.
Milestone 19 reuses [scheduling.md](scheduling.md) and the existing
`schedule` area under ADR-0072, adding five gates for the deliberately narrow
one-time creation bridge.
Milestone 20 reuses the same design and gate area under ADR-0073, adding six
gates for calendar recurrence and recurring conversational creation.
Milestone 21's
[adaptive-memory-distillation.md](adaptive-memory-distillation.md) and ADR-0077
add thirty-one `gate.memory.*` entries for integrated episodes, high-recall
formation, evidence-based forgetting, comparative evidence, activation, and
the attributed-communication widening of their source-grounding gate.
Milestone 22's [persona-surface.md](persona-surface.md) and ADR-0079 landed
with that milestone's authorization on 2026-09-01, adding fourteen
`gate.persona.*` entries in a new area of their own for the trusted persona
row, curated promotion, and the persona surfaces.

Milestone 12 — notifications and device identity — completed all eight build
steps. The delivered slice includes the principal-scoped device registry,
transactional content-free outbox, provider-partitioned claim and retry worker,
APNs HTTP/2 adapter, least-privilege `notify` role, seven exact-scope routes,
offline inbox, and native Apple registration and deep-link restoration. Review
hardening made registration request identity deterministic, preserves partial
per-target delivery outcomes, and executes queued navigation plus initial and
changed focus behavior. All twenty gates and all 247 cumulative gates pass;
PostgreSQL, Apple package, iPhone and iPad UI, unsigned Release builds, and hosted
CI pass; the completed integration is delivered directly to `dev`. Production
APNs activation was completed by the owner on 2026-08-23: a signed TestFlight
build registered a physical iPhone as a production target and the authenticated
test-notification route delivered the Test notification alert. That external
activation followed repository completion and leaves the default-off contract
unchanged for fresh deployments.

Authoritative acceptance criteria for every milestone are defined only by the
canonical [engineering plan](engineering-plan.md); this page is a pointer, not a
substitute.

## Authorized work

- [Milestone 0 — Repository and engineering foundation](engineering-plan.md#milestone-0-repository-and-engineering-foundation)
- [Milestone 1 — In-memory vertical slice](engineering-plan.md#milestone-1-in-memory-vertical-slice)
- [Milestone 2 — PostgreSQL persistence and durable worker](engineering-plan.md#milestone-2-postgresql-persistence-and-durable-worker)
- [Milestone 3 — model adapters and normalized streaming](engineering-plan.md#milestone-3-model-adapters-openai-anthropic-openai-compatible-and-normalized-streaming)
- [Milestone 4 — policy, approvals, and complete tool lifecycle](engineering-plan.md#milestone-4-policy-approvals-and-complete-tool-lifecycle)
- [Milestone 5 — HTTP API and SSE](engineering-plan.md#milestone-5-http-api-and-sse)
- [Milestone 6 — isolated execution and artifacts](engineering-plan.md#milestone-6-isolated-execution-and-artifacts)
- [Milestone 7 — context budgeting and structured working state](engineering-plan.md#milestone-7-context-budgeting-and-structured-working-state)
- [Milestone 8 — skills and MCP integration](engineering-plan.md#milestone-8-skills-and-mcp-integration)
- [Milestone 9 — long-term memory and knowledge retrieval](engineering-plan.md#milestone-9-long-term-memory-and-knowledge-retrieval)
- [Milestone 10 — memory maturation](engineering-plan.md#memory-maturation)
- [Milestone 10A — self-authored skills](engineering-plan.md#self-authored-skills-authorized-tranche)
- [Milestone 10B — web access](engineering-plan.md#32-web-access)
- [Milestone 10C — authenticated browser automation](engineering-plan.md#33-authenticated-browser-automation)
- [Milestone 11 — scheduled runs](engineering-plan.md#milestone-11-scheduled-runs)
- [Milestone 12 — notifications and device identity](engineering-plan.md#milestone-12-notifications-and-device-identity)
- [Milestone 13 — general-purpose subagents and delegation](engineering-plan.md#milestone-13-general-purpose-subagents-and-delegation)
- [Milestone 14 — inbound surfaces and pairing](engineering-plan.md#milestone-14-inbound-surfaces-and-pairing)
- [Milestone 15 — operational hardening](engineering-plan.md#milestone-15-operational-hardening)
- [Milestone 16 — memory evaluation and lifecycle](engineering-plan.md#milestone-16-memory-evaluation-and-lifecycle)
- [Milestone 17 — memory read API and browser](engineering-plan.md#milestone-17-memory-read-api-and-browser)
- [Milestone 18 — first-class email integration](engineering-plan.md#milestone-18-first-class-email-integration)
- [Milestone 19 — conversational schedule creation](engineering-plan.md#milestone-19-conversational-schedule-creation)
- [Milestone 20 — calendar recurrence and conversational schedules](engineering-plan.md#milestone-20-calendar-recurrence-and-conversational-schedules)
- [Milestone 21 — adaptive memory distillation](engineering-plan.md#milestone-21-adaptive-memory-distillation)
- [Milestone 22 — persona surface and curated belief promotion](engineering-plan.md#milestone-22-persona-surface-and-curated-belief-promotion)
- [Milestone 23 — conversational schedule lifecycle](engineering-plan.md#milestone-23-conversational-schedule-lifecycle)
- [Milestone 24 — SMS through the owner's iPhone](engineering-plan.md#milestone-24-sms-through-the-owners-iphone)
- [Milestone 25 — WhatsApp business surface](engineering-plan.md#milestone-25-whatsapp-business-surface)
- [Roadmap beyond Milestone 15](engineering-plan.md#roadmap-beyond-milestone-15)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

The Milestone 10 workstreams are independently deliverable because they do
not share a delivery dependency. The self-authored-skills contract is Section
30.6, the six `gate.skill.*` entries, and the definition of done. The
memory-formation specification supplies fifteen memory-maturation gates for
ordinary conversation and lifecycle, governed inspection, and the
evaluation-gated provider path. The web-access tranche uses Section 32.3 and
the seven formal gates in [web-access.md](web-access.md#hard-gates); the
browser tranche uses Section 33.3 and the ten gates in
[browser-automation.md](browser-automation.md#hard-gates). The Milestone 11
contract is its twenty-three `gate.schedule.*` entries plus the acceptance
criteria in the engineering plan and the [scheduling design](scheduling.md).

Milestone 12's contract is its twenty `gate.device.*` and `gate.notify.*`
entries plus the acceptance criteria in the engineering plan and the
[notifications-and-devices design](notifications-and-devices.md); Milestone
13's is its twenty-one `gate.delegate.*` entries plus the plan's acceptance
criteria and the [delegation design](subagents-and-delegation.md), with tenant
activation gated on the capability-scenario evidence; Milestone 14's is its
twenty-one `gate.surface.*` entries plus the plan's acceptance criteria and
the [inbound-surfaces design](inbound-surfaces.md); Milestone 15's is its
sixteen `gate.ops.*` entries plus the plan's acceptance criteria and the
[operational-hardening design](operational-hardening.md); and Milestone 16's
is its twenty `gate.memory.*` entries plus the plan's acceptance criteria
and the
[memory-evaluation-and-lifecycle design](memory-evaluation-and-lifecycle.md),
whose benchmark baseline is re-recorded deliberately by every change that
moves it. Milestone 17's contract is its ten `gate.memory.*` entries —
`read_api_ceiling_required`, `read_api_ceiling_filter`,
`read_api_principal_isolation`, `read_api_pagination`, `read_api_filters`,
`read_api_read_only`, `read_api_flag_absent`, `browse_contract_parity`,
`read_api_view_projection`, and `read_api_error_vocabulary` — plus the plan's
acceptance criteria and the
[memory-read-api-and-browser design](memory-read-api-and-browser.md).
Milestone 18's contract is its seventeen `gate.email.*` entries plus the
plan's acceptance criteria and the
[email-integration design](email-integration.md). Milestone 19's contract is
its five new `gate.schedule.*` entries plus the plan's acceptance criteria and
the [scheduling design](scheduling.md#model-callable-creation). Milestone 20's
contract is its six new `gate.schedule.*` entries plus the plan's acceptance
criteria and the same scheduling design.
Milestone 21's contract is its thirty-one new `gate.memory.*` entries plus the
plan's acceptance criteria and the
[adaptive-memory-distillation design](adaptive-memory-distillation.md), including
ADR-0090's attributed-communication amendment to source grounding.
Milestone 22's contract is its fourteen `gate.persona.*` entries plus the
plan's acceptance criteria and the [persona-surface design](persona-surface.md).

- [Milestone 26 — Client modes and adaptive email experience](engineering-plan.md#milestone-26-client-modes-and-adaptive-email-experience)
- [Milestone 27 — Bland calling and public reception](engineering-plan.md#milestone-27-bland-calling-and-public-reception)
- [Milestone 28 — People and relationship memory](engineering-plan.md#milestone-28-people-and-relationship-memory)
- [Milestone 29 — Chat thread folders](engineering-plan.md#milestone-29-chat-thread-folders)
- [Milestone 30 — Advisory approval layer](engineering-plan.md#milestone-30-advisory-approval-layer)
- [Milestone 31 — Email unsubscribe assistance](engineering-plan.md#milestone-31-email-unsubscribe-assistance)

## Completion rule

Milestone 10 completed after all thirty-eight Milestone 10 gates and all 204
cumulative gates passed, all required CI lanes passed on the final head, and the
final CodeRabbit review had no finding or unresolved conversation. Enabling
authoring for a tenant is separately governed by the rollout evidence rule and
is not a completion condition (ADR-0061). Partial work does not advance the
verified gate ceiling.

Milestone 11 completed after all twenty-three scheduling gates and all 227
cumulative gates passed, the PostgreSQL integration and resilience lanes passed,
the hosted CI lanes passed on the final head, and the final CodeRabbit review
had no finding or unresolved conversation.

Milestones 12 through 15 complete, in order, when every gate their specification
declares and the cumulative registry pass, the PostgreSQL lanes pass where the
milestone touches persistence, hosted CI passes on the final head, and the final
CodeRabbit review is clean. The verified ceiling advances through each only
after every earlier milestone has completed.

Milestone 13 completed on those terms on 2026-08-27: all twenty-one delegation
gates and the cumulative registry passed, the PostgreSQL lane passed, and hosted
CI and a finding-free final CodeRabbit review passed on pull request 72's final
head. It was recorded on 2026-09-28, advancing the verified ceiling to
Milestone 13 and 268 gates.

Milestone 14 completed on those terms on 2026-10-07: all twenty-one surface
gates and the cumulative registry passed, the PostgreSQL lane passed, the
owner's production Telegram smoke passed on 2026-10-06, and hosted CI and a
clean final CodeRabbit review passed on pull request 153's final head, which
carried the smoke's one repair. It was recorded on 2026-10-09, advancing the
verified ceiling to Milestone 14 and 289 gates.

Milestone 16 completes on the same terms, with its own additional condition:
the checked-in benchmark baseline equals a fresh deterministic run exactly and
the provider-assisted extraction evidence has been republished at its new
policy version. Being a parallel workstream changes nothing about the ceiling,
which still advances only after every earlier milestone has completed.

Milestone 16 completed on those terms on 2026-08-23: every declared gate and
the cumulative registry passed, the PostgreSQL lanes passed, and hosted CI and
the final CodeRabbit review finished clean on the `dev` to `main` pull request.

Milestone 17 completes on the same terms, with its own additional condition:
the native Apple package and simulator lanes pass, because the browser is half
of what the milestone delivers and no Python gate observes Swift. Being a
parallel workstream changes nothing about the ceiling, which still advances
only after every earlier milestone has completed.

Milestone 17 completed on those terms on 2026-08-24: every declared gate and
the cumulative registry passed, the PostgreSQL and native Apple lanes passed,
and hosted CI, GitGuardian, and the final CodeRabbit review finished clean on
dev-to-main pull request 58. Pull request 64 subsequently added end-to-end
PostgreSQL pagination and iPhone/iPad navigation coverage with the same hosted
lanes green and no unresolved review conversation.

Milestone 18 completes on the same terms, with its own additional condition:
the owner's real-mailbox smoke — bootstrap consent, a scheduled triage run,
an approval delivered to the phone, and one approved send — is recorded as
evidence, because the mailbox is an external system no gate's fake can vouch
for. Being a parallel workstream changes nothing about the ceiling, which
still advances only after every earlier milestone has completed.

Milestone 19 completes when its five gates and the cumulative registry pass,
all relevant local and hosted lanes pass on the final head, and the final
CodeRabbit review is clean. Because it is a parallel workstream, completion
does not advance the verified gate ceiling past the sequential milestones. It
completed on those terms with pull requests 64 and 66 on 2026-08-25.

Milestone 20 completes when its six gates and the cumulative registry pass,
all relevant local and PostgreSQL lanes pass, hosted CI passes on the final
head, and the final CodeRabbit review is clean. Because it is a parallel
workstream, completion does not advance the verified gate ceiling past the
sequential milestones. It completed on those terms with pull request 74 on
2026-08-28.

Milestone 21 completes when its thirty-one gates and the cumulative registry
pass, comparative `formation@7`/`formation@8`/`formation@9` evidence for the
intended production tuple is published, all relevant local, PostgreSQL, and
hosted lanes pass on the final head, and the final CodeRabbit review is clean.
Because it is a parallel workstream, completion does not advance the verified
gate ceiling past the sequential milestones.

Milestone 22 completes when its fourteen gates and the cumulative registry
pass, the deterministic benchmark baseline has been re-recorded by the change
that implements snapshot de-duplication, the native Apple lanes pass because
the persona editor is half of what the milestone delivers, all relevant
local, PostgreSQL, and hosted lanes pass on the final head, and the final
CodeRabbit review is clean. Because it is a parallel workstream, completion
does not advance the verified gate ceiling past the sequential milestones. It
completed on those terms with pull request 82 on 2026-09-01.

Milestone 23 completed when its twelve gates and the cumulative registry
passed, the PostgreSQL lane passed, and hosted CI and a clean final CodeRabbit
review passed on pull requests 89 and 98, the second merged on 2026-09-06.

Milestone 26 completes only when all thirty-two gates and approved quality
requirements, relevant local/PostgreSQL/native lanes, authorized owner mailbox
smoke, final-head hosted review and production delivery evidence pass. Until then
its status remains in progress; it does not move the verified sequential ceiling.

## Milestone 27: Bland calling and public reception

The owner approved [bland-calling.md](bland-calling.md) on 2026-09-11 under
ADR-0097. Fourteen gates are registered. This independent workstream remains
in progress. Calling has been active in production since 2026-09-17, and every
pull request that carried it to `main` passed hosted CI and a clean review.

Local code now implements the scoped tool rosters, approval-bound dispatch,
public correspondence intake, retained owner results, erasure and call-result
notifications. Twelve gates have executable offline bindings; provider admission
and live release remain pending, and binding them will set the final head that
the exact-head review needs. [Setup](../bland-setup.md) covers the remaining
owner configuration and external verification.

## Milestone 28: People and relationship memory

The owner authorized implementation on 2026-09-15 under ADR-0100.
[People and relationship memory](people-and-relationships.md) specifies
36 gates, stable identities, temporal relationships, source-linked history,
governed formation/retrieval, correction/erasure and the native People surface.
This independent workstream is in progress and does not move the verified
ceiling; none of its thirty-six gates is yet bound to an executable check.

ADR-0101 removes evaluation-dependent People availability; the complete implemented
feature is enabled by default while quality measurement continues.

## Milestone 29: Chat thread folders

The owner authorized implementation on 2026-09-16 under ADR-0102.
[Chat thread folders](thread-folders.md) specifies twelve gates: flat,
single-parent folders as server-owned session state; owner-resolved grouping
proposals with a bounded open set, durable decline and withdrawal; grounded
model-assisted grouping with a deterministic lexical fallback; nine routes on
the session scopes behind a default-off flag; and native sidebar folder
sections with proposal review. The implementation landed on 2026-09-16 with
every gate bound to an executable check, and pull request 118 carried it to
`main` with hosted CI and a clean review; the owner activated it in production
on 2026-09-17.

ADR-0110 amended the milestone on 2026-09-19. [Typed judgment](typed-judgment.md)
specifies a provider-neutral port for closed, typed questions with the TypeSafe
Jev adapter as its first provider and declares four `gate.judgment.*` gates;
[Chat thread folders](thread-folders.md) gains a thirteenth gate for an
optional judgment matcher that decides which existing folder an unfiled
conversation belongs in. Both are default off, the matcher only ever proposes,
and any failure returns the existing grouping unchanged. The same ADR admits
an offline, non-activating email-importance evaluation under Milestone 26. The
port, the adapter, the fake and the selector landed on 2026-09-20 with the
four `gate.judgment.*` gates bound to executable checks, and the judgment
matcher landed the same day with `gate.folder.judgment_matching` bound to a
composed end-to-end check. No judgment request is made in a deployment until
the owner sets the selector, the key, and the matcher's knob. Pull request 127
carried the amendment to `main` with hosted CI and a clean review on
2026-09-21, which completed this independent workstream.

## Milestone 30: Advisory approval layer

The owner authorized implementation on 2026-09-19 under ADR-0111, the policy
ADR roadmap item B8 requires for its second half.
[Policy and approvals](policy-and-approvals.md) specifies five further gates:
the composite engine cannot lower a rank; the advisor runs on allow paths only
and once per invocation; an unavailable advisor abstains; the advisor is blind
to the rules and sees only redacted, delimited arguments; and the layer off or
observing changes no decision. The first advisor uses the
[typed-judgment port](typed-judgment.md) rather than the model gateway, a
divergence ADR-0111 records, and the tool pipeline gains a deterministic
recovery policy. General standing approval grants stay on the roadmap. The
implementation landed on 2026-09-20 with every gate bound to an executable
check. Observing is the environment flag
`AGENT_POLICY_ADVISORY_OBSERVE_ENABLED`, which moves no policy version;
enforcing is the profile value `advisory.enabled`, which does and therefore
unbinds the memory-formation release evidence until it is regenerated
(ADR-0111, amended 2026-09-20). Pull request 127 delivered it with hosted CI
and a clean review; threshold calibration and the final-head review remain.
This independent workstream is in progress and does not move the verified
ceiling.

## Milestone 31: Email unsubscribe assistance

The owner requested this capability on 2026-09-19 and decided its surfaces,
consent model, egress and fallbacks under ADR-0112.
[Email unsubscribe assistance](email-unsubscribe.md) specified twenty gates: a
deterministic census of bulk senders read from header metadata inside the
existing foreground refresh; the RFC 8058 one-click request, offered only for
mail Gmail authenticated and sent by one builtin tool whose destination is
server-derived and never an argument; a dedicated public-HTTPS egress
transport for that tool alone; the header's `mailto:` message through the
account's send server; Report spam, Not spam and sender cleanup as fixed label
deltas; owner-gesture consent for a batch of at most twenty-five senders and
one by-value approval per batch in Chat; four routes behind a default-off
flag; and the native Subscriptions view and thread action. The owner directed
implementation on 2026-09-19. Nineteen gates bind passing checks, and pull
request 131 delivered it with hosted CI and a clean review; its flag was on in
production by 2026-09-21. Simulator UI journeys, the owner's real-mailbox
smoke and integrated release evidence remain open. On 2026-10-08 the owner
extended the milestone under ADR-0165 with three more gates: a spam verdict in
the importance assessment, informed by Gmail's own DMARC result, flags
suspected spam without touching Gmail; the owner clears a flag or reports one
thread as spam with Archive's gesture, through two routes under
`/v1/email/threads` behind the same flag. This independent workstream is in
progress and does not move the verified ceiling.
