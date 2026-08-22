# ADR-0067: Credential-free production execution service

- **Status:** Accepted
- **Date:** 2026-08-21

## Context

The engineering plan and ADR-0008 reject container-runtime access from a worker
that handles or orchestrates untrusted code. ADR-0046 nevertheless placed the
host Docker socket in the production worker units. Because those units also
load database, provider, browser, and API credentials, compromise of a worker
could become host-root compromise. It could also overwrite immutable source and
documentation releases despite systemd's read-only filesystem view, because
Docker-socket authority is not constrained by that view.

The single-Droplet deployment still needs one component to own the existing
gVisor-backed Docker adapter. That component must receive sandbox specifications
and workspace operations without inheriting the application environment.

## Decision

1. Production uses three Unix identities: `veetbot-deploy` owns releases and
   delivery, `veetbot` runs application services, and `veetbot-exec` runs the
   execution service. Only the deployment and execution identities join the
   Docker group. Application units expose no Docker socket.
2. `veetbot-execution.service` is a separate systemd unit. It loads no
   application environment file and owns the gVisor `DockerExecutionEnvironment`
   adapter. Its writable host surfaces are the Docker socket and its private
   runtime directory.
3. Workers call an `ExecutionEnvironment` client over
   `/run/veetbot/execution.sock`. The socket is group-readable and writable only
   by the application group. The framed protocol serializes the existing domain
   types, normalizes boundary failures, relays governed programmatic-tool bridge
   requests, and asks the worker to confirm lease liveness during reaping.
4. Production configuration requires `AGENT_EXECUTION_SERVICE_SOCKET` for
   kernel-isolating sandbox mechanisms. Development may continue to construct
   the adapter in-process for deterministic and local runtime tests.
5. The deploy account still builds and tags immutable images before restarting
   the execution service. The execution service resolves that tag to a digest;
   every provision request continues to use the immutable digest.

This decision implements the existing engineering-plan and ADR-0008 boundary.
It supersedes ADR-0046 decision 2 (three systemd units, with the worker alone
in the `docker` group), the worker-owned gVisor adapter described in that
ADR's context, and its Docker-group consequence; ADR-0046 decision 3 —
generated code runs through Docker's `runsc` runtime — stands under the new
owner. It also supersedes ADR-0048's count of three systemd processes;
delivery now manages the execution service alongside the API and worker roles.

## Consequences

- A compromised application process cannot use Docker to modify
  `/opt/veetbot`, `/opt/veetbot/docs`, other host files, or other containers.
- The execution service remains root-equivalent through Docker on the initial
  single host, but it receives no database, provider, browser, or API
  credentials. A later multi-host deployment can replace the local transport
  while preserving the `ExecutionEnvironment` port.
- Worker startup and sandbox maintenance now depend on the execution service.
  The worker, async-worker, and maintenance units declare `Requires=` and
  `After=` on `veetbot-execution.service`, so systemd activates the
  credential-free unit first and orders the dependents after it. Two restart
  paths follow. An explicit `systemctl restart` or `stop` of the execution
  service propagates through `Requires=` to those dependents, which systemd
  restarts in the same transaction; the release and rollback paths already
  restart every unit, so they gain nothing and lose nothing there. The
  service's own automatic `Restart=on-failure` recovery propagates nowhere —
  no unit binds to it with `BindsTo=` or `PartOf=` — so a dependent that
  loses the socket surfaces `ExecutionUnavailable` for that request and
  reconnects on its next one, because the client opens a connection per call.
- Large workspace reads cross the local socket as bounded frames. Artifact
  export retains its existing maximum size and digest verification.
