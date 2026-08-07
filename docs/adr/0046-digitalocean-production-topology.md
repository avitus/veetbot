# ADR-0046: Host-native DigitalOcean production topology

- **Status:** Proposed
- **Date:** 2026-08-07

## Context

Milestones 0 through 9 implement the application and its gates, but they do not
select a hosting provider or package the API, worker, maintenance role, reverse
proxy, database, and sandbox runtime into an operator-owned deployment. The
first target is one DigitalOcean Droplet. Production startup already refuses
development authentication and the Docker and fake sandbox mechanisms.

The worker's gVisor adapter controls the host Docker daemon and mounts
lease-scoped workspaces. Putting the whole application in another container
would require passing the Docker socket and reconciling host paths through two
mount namespaces. That adds privilege and path ambiguity without improving the
sandbox boundary.

## Decision

The initial production topology is host-native:

1. One immutable source release and locked `uv` environment live below
   `/opt/veetbot`; `/opt/veetbot/current` selects the active release.
2. Three systemd units run the API, durable worker, and maintenance worker. Only
   the worker joins the `docker` group.
3. Generated code runs in the existing sandbox image through Docker's `runsc`
   runtime. Ordinary Docker and the fake adapter remain forbidden in production.
4. Caddy terminates TLS and proxies only to loopback port 8000 with immediate SSE
   flushing. Port 8000 and PostgreSQL are never public.
5. DigitalOcean Managed PostgreSQL 16 is preferred and restricted by trusted
   sources. Migrations remain an explicit pre-start release step.
6. Artifact bytes use a protected persistent host directory for the single-node
   deployment. Moving artifacts to shared object storage is required before
   horizontally scaling the API or workers.
7. Repository assets prove only their own presence and static correctness.
   Account, host, network, restore, and smoke-test checklist items require output
   from the actual deployment before they may be checked.

## Consequences

- The first deployment has one application-node failure domain; Droplet backups
  and a tested reconstruction procedure are required.
- Managed PostgreSQL separates durable database failure from the application
  host, but the filesystem artifact store still prevents safe horizontal scale.
- Docker-group membership is root-equivalent host authority. It is confined to
  the worker account/process and must not be granted to the API or maintenance
  service.
- A production application image and orchestrator can be added later, but must
  preserve host-path identity, gVisor isolation, and least-privilege Docker
  access or supersede this ADR.
- This decision adds deployment mechanism only. It does not alter or weaken any
  engineering-plan requirement or milestone gate.
