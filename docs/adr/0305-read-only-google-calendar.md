# ADR-0305: Read-only Google Calendar access

- Status: Accepted by owner instruction on 2026-11-09
- Date: 2026-11-09
- Related: ADR-0071, ADR-0255, ADR-0291
- Detailed design: `docs/plan/calendar-integration.md`

## Decision

The owner approved implementation of the read-only Calendar proposal. One
operator-configured first-party stdio MCP server wraps Google's Calendar REST
API, following Gmail's credential isolation. It exposes only selected calendars
and three read tools. OAuth uses a separate read-only grant and an operator
bootstrap with PKCE, loopback state validation and a data-use disclosure.

The server is default off. Google-specific code stays in `calendar_mcp`, outside
`agent_core`; neither package imports the other. Existing dependencies suffice.
Calendar data is untrusted tool output and receives no new memory ingestion path.
Schedules explicitly name the server, and the credential-free scheduler mirrors
the feature flag to vouch for its scope. Existing job grants can authorize reads.

This authorizes the read-only portion of roadmap B11 independently, without
moving any milestone or weakening a gate. Event writes, invitations, native
Calendar UI, multi-account configuration and automatic synchronization are deferred.
Google's remote MCP preview requires separate client provisioning; it is not a
prerequisite for the REST-backed integration.

## Verification

Implemented on `calendar-access`, with Reading-Lane: A (also the diff-derived
minimum). The isolated provider package, platform configuration/composition,
schedule eligibility, safe failure mapping, deployment flag parity and public
privacy disclosure ship together. No dependency or database migration was added.

The local Linux sandbox ran CircleCI's `static` suite narrowed to
`tests/unit/test_calendar_.*.py` (three atoms, 21 tests), and `contract` narrowed
to `tests/contract/test_calendar_mcp_contract.py` (one atom, 21 tests). All passed.
These exercise fake Google responses, the real stdio child and a composed Chat
read plus schedule admission. The initial red runs failed on absent Calendar
composition and adapter assertions; the failure-message regression first failed
because every error became `tool.server_error`. The commands use
`circleci testsuite run <suite> --run-tests=all -t <pattern>` inside the documented
local test container. No live Google request is part of this evidence.

Additional local verification:

- The complete contract partition passed: 2,243 tests in 148.55 seconds
  (206.12 seconds including discovery and command overhead).
- The broad static partition ran 9,954 tests: 9,947 passed, three skipped and
  four failed in 929.35 seconds. The package-layout assertion needed the newly
  authorized package. Two public OAuth endpoint constants needed exact-line
  secret-scanner allowlist entries. Two recall tests rejected the temporary
  46 MiB Linux CLI inside the checkout; moving that tool outside the checkout
  removed the unrelated input without changing recall code or its tests.
  Rerunning all four affected test files passed: 152 tests in 41.28 seconds
  (80.23 seconds including discovery). No observed failure remains unresolved;
  the complete static partition was not repeated after these corrections.
- Full typing passed for 1,798 source files in 7.79 seconds. Final lint passed
  for 1,941 files in 1.02 seconds.
- Deployment release/rollback/nginx checks passed; root-only permission cases
  remained skipped by their existing checks. The public website built, all nine
  rendered-page tests passed, and its lint passed. These were local checks,
  not a production deployment.
- Citation repair reported no unresolved citations. Strict documentation builds
  and checks passed in 25.50 seconds before the final verification record.

The slowest check was the broad static partition (15 minutes 29 seconds).
Contract, typing, website, deployment and documentation checks overlapped that
run; their durations must not be added to obtain elapsed wall time. Verification
from the initial red run through the final targeted reruns took approximately
30 minutes (estimate, including iteration and discovery).

The accepted scope is implemented: selected-calendar reads, exact read-only
consent, bounded/paginated agendas, calendar-time preservation and explicit
scheduled access. Event writes, multiple Google accounts, native connection UI,
automatic synchronization and production activation remain outside this release.
The engineering plan records this authorized B11 extension; no other requirement,
policy decision or milestone ceiling changed.
Production activation requires the owner's Google consent and a live calendar
smoke. No production acceptance is claimed by this decision.

## PR review follow-up

PR #247's first review identified two setup corrections and a swallowed diagnostic.
The guide now requires a separate Google Cloud project so Calendar revocation does
not revoke Gmail grants, and clears the credential-file setting before restart.
Credential metadata checks now preserve the 0600/size diagnostic outside the
`ValueError` translation. Added docstrings document the touched adapter functions.

The local Linux sandbox reproduced the diagnostic defect with
`circleci testsuite run static --run-tests=all -t tests/unit/test_calendar_composition.py`:
one assertion failed because the required mode/size hint became a generic error.
After the fix, 280 tests passed across Calendar, configuration, toolchain and
secret-scanner files; the repository scanner itself initially could not read a
mis-mounted Git directory. After correcting the mount, all five scanner tests
passed in 10.71 seconds (56.34 seconds including discovery).
Calendar contracts passed all 21 tests. Lint and typing passed (1.11 and 8.81
seconds respectively). Static execution took 23.61 seconds and contract execution
1.17 seconds; including discovery, those commands took 68.66 and 47.76 seconds
and overlapped. Documentation checks passed in 19.69 seconds; citation repair
reported no unresolved citations. Total local verification including setup and
iteration was approximately five minutes (estimate), with test discovery the
slowest stage. This follow-up uses Reading-Lane: A and changes no acceptance
criteria or activation requirements.
