# ADR-0323: The static and integration jobs run on four nodes

- Status: Proposed (requested by the repository owner, 2026-11-13)
- Date: 2026-11-13
- Related: ADR-0114, ADR-0228, ADR-0230, ADR-0257, ADR-0322
- Amends: ADR-0230 decisions 3, 4 and 5, under which `static` and
  `integration` ran on two nodes, `static` ran its checks on node 0, and a job
  ran on at most two nodes
- Detailed design: `docs/plan/development-toolchain.md`

## Context

After ADR-0322 cut the hosted `browser` job to 6.8 minutes, `integration`
(17.5 minutes) and `static` (16.4 minutes) set the length of the verify
workflow whenever the Apple jobs skip their tree. On the dev verify of
2026-11-12, both jobs ran their CPUs flat out:

- `integration` ran on two one-CPU nodes at a mean of 0.96 CPU. Its tests take
  about 1,630 seconds in all; the largest files are People persistence (330 s)
  and reconsolidation (255 s). The timing split left 731 s on one node and
  931 s on the other.
- `static` ran on two two-CPU nodes at a mean of 1.8 CPU, about 2,880 seconds
  of tests over 10,323 cases with no dominant file. Node 0 also ran lint, the
  type check (93 s), documentation, deployment and client-build checks, which
  the timing split does not count.
- Each job's discovery collected the whole test tree, about 80 seconds, while
  the other nodes waited. Integration collected about 600 static files only to
  deselect them.
- Nine gates in five modules each re-ran the full architecture scan of the
  checkout, about 18 seconds apiece in CI.

## Decision

1. `integration` runs on four nodes (`parallelism: 4`) and stays on the
   one-CPU `small` class. Each node keeps its own PostgreSQL container.
2. `static` runs on four two-CPU nodes (`parallelism: 4`), still with two
   load-scope workers a node. Its checks run before the tests on nodes other
   than the first: the type check on node 1, Pandoc and the documentation check
   on node 2, and lint, deployment tests and the client build on node 3. The
   first node to reach the test step discovers the files, so it should have no
   check ahead of it, and the other nodes run their checks while it does. The
   reading-lane floor still runs on every node.
3. The exact-tree record restores four node records, so a job may run on at
   most four nodes. Raising that again needs another restore step.
4. Integration discovery names its directories: those conftest routes to the
   partition (`tests/integration`, `tests/resilience`, `tests/security`) and
   `tests/native`, whose modules mark themselves `integration`. A gate checks
   that every top-level test directory holding such a module is named. The run
   still selects by marker through `make test-integration`.
5. The nine repository-root architecture gates share one scan per test
   process (`tests/architecture_scan.py`). Gates that scan a fixture tree still
   call `architecture_errors` directly.

## Evidence

Measured in one trial pipeline (2167, 2026-11-13), with one job per variant.
Every test ran. About 20 config-reading tests failed in the static variants
because the trial's stripped config lacked the real jobs; they fail in under a
second each, so the timings stand. Credits are estimated at 5, 10 and 20 a
minute for `small`, `medium` and `large`, summed over nodes.

| Variant | Job | Slowest node | Credits (est.) |
| --- | --- | --- | --- |
| Before: `integration`, 2 × `small` (verify, 2026-11-12) | 17.5 min | 1,016 s of tests | about 175 |
| `integration`, 4 × `small` | 9.3 min | 560 s | about 170 |
| `integration`, 4 × `medium` | 9.5 min | 570 s | about 365 |
| Before: `static`, 2 × `medium` (verify, 2026-11-12) | 16.4 min | 984 s | about 315 |
| `static`, 4 × `medium`, checks spread | 9.1 min | 547 s | about 350 |
| `static`, 2 × `large`, 4 workers a node | 10.2 min | 614 s | about 373 |

- **Two CPUs an integration node** did not help: the suite runs serially, so
  PostgreSQL's share of one CPU was not the limit. They double the credits.
- **Two four-CPU static nodes** cost the same as four two-CPU nodes but
  finished later, because the node that ran the checks started its tests about
  150 seconds after the first node.
- In the container, the narrowed integration discovery finds the same 129
  files as the full collection. One architecture scan takes 7.6 seconds there,
  and the five gate modules now run it once a process instead of nine times.

## Consequences

- With the Apple jobs skipped, verify takes about 9.5 minutes instead of 17.5,
  for about 30 more credits a run across the two jobs.
- When the Apple jobs run, the macOS job (about 25 minutes) still sets the
  pace; this decision does not change it.
- The first run of each job after this change finds no four-node record and
  runs in full.
- Any edit to `.circleci/config.yml` moves the line the secret allowlist pins
  for the CI database credential; the scanner gate reports the stale pin.
