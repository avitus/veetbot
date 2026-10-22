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
