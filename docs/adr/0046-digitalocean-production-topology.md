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
4. Caddy terminates TLS directly on the Droplet and proxies to port 8000 with
   immediate SSE flushing. There is no load balancer.
5. PostgreSQL 16 runs from the repository's Compose file on the same Droplet and
   binds to loopback. Migrations remain an explicit pre-start release step.
6. Artifact bytes use a protected persistent host directory for the single-node
   deployment. Moving artifacts to shared object storage is required before
   horizontally scaling the API or workers.
7. The initial launch does not require a DigitalOcean Cloud Firewall, VPC,
   monitoring, alerts, backups, or a restore rehearsal. This is explicit human
   acceptance of the security, durability, and operational risk, not a claim
   that those controls lack value.
8. Repository assets prove only their own presence and static correctness. Host
   and smoke-test checklist items require output from the actual deployment.
9. On a shared Droplet, existing Docker and reverse-proxy installations are
   reused. The operator inventories listeners and containers before changes,
   chooses a free loopback PostgreSQL port, appends rather than replaces proxy
   configuration, and verifies other containers after the Docker restart needed
   to register `runsc`.

## Consequences

- The application, database, and artifact store share one failure domain. Loss
  of the Droplet may cause total and unrecoverable data loss.
- No firewall requirement means unnecessary services accidentally bound to a
  public interface may be reachable from the internet.
- The filesystem artifact store and local database prevent horizontal scale.
- Docker-group membership is root-equivalent host authority. It is confined to
  the worker account/process and must not be granted to the API or maintenance
  service.
- A production application image and orchestrator can be added later, but must
  preserve host-path identity, gVisor isolation, and least-privilege Docker
  access or supersede this ADR.
- This decision adds deployment mechanism only. It does not alter or weaken any
  engineering-plan requirement or milestone gate.
