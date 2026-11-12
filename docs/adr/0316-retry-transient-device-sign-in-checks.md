# ADR-0316: Recognize unresolved sign-ins and retry temporary checks

- Status: Proposed under the owner's 2026-11-11 sign-in reliability repair instruction
- Date: 2026-11-11
- Amends: ADR-0128 (native device ceremony orchestration)
- Detailed design: `docs/plan/browser-automation.md`

## Context

The owner reported a LinkedIn device sign-in that succeeded on the third manual
attempt. The matching production diagnostic reported `with_session=settle` at
the thirty-second deadline. It identifies the stalled stage, not the request
or its purpose. Ignoring pending application requests would risk accepting a
page whose authentication decision has not arrived.

The subsequent chat encountered a remembered-account **Sign in as** control,
then a **Performing security verification** interstitial. Both reached ordinary
browser observations without the existing owner-intervention signal. A newly
ready profile also reached the interstitial shortly afterward. Those observations
do not establish whether the site invalidated a session or the earlier verifier
accepted an unresolved sign-in.

## Decision

Ceremony verification and automation readiness recognize visible headings
whose entire text is **Performing security verification** or **Verify you are
human**, and visible links/buttons containing **Sign in as** followed by an
account label. The patterns are shared, case-insensitive and tolerate whitespace.
Hidden elements and incidental prose do not qualify. The existing bounded DOM
read returns only a boolean; no account label is logged or persisted by it.
These are unresolved challenges: verification cannot seal them, and automation
returns the existing `tool.browser.needs_user` instead of treating them as
ordinary content. Nothing attempts to solve or bypass the site's challenge.
An internal, content-free evidence flag distinguishes a security interstitial
from an authentication prompt. A security interstitial in either comparison
browser makes the pair unconfirmed, including configured-site verification:
an empty browser blocked by bot protection cannot prove that the other browser
has authenticated, rather than merely passed the bot check.

The native client may make at most three sequential device handoffs for one
press of **I'm signed in**. A further attempt requires both an explicit HTTP
409 `tool.browser.provider_unavailable` answer from the isolated service and
an authoritative `cancelled` outcome from closing that ceremony. Missing,
ambiguous, nonterminal, authentication and profile-revocation outcomes stop.
Transport failures retain status reconciliation and are never retried.

Each retry begins a new ceremony for the same profile and exact origins, uses
its new single-use capability once, and sends the same locally scoped session.
The session remains only in the existing completion call's local variables;
no session or capability is published, logged or persisted. Half-second then
one-second delays separate attempts. A closed window, changed connection or
cancelled task stops further attempts. The UI names the current retry.
Cancelled cleanup retains its request identity. Connection reconfiguration blocks
new device attempts and waits for registered cleanup before replacing the shared
credential, so an old client cannot dispatch cleanup with the replacement token.

Each attempt receives the unchanged server verification: two isolated browsers,
ordinary application-request completion, signed-in/signed-out differentiation,
late-challenge observation, the thirty-second limit, origin confinement and
revocation checks before sealing. A failed attempt saves nothing. There is no
new server retry, request exemption, timeout increase, grant or policy change.
The complete user operation can take roughly ninety seconds plus orchestration
and cleanup; it is bounded by three attempts and existing request deadlines.
Retry admission never cancels a different ceremony that wins the profile.

## Validation and limits

Local sandbox evidence on 2026-11-11:

- The native retry regression first failed because only one ceremony began
  instead of three, and the eventual verified success was missing. After the
  repair, all 66 tests in `ChatViewModelDeviceSignInTests` and
  `DeviceSignInHandoffClientTests` passed in the headless macOS VM. They cover
  fresh capabilities, the retry cap, terminal failures, ambiguous closure,
  cancellation, connection changes and competing ceremonies.
- The real-Chromium regressions first missed both visible prompts in
  verification and automation. Before the distinct security-interstitial flag
  was added, the blocked-control regression returned HTTP 200/ready instead of
  HTTP 422/unconfirmed. All six focused browser cases
  subsequently passed in the Linux container, including hidden prompts and
  incidental prose.
- The focused server checks passed: 143 contract tests and 72 static tests,
  including configured-site rejection of a security interstitial in either
  comparison browser for device and remote ceremonies.
- The full static partition completed with 10,089 passed and three skipped.
  Ruff formatting/lint, mypy, documentation validation and citation repair
  also passed; the citation repair was reviewed.
- The full browser partition ran 445 cases: 444 passed and the existing
  delayed-receipt case with hidden confirmation returned `outcome_unknown`.
  Rerunning its entire six-case file unchanged passed all six. The full run
  itself is not recorded as a clean pass.
- Full native validation is blocked by a Swift runtime allocator crash in
  `AttachmentTests.testUploadRetriesAServerFailureWithTheSameKey`. The same
  crash reproduces with the Apple changes reversed in the isolated VM. No
  failing test was weakened or skipped to claim a complete native pass.
- Integration onto `dev` at `c40301a5` preserves its composed-ancestor and
  open-shadow challenge checks. Revalidation passed all 42 browser cases
  covering shadow authentication, completion and these sign-in regressions,
  all 66 native sign-in tests, 72 focused static tests and 143 contract tests.
  Formatting, lint, mypy and documentation checks also passed on that base.

The red-test commands were:

```sh
scripts/run_apple_ui_macos_vm.sh swift test --package-path clients/apple \
  --filter ChatViewModelDeviceSignInTests.temporaryVerificationFailuresUseFreshCeremoniesAtMostThreeTimes
scripts/run_in_linux_container.sh \
  'make test-browser-cases CASES=tests/unit/test_device_handoff_real_chromium.py::test_a_security_blocked_control_cannot_prove_a_session'
```

Existing delayed-authentication, origin, session differentiation and revocation
checks remain required. This recovers transient failures; a permanently busy
or incompatible website still fails closed. This does not prove that LinkedIn
will retain a transferred session. Live LinkedIn acceptance requires the
updated server and client and is pending.
