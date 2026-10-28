# ADR-0236: Real-browser cases run off the owner's desktop

- Status: Proposed (requested by the repository owner, 2026-10-20)
- Date: 2026-10-20
- Related: ADR-0106, ADR-0107, ADR-0145, ADR-0156
- Detailed design: `docs/plan/development-toolchain.md`

## Context

Every hosted browser is headed (ADR-0145). On Linux the runtime starts a
private Xvfb display for each one; on any other platform it uses the native
display. The `browser` partition (ADR-0156) launches those headed browsers, so
running it on the owner's Mac opens Chromium windows on their desktop. Agent
sessions ran `make test-browser`, `pytest -m browser`, or single real-Chromium
modules on the Mac host about 800 times in the three days before 2026-10-20,
while the macOS UI journeys had already moved into a Tart VM so that local
automation does not take over the desktop.

## Decision

1. On macOS, `make test-browser` and the new `make test-browser-cases
   CASES=<paths>` run on the Linux chunk sidecar through
   `scripts/run_on_sidecar.sh`, which runs the project's `install` command and
   then the same Make target there. On Linux both run in place, as CI and the
   sidecar already do.
2. `test-browser-cases` runs only the named cases, still with
   `VEETBOT_REQUIRE_REAL_BROWSER=1`, and writes no baseline report, so a focused
   run can never stand in for the required gate.
3. A case in the browser partition collected on a Mac host skips with the
   sidecar route as its reason. Under `make test-browser` the baseline counts a
   skip as unpassed, so the gate cannot pass on the host.
4. `VEETBOT_BROWSER_ON_HOST=1` runs either target, or a direct pytest
   selection, on the Mac deliberately.
5. The production runtime is unchanged.

## Consequences

- A Mac run of `make check` sends its browser lane to the sidecar while the
  other lanes run locally; `chunk validate` on the sidecar is unchanged.
- A red or green browser test during development needs a sidecar. Without the
  `chunk` CLI or a sidecar, the browser targets fail on a Mac rather than open
  windows.
- Headless Chromium launched outside the partition (for example the
  authentication-surface typing test) draws no window and keeps running on the
  host.

## Amendment (2026-10-28): a local Docker route

The owner asked for a way to run the partition on the Mac without the sidecar.
Linux Chromium in a local container draws to Xvfb as it does on the sidecar,
so no window reaches the desktop.

1. `VEETBOT_BROWSER_ROUTE` picks where a Mac sends both browser targets:
   `sidecar` (the default, unchanged) or `docker`, which runs the same Make
   target through `scripts/run_in_linux_container.sh`. Any other value fails
   the browser targets and nothing else.
2. The container is linux/amd64, the sidecar's and hosted CI's architecture,
   emulated on Apple silicon. `deploy/browser-test.Dockerfile` caches the
   locked dependencies, Chromium and Xvfb on `uv.lock`; the checkout is
   mounted, so the baseline report lands in it and records the revision.
3. arm64 is not offered. Native arm64 Linux Chromium ran a module four times
   faster but crashed the renderer on the deep-page readable-text case that
   passes on x86_64, so it does not match the gate.

Measured on 2026-10-28 on dev 86d03abc, one run each, not concurrent: the
container passed all 348 browser cases in 18 min 19 s (18 min 0 s of tests;
a cold image build adds 96 s once per lock change). A fresh sidecar passed them
in 12 min 41 s, including creation, sync and install (11 min 15 s of tests).
The container is slower but uses no CircleCI credits, and each run has its own
container, so concurrent sessions no longer share one sidecar's install.
