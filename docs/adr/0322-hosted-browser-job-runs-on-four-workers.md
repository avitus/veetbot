# ADR-0322: The hosted browser job runs on four workers

- Status: Proposed (requested by the repository owner, 2026-11-13)
- Date: 2026-11-13
- Related: ADR-0107, ADR-0114, ADR-0156, ADR-0236, ADR-0257
- Amends: ADR-0236 decision 4, under which `BROWSER_TEST_WORKERS` defaulted to
  0 so that CI stayed serial
- Detailed design: `docs/plan/development-toolchain.md`

## Context

On 2026-11-13 the owner asked why the CircleCI browser tests take so long. The
hosted `browser` job ran the real-Chromium partition serially on a `medium`
runner (two CPUs). On that day's verify run it took 20.6 minutes, longer than
any other job, so it set the length of the whole verify workflow. The next
longest jobs were `integration` at 17.5 minutes and `static` at 16.4.

The same day a review removed redundant and slow cases. The partition went
from 526 cases to 401, about 1,000 seconds of test time, and 111 fake-page
Playwright cases moved to the fast suite.

ADR-0236 already spreads the partition over xdist workers in the local
container. Each worker takes whole files and ships its evidence to the
controller, which writes the one baseline report, so a parallel run meets the
same gate. Only CI and the sidecar stayed serial.

## Decision

The hosted `browser` job runs on a `large` runner (four CPUs) and sets
`BROWSER_TEST_WORKERS=4`. Nothing else about the gate changes: the same
partition, the same report, and the same skip and record steps (ADR-0114,
ADR-0257). The sidecar stays serial.

## Evidence

Measured in one trial pipeline (2163, 2026-11-13) on the reviewed partition,
with one job per variant and all 401 cases passing in each:

| Variant | Job | Chromium install | Test step | Credits (est.) |
| --- | --- | --- | --- | --- |
| Before: `cimg/python`, `medium`, serial (verify, 2026-11-13, 526 cases) | 20.6 min | in the test step | 18.4 min | about 206 |
| `cimg/python`, `large`, 4 workers | 6.8 min | 35 s | 5.9 min | about 136 |
| `cimg/python`, `xlarge`, 8 workers | 5.0 min | 23 s | 4.4 min | about 198 |
| Playwright 1.62.0 image, `large`, 4 workers | 6.8 min | 7 s | 5.7 min | about 136 |

Credits are estimated at 10, 20 and 40 a minute for `medium`, `large` and
`xlarge`.

- **Eight workers.** They finish 1.8 minutes sooner, but at 6.8 minutes
  the browser job is no longer the longest, so the workflow does not finish
  any sooner. They cost about 45% more a run.
- **The pre-built Playwright image.** It saves the Chromium install, but its
  pull added 33 seconds to spin-up, so the job took the same time. It would
  also add `make` to install on every run and a second image to keep in step
  with `uv.lock`, so it is not adopted.

## Consequences

- The browser job takes about 7 minutes instead of 21, and costs about a third
  fewer credits a run. The verify workflow now ends with `integration`.
- Load-sensitive browser cases now share four CPUs with three other workers.
  The trial passed with no failures. A real-Chromium case that fails only in
  this job is reproduced under load in the local container and fixed in the
  test, not by returning the job to serial.
- The device-handoff module, at about 190 seconds, is the longest single file.
  It bounds how far more workers can help.
- Any edit to `.circleci/config.yml` is an input of the Apple jobs' skip record
  (ADR-0257), so the next hosted verify run also reruns the Apple jobs once.
