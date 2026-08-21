# ADR-0065: Milestone 15 operational hardening

- Status: Proposed
- Date: 2026-08-20
- Related: Sections 2.2, 2.6, 21, and 22 of the engineering plan; ADR-0025,
  ADR-0046 (amended by this decision), ADR-0048, ADR-0050, ADR-0058,
  ADR-0061, ADR-0062
- Detailed design: `docs/plan/operational-hardening.md`

## Context

ADR-0046 accepted, at launch, a single host with no required firewall,
monitoring, alerts, backups, or restore rehearsal, and the deployment page
says plainly that loss of the Droplet may mean unrecoverable data loss.
ADR-0048 made delivery atomic and verified but left rollback manual and
migrations forward-only. The persistence design already states that rolling a
deployed schema backwards is a restore from backup, not a downgrade, and that
the migration round-trip gate proves authoring rather than operational safety.
Milestone 12 (ADR-0062) gives the platform a durable way to reach the owner's
phone.

The owner authorized Milestone 15 on 2026-08-20 (ADR-0061) with full milestone
treatment rather than backlog placement, because the data-loss risk is live
and the memory store is the owner's own.

## Proposed decisions

1. **Backup and alerting are host jobs, not application sweeps.** Systemd
   timers driving oneshot services with their own environment files; the
   bucket credential and thresholds live there and in no application process.
2. **The backup set is declared and secrets are escrowed, not backed up.**
   Database dump, artifact store, browser-profile ciphertext, and a manifest;
   environment files, the browser keyring, and certificates are excluded from
   automated off-host backup and handled by a documented manual `age` escrow
   with a staleness signal.
3. **Off-host object storage, encrypted client-side, with provider snapshots
   as a complement.** S3-compatible object storage in another region via
   `rclone`; `age` X25519 with the private identity held only by the owner;
   seven daily and four weekly objects enforced by a property-tested retention
   function and mirrored by lifecycle rules; provider weekly snapshots for fast
   host rebuild.
4. **A backup that has not been restored is not a backup.** Every host run
   rehearses its own staging copy before upload; CI rehearses the scripts
   against synthetic data; the owner rehearses the off-host copy monthly with
   the escrowed key and records it. "Passed" is a five-part verdict.
5. **A pre-migration local dump in the release path.**
6. **Degraded states alert through the outbox; dead states through an
   independent channel.** A closed signal list with versioned thresholds; an
   `ops.alert` kind with deduplication and cool-down delivered by the `notify`
   role; an external uptime check with certificate-expiry alerting and a
   dead-man's switch for the states that take the outbox down.
7. **OpenTelemetry stays local.** No exporter; journal with persistent storage
   and a size cap; the endpoint variable remains the future seam.
8. **Cloud and host firewall to 22, 80, 443; SSH hardening; proxy rate limits;
   a structural loopback-only gate.**
9. **Rollback is code-only and migrations are forward-only.** One command
   promotes the previous retained release through the same symlink swap and
   unit restart, verifies the release identity, refuses to cross a schema
   boundary without an explicit override, and never downgrades; crossing a
   boundary is a restore plus a rollback.
10. **Worker roles run under a systemd watchdog.**
11. **ADR-0046 is amended in place**: decision 4 superseded by ADR-0048,
    decision 7 superseded by this decision; ADR-0048 is untouched.
12. **One gate area, `ops`, sixteen gates**, named `ops` rather than `deploy`
    because ADR-0048 deliberately says delivery jobs add no milestone gate and
    the subject here is the operational lifecycle.

## Consequences

- "Unrecoverable" becomes "recoverable within the backup window", and
  degradation is noticed on the owner's phone or by email when the host is
  dark.
- Two timers, two environment files, two feature flags, a rollback script, a
  firewall rules file and apply script, proxy rate limits, watchdog lines in
  the worker units, a notify helper, an `ops.alert` notification kind, and a
  CI rehearsal job are added; `release.sh` gains a pre-migration dump and the
  unit list grows.
- The security page states that erased content can persist in backups for up
  to thirty-five days; the deployment page's accepted limitations shrink to
  the residual single failure domain.
- The owner must create the bucket and scoped key, hold the `age` identity,
  perform the first escrow, and run the first off-host rehearsal.

## Alternatives considered

- **Droplet snapshots as the only backup:** rejected; crash-consistent only,
  includes secrets under the provider's keys, cannot restore one database or
  be rehearsed cheaply.
- **Backups from the maintenance worker:** rejected; it must work when the
  application cannot, and the bucket credential must not live in an
  application process.
- **Encrypted inclusion of secrets in the backup set:** rejected in favour of
  manual escrow; a bucket compromise must not be a live-credential compromise.
- **A hosted metrics and alerting service:** rejected for a single-owner host;
  the outbox plus a free uptime check and dead-man's switch cover the two
  failure classes without a new credential.
- **Automatic schema downgrade on rollback:** rejected by the persistence
  design's standing decision.
