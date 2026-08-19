# ADR-0055: Configurable CLI wait for durable runs

- Status: Accepted
- Date: 2026-08-18
- Related: Section 17 (CLI contract), ADR-0024 (composition and CLI),
  ADR-0054 (provider-neutral web access)
- Detailed design: `docs/plan/bootstrap-and-composition.md`

## Context

`agent run` submits through the durable `RunService`, then waits locally so it
can print the final assistant message. ADR-0024 originally fixed that local
wait at 30 seconds and counted four CLI options. A web run that made three model
calls and three provider-backed tool calls completed successfully in about 36
seconds, but the submit process exited 5 at 30 seconds and reported that the run
had not reached a terminal state. The durable run completed normally after the
CLI stopped listening.

A fixed local deadline cannot represent both fast deterministic runs and
provider-backed work with variable latency. It also must not become a server
deadline: Swift and other HTTP clients already observe the durable run
asynchronously.

## Decision

1. `agent run` accepts `--wait-timeout <seconds>`. The value is a finite,
   positive number and defaults to 300 seconds.
2. The value bounds both the terminal-state poll and the replay that retrieves
   the persisted terminal event. Expiration preserves exit code 5 and prints
   the durable run identifier; it does not cancel or fail the run.
3. The option is local to CLI submission. It changes no API timeout, worker
   deadline, run limit, or native-client behavior.
4. `agent run events` retains its separate bounded read. The submission option
   does not silently change another command.
5. This decision supersedes only ADR-0024's count and rationale for four CLI
   options. It adds no command and changes no application-service boundary.

## Consequences

- Multi-step web runs can remain attached long enough to print their final
  response without making the CLI wait unboundedly.
- Scripts that rely on the old behavior can request `--wait-timeout 30`.
- The 300-second default changes only local waiting; durable execution remains
  independent of the submitting process.
