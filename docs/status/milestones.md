---
title: Milestones
---

# Milestones

This page is the human-readable summary of every milestone and the work still
open. It is a projection of the authoritative, machine-readable
[`project-state.yaml`](project-state.yaml): `make docs-check` fails when a
milestone appears here under the wrong group, under a stale title, or with a
checklist that disagrees with that file's `open_items`. Update the state file
first; this page follows it.

The **verified gate ceiling is Milestone 14** (289 cumulative gates). The
parallel workstreams below advance their own gates independently but never
move that ceiling; it advances only when Milestone 15 completes. Which gate belongs to which milestone is the
[milestone map](../plan/milestone-map.md); the authoritative acceptance
criteria live in the [engineering plan](../plan/engineering-plan.md).

## At a glance

State reviewed on 2026-09-28 against `main` at `e55551fa`, which production
runs as release `20260928-202932-e55551f`; Milestone 14's completion was
recorded on 2026-10-09. With Milestone 32 authorized, twenty-two of thirty-three milestones are complete; ten
are in progress and one is authorized but not started.
Milestone 32 has implemented components and registered gates;
comparative quality and complete runtime/release evidence remain pending.

| Milestone | State | What remains | Who acts |
| --- | --- | --- | --- |
| 15 Operational hardening | Not started | All sixteen gates | Engineering, then owner |
| 18 Email integration | In progress | Mailbox smokes; 1 open review finding | Owner; engineering |
| 21 Memory distillation | In progress | Final-head review; production formation@9 check; Sol/Astra study | Engineering; owner |
| 24 SMS | In progress | Physical-iPhone verification | Owner |
| 25 WhatsApp | In progress | Meta ceremony, live smoke | Owner |
| 26 Email experience | In progress | Private evaluations, cost calibration, two-account acceptance; expired-body drafting | Owner with engineering |
| 27 Bland calling | In progress | Dashboard and live checks; final-head review | Owner; engineering |
| 28 People | In progress | All 36 gates unbound; verification, evaluations, review | Engineering; owner |
| 30 Advisory approval | In progress | Threshold calibration and enforce decision; final-head review | Owner; engineering |
| 31 Email unsubscribe | In progress | Simulator journeys, mailbox smoke; final-head review | Engineering; owner |
| 32 Memory reconsolidation | In progress | Measured quality failures, remaining integrated gates and release evidence | Engineering |

## Complete

- **Milestone 0 — Repository and engineering foundation**
- **Milestone 1 — In-memory vertical slice**
- **Milestone 2 — PostgreSQL persistence and durable worker**
- **Milestone 3 — Model adapters (OpenAI, Anthropic, OpenAI-compatible) and normalized streaming**
- **Milestone 4 — Policy, approvals, and complete tool lifecycle**
- **Milestone 5 — HTTP API and SSE**
- **Milestone 6 — Isolated execution and artifacts**
- **Milestone 7 — Context budgeting and structured working state**
- **Milestone 8 — Skills and MCP integration**
- **Milestone 9 — Long-term memory and knowledge retrieval**
- **Milestone 10 — Automatic memory, skills, public-web, and authenticated-browser tranches**
- **Milestone 11 — Scheduled runs**
- **Milestone 12 — Notifications and device identity** — completed with
  production APNs delivery owner-verified on a physical iPhone.
- **Milestone 13 — General-purpose subagents and delegation** — completed on
  2026-08-27, when pull request 72 passed hosted CI and a clean CodeRabbit
  review on its final head; the delegating arm scored above the failed
  single-agent baseline. Recorded on 2026-09-28, moving the verified ceiling
  to Milestone 13.
- **Milestone 14 — Inbound surfaces and pairing** — completed on 2026-10-07:
  the owner's production Telegram smoke passed on 2026-10-06, and pull request
  153 shipped its one finding with hosted CI and a clean CodeRabbit review.
  Recorded on 2026-10-09, moving the verified ceiling to Milestone 14.
- **Milestone 16 — Memory evaluation and lifecycle** — the first parallel
  workstream to complete; hosted review finished clean on 2026-08-23.
- **Milestone 17 — Memory read API and browser** — the second completed
  parallel workstream; hosted review finished clean on 2026-08-24, followed by
  supplemental end-to-end PostgreSQL and native navigation coverage.
- **Milestone 19 — Conversational schedule creation** — hosted CI and review
  finished clean on pull requests 64 and 66 (2026-08-25).
- **Milestone 20 — Calendar recurrence and conversational schedules** — hosted
  CI and review finished clean on pull request 74 (2026-08-28).
- **Milestone 22 — Persona surface and curated belief promotion** — hosted CI
  and review finished clean on pull request 82 (2026-09-01).
- **Milestone 23 — Conversational schedule lifecycle** — hosted CI and review
  finished clean on pull requests 89 and 98 (2026-09-03 and 2026-09-06).
- **Milestone 29 — Chat thread folders** — hosted CI and review finished clean
  on pull requests 118 and 127, including the ADR-0110 typed-judgment
  amendment, and the owner activated folders in production (2026-09-21).

Evidence for completed milestones lives in
[`verification-history.yaml`](verification-history.yaml). Milestones 13, 19,
20, 22, 23, and 29 met their completion rules weeks before this page caught
up; their status was recorded on 2026-09-28 from the pull requests' review and
hosted-CI records.

## In progress

### Milestone 25 — WhatsApp business surface

The optional WhatsApp Cloud API adapter now runs on the Milestone 14 surface
seam: its loopback webhook verifies the handshake and raw-body signature,
normalizes Meta message IDs into shared receipts, confines outbound calls to
the Meta Graph origin, and enforces template-only delivery outside the
twenty-four-hour window. All twelve `gate.whatsapp.*` entries resolve to live
checks and pass locally. It shipped in pull request 104 with Milestone 14 and
shares its review resolution and surface run budget. The
[WhatsApp integration runbook](../whatsapp-integration-runbook.md) owns the
remaining operator ceremony. Remaining:

- [ ] Complete the owner Meta ceremony and obtain approval for the content-free utility template
- [ ] Deploy and run the live-number webhook, pairing, reply, approval, window-boundary, and revocation smoke

### Milestone 18 — First-class email integration

A parallel workstream providing one isolated triplet of least-privilege Gmail
MCP servers per operator-managed account for read and triage, drafts and label
mutation, and approval-gated sending. All seventeen gates and the full local
repository check pass; ADR-0085 owns the four multi-account additions. The
final head of
[pull request 73](https://github.com/avitus/veetbot/pull/73) passed hosted CI,
GitGuardian, and CodeRabbit with every review thread resolved before merge
([Glen review](https://app.tryglen.com/avitus/veetbot/pull/73)). The
multi-account extension reached `main` in pull request 94, which merged with
one CodeRabbit finding unresolved and still unfixed. Follow the
[Gmail integration runbook](../gmail-integration-runbook.md) for the remaining
owner-controlled work. Remaining:

- [ ] Owner real-mailbox smoke covering bootstrap consent, scheduled triage, phone approval, and one approved send
- [ ] Work-account bootstrap, production manifest activation, and work-only draft smoke
- [ ] Resolve the CodeRabbit finding pull request 94 left open (a root-level --account-id never reaches bootstrap), then pass hosted CI and the CodeRabbit review loop for the multi-account extension

### Milestone 21 — Adaptive memory distillation

A sixth parallel workstream making memory formation materially less timid.
Its thirty-one gates cover integrated episodes, the fixed three-call
prediction-error pipeline, direct and hypothesis recall, evidence-based
forgetting, persistence, comparative activation evidence, the honesty of
that evidence: a scorer that cannot be fooled, a seeded evaluation store, a
fallback that never fabricates, verified coverage dispositions, and bounded
segmentation, and the repaired provider-assisted `formation@10` control with
its explicit policy precedence (ADR-0086). ADR-0090 widens the existing
source-grounding gate so authenticated communication channels can form bounded,
attributed memories without upgrading correspondent content to owner speech.
The local implementation, static and contract suites, strict
documentation build, and Apple package tests pass locally; the fresh
PostgreSQL 16 integration lane passed in hosted CI on pull request 90
(CircleCI build 2668); and the 2026-09-03 three-arm live production-tuple
corpus under distillation-scorer@2 passed. The 2026-09-02 artifact was withdrawn
because its scorer, corpus, empty store, and build reference could not
support the numbers it carried; the replacement is bundled for immediate
exact-tuple activation. On 2026-09-03 the deployed artifact was found bound
to the pre-Milestone-24 policy version and was regenerated on the deployed
tree; the bundle test now refuses that mismatch, and the starved
`formation@8` artifact is withdrawn in favor of `formation@10`. On 2026-09-04
an independent review reproduced eight release-blocking defects; each is
fixed with a regression test (ADR-0087), the scorer advanced to
`distillation-scorer@5`, and the `formation@9` artifact evaluated under the
old scorer is withdrawn, so `formation@10` serves the production tuple until
`formation@9` is re-evaluated on the deploying tree. On 2026-09-09 the first
run over the development corpus and the frozen holdout failed on hypothesis
recall, precision, and paraphrase-bound direct recall; the defects it exposed
are fixed and the scorer is `distillation-scorer@6`. A bounded prompt round
on 2026-09-10 lifted holdout hypothesis recall to 0.800 and the development
corpus to full direct recall, but the holdout still failed on direct recall
and precision. The owner then set a recall-first bar (`distillation-scorer@7`,
a 0.75 holdout precision floor) and gating over three pooled repeats; the run
at `0e3ca2f` passed and its `formation@9` artifact is bundled. It reached
`main` in pull request 104 and was first deployed by the next merge; the
artifact stays bound to the unchanged shipped policy, but no production
selection audit naming `formation@9` has been recorded since. Remaining:

- [ ] Run hosted CI and the CodeRabbit review loop on the final head
- [ ] Promote the bundled formation@9 evidence to main through the CodeRabbit loop and verify the deploy selects formation@9 for the production tuple
- [ ] Memory formation temporarily remains on GPT-5.6 Sol under ADR-0093; audit scorer semantics and compare Sol/Astra under controlled reasoning settings before changing the memory tuple with new activation evidence.

### Milestone 24 — SMS through the owner's iPhone

A ninth parallel workstream. The `DeviceChannel` port with its push-wake
adapter, capability-derived `device.sms.send` registration, the three
device-authenticated routes, the SMS ingest path with its standing triage
session, and the expiry sweep are implemented, all twelve registered
gates pass locally, and the iOS client's SMS integration setting,
compose-sheet send flow, "Forward Message to Veetbot" App Intent, and ingest
forwarding are built with the owner capture ceremony documented
([deployment.md](../deployment.md#the-sms-capture-ceremony)). Remaining:

- [ ] Owner end-to-end verification on a physical iPhone

### Milestone 26 — Client modes and adaptive email experience

The owner approved the complete email experience and aggregate automatic-email
budgets on 2026-09-11 under ADR-0092. Twenty-seven of its thirty-two registered
gates now bind implemented backend and native acceptance checks; four private
quality gates and the integrated release gate remain pending. No private quality or owner-smoke
evidence is claimed. The milestone stays in progress and does not move the
verified ceiling. Remaining:

- [ ] Calibrate aggregate cost and performance with authorized representative mailbox work
- [ ] Execute frozen private importance, reply relevance and writing-style evaluations with the owner
- [ ] Pass private semantic precision, recall and usefulness evaluation before activating the exact policy tuple
- [ ] Pass all thirty-two Milestone 26 gates and relevant local, PostgreSQL, native and private quality evaluations
- [ ] Record authorized two-account real-mailbox acceptance and final-head hosted review and production delivery evidence
- [ ] Make reply drafting refetch a thread whose retained body has expired, as email-experience.md requires; today the draft run fails as internal_error (EmailToolError) before any model request, so tests/unit/test_email_scan_cost.py::test_draft_generation_never_receives_an_expired_body passes vacuously

### Milestone 27 — Bland calling and public reception

Approved on 2026-09-11 under ADR-0097. Local implementation includes twelve
executable offline gate bindings, PostgreSQL contract evidence and 289 passing
Apple package tests. Calling has been active in production since 2026-09-17.
Live inbound calls and one owner-approved outbound call are retained, readable from
Chat and announced to every device. Reaching that point took two fixes: ADR-0105's
tool-definition budget and the missing `call_finished` lock-screen alert. Bland's
signed post-call webhook has been verified in production against the raw bytes.
Every pull request that carried calling to `main` passed hosted CI and a clean
review; the final-head review waits for the two pending gates,
`gate.call.admission_bounds` and `gate.call.live_release`, to be bound.

- [ ] Verify provider-side admission controls and account-level memory or persona attachments in the Bland dashboard
- [ ] Live approval denial, no-answer, post-dispatch cancellation and owner deletion checks
- [ ] Exact-head hosted CI and explicitly authorized CodeRabbit review evidence

### Milestone 28 — People and relationship memory

Thirty-six gates; independent workstream under ADR-0100.

ADR-0101 makes the complete implemented Milestone 28 People experience available
by default. Quality measurements remain open evidence work, without gating runtime
functionality or release. None of the thirty-six `gate.people.*` registry
entries is bound to an executable check yet; every one still points at the
pending check. Production delivery still follows exact-head CI/review.

- [ ] Bind each of the thirty-six gate.people.* registry entries to an executable check; every one still points at the pending check
- [ ] Review complete temporal/source-erasure gate coverage and worst-case initial-fence latency
- [ ] Finish native accessibility, load/contention and signed restore verification
- [ ] Independently reviewed labels, version-bound quality and private owner evaluation evidence
- [ ] Private quality evaluation of the generated correspondence summaries that ADR-0126 activated ahead of it
- [ ] Exact-head hosted CI and explicitly authorized review and production delivery evidence

### Milestone 30 — Advisory approval layer

Five gates; independent workstream under ADR-0111, the restrictive-only half
of roadmap item B8. An optional advisor behind a port can only escalate an
allowed web search, page fetch or browser navigation to an approval; it never
denies, abstains on any failure, and is consulted once per invocation. The
port, the composite engine, the judgment-backed advisor and the pipeline's
recovery policy are implemented with all five gates bound to executable
checks, and reached `main` in pull request 127 with a clean review. Observing
is an environment flag that moves no policy version; enforcing is the profile
value and does, and the shipped profile keeps it disabled.

- [ ] Calibrate the advisor's thresholds while observing, then decide whether to enforce
- [ ] Exact-head hosted CI and explicitly authorized review and production delivery evidence

### Milestone 31 — Email unsubscribe assistance

Twenty-three gates; independent workstream under ADR-0112, extended by
ADR-0165. A header-derived census of bulk senders, the authenticated one-click
unsubscribe request to a server-derived destination through a dedicated
public-HTTPS transport, and Report spam, `mailto:` and sender-cleanup
fallbacks, each behind the owner's tap; and a suspected-spam flag that never
touches Gmail, with thread-level Report spam and Not spam. Nineteen gates bind passing checks. The milestone reached `main` in pull
request 131 with a clean review, and its flag was switched on in production by
2026-09-21, ahead of the owner's real-mailbox smoke.

- [ ] Pass the five native Subscriptions journeys (added 2026-09-28 and passing locally on the iPhone and iPad simulators) in the hosted Mac, iPhone and iPad UI lanes
- [ ] Show the Subscriptions detail beside its list on regular-width iPad as email-unsubscribe.md requires; the iPad sheet is a 580-point compact-width form sheet, so the detail pushes as on iPhone
- [ ] Pass the two native suspected-spam journeys (added 2026-10-08 and passing locally on the Mac and the iPhone and iPad simulators) in the hosted Mac, iPhone and iPad UI lanes
- [ ] Owner-authorized real-mailbox smoke on both accounts covering one-click, mailto, sender and thread spam each with Not spam, and cleanup
- [ ] Exact-head hosted CI and explicitly authorized review and production delivery evidence

### Milestone 32 — Memory reconsolidation (dreaming)

The [detailed design](../plan/memory-reconsolidation.md) declares twenty-four
registered gates. Both stores implement bounded inventory, leases/spend, merge,
summary, grounded connection and conflict operations, source invalidation/erasure,
owner controls and gated background composition. The Synthesis browser exposes
supporting originals, uncertainty, conflicts and previewed merge undo. Shared
contracts, PostgreSQL RLS/migration checks, Apple package tests and iPhone/iPad
journeys cover these components; the complete runtime/release gates remain pending.

Two answer-bearing original controls and full three-arm comparisons are retained
under `evals/observations/memory-reconsolidation/`. Both failed the unchanged quality
gate. The second run preserved every fact and had zero false merges, but its answer
lift was only 1/36 per split, merge answers regressed and no hypothesis matched the
scorer's exact reference wording. The owner approved ADR-0170 on 2026-10-08;
its offline equivalence-review contract is implemented. The third comparison bound
that contract before measurement. The owner judged all fifteen candidates: ten
equivalent and five not equivalent. Precision is 75% on development and 57.14% on
holdout, below the unchanged 80% floor; recall passes both splits. Every fact is
preserved with no false merges, but answer lift and development merge-answer
nonregression still fail. Review artifacts and the recomputed assessment are in
`evals/observations/memory-reconsolidation/20261008-owner-review-v2/`. Production stays off.

The ADR-0171 isolated owner-data experiment completed on 2026-10-09, and the
owner judged its accepted related-memory summary useful. This completes that
review; legacy bank replay and the comparative quality failures remain unresolved.

The October 9 diagnostic comparison records 14/18/14 development and 17/17/19
holdout answers out of 36 for original/merge/connections. Merge-answer
nonregression passes, but answer lift remains below ten percentage points.
Eleven new hypotheses await their own bound owner review; earlier judgments and
failures remain unchanged. The design records the repairs and measured limits.

- [ ] Resolve precision and answer-lift failures without repeating completed owner reviews; new hypothesis quality remains unverified under ADR-0170, and release-tuple proof is still required
- [ ] Complete the once/dry-run operator controls and bind each remaining complete runtime gate to integrated evidence
- [ ] Complete macOS Synthesis journeys and publish matching upstream, comparative and exact-revision release evidence before production activation

## Outside a milestone

Open work that no active milestone owns: owner-only steps of non-milestone
ADRs, and specification-versus-code gaps found in completed milestones. It
mirrors `open_items_outside_milestones` in
[`project-state.yaml`](project-state.yaml).

- [ ] Owner: install a macOS TestFlight build carrying ADR-0127 to ADR-0130 (merge a9b81420 or later) on every Mac
- [ ] Owner: confirm production AUTH_SCOPES contains browser.profile.read, browser.profile.write, browser.grant.read and browser.grant.write
- [ ] Owner: production acceptance of device sign-in (ADR-0128 V1, V3 and V4; V2 once the iOS build ships)
- [ ] Owner: once a client build that decodes approve_for_task runs on every device, set BROWSER_TASK_GRANT_SCOPES=https://www.duolingo.com/lesson and BROWSER_TASK_GRANTS_ENABLED=1 for the API and workers
- [ ] Owner: production acceptance of the task grant (ADR-0129 Validation): one approval runs a lesson while the banner counts actions and minutes; Stop brings the card back; an action outside the lesson, such as a payment or settings control, still asks and says why; activity rows the grant authorized show "Allowed by task permission" and what was clicked or typed. Then measure the budget of the first lesson in a new bound chat (ADR-0130)
- [ ] Owner: keep BROWSER_PROFILE_DEVICE_SIGN_IN_ENABLED unset or true in production; to turn device sign-in off, set it to false in /etc/veetbot/veetbot.env and recreate the browser-profile-service container with the block in docs/deployment.md (a restart keeps the old value)
- [ ] Tool system: the circuit-breaker section of tool-system.md specifies one counter keyed on name, arguments hash, outcome and reason, but runtime/loop.py keeps separate identical-denial and identical-call counters; change the code or propose an ADR that accepts two (found during ADR-0130)
- [ ] Command line: engineering-plan.md Section 17 and bootstrap-and-composition.md require `agent chat`, arriving at Milestone 3, but the CLI registers no chat command although Milestone 3 is complete; implement it or propose an ADR that removes it (found in the 2026-09-28 documentation audit)
- [ ] HTTP API: event-log-and-persistence.md and ADR-0032 decision 9 require `POST /v1/runs/{run_id}/export`, but no such route is mounted and only `agent run export` exists; mount it or propose an ADR that drops it (found in the 2026-09-28 documentation audit)
- [ ] Owner: accept or reject ADR-0104, ADR-0105, ADR-0106, ADR-0107, ADR-0114, ADR-0135, ADR-0137 and ADR-0138, which are still Proposed although their mechanisms are implemented, specified and on main
- [ ] Owner: accept or reject ADR-0146, which is Proposed and implemented: a hosted browser launch repeats the Playwright disabled-feature list, gives push messaging a check-in address it cannot fetch, and leaves one account request to accounts.google.com on
- [ ] Owner decision: the hardline protected_host_path rule blocks .git/config only at the workspace root, so sub/.git/config is not blocked, while .env and ~/.ssh already match at any depth; decide whether it should, knowing that a change to the matcher in policy/hardline.py leaves policy_version unchanged but an edit to hardline.yaml moves it and unbinds the memory-formation release evidence (found in the 2026-09-28 test audit)

The earlier item asking the owner to confirm the ADR-0127 to ADR-0130 deploy
is closed: after the owner reran the workflow, deploy-app and deploy-nginx
succeeded on merge `a9b81420` (`outside_milestone_evidence` in the state file).

## Authorized

Specified, gated, and authorized, with implementation not yet begun.

- **Milestone 15 — Operational hardening** — sixteen gates; the next
  sequential milestone now that Milestone 14 is complete. Its backup tranche
  depends on none of the milestones before it.




## Deferred

Nothing on the engineering plan's
[roadmap beyond Milestone 15](../plan/engineering-plan.md#roadmap-beyond-milestone-15)
is authorized until the owner says so and a specification with gates exists
for it. That roadmap holds, among its items: tenant activation of
self-authored skills (B1), dynamic model routing and a second provider
adapter (B2), Slack and email inbound surfaces (B3), email and webhook
notification transports (B4), scheduling residue such as cron, interval
multipliers, and dependency graphs (B5), the memory residue after Milestone 22 — the semantic arm,
`pgvector`, an external memory provider, and a learned memory policy (B6) — presence routing and
hand-off (the B7 residue after Milestone 24), standing approval grants (B8), the
trajectory-to-fine-tuning loop (B9), S3-compatible artifact storage (B10),
calendar integration and the other B11 surfaces beyond email, and
multi-tenant billing and quotas (B12), and the WhatsApp linked-device
bridge (B13).

Work each milestone explicitly set aside is recorded as that milestone's
`deferred_scope` in [`project-state.yaml`](project-state.yaml); a deferred
item re-enters only through a new authorization and, where the change is
architectural, an ADR.
