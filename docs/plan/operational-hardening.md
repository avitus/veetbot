---
title: Operational Hardening
status: design
canonical: true
---

# Operational hardening

This document specifies Milestone 15. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md), and it hardens the deployment the
corpus already has — the host-native single Droplet of ADR-0046, the atomic
CircleCI delivery of ADR-0048, the loopback-only PostgreSQL and reverse proxy,
the systemd roles, and the Milestone 12 notification outbox — rather than
replacing any of it.
[ADR-0065](../adr/0065-milestone-15-operational-hardening.md) records the
architectural decisions; ADR-0061 records the authorization.

The deployment page states the risk plainly: the single-server failure domain
has "no required cloud firewall, off-host database, backup, restore rehearsal,
monitoring, load balancer, rolling deployment, or high availability. Loss of
the Droplet may mean unrecoverable data loss" (deployment.md:498-505). ADR-0046
accepted that at launch as an explicit decision, not a claim that the controls
lack value. Milestone 15 converts "unrecoverable" into "recoverable within the
backup window" and makes the owner aware when production degrades, and it
leaves Section 2.6's exclusions — Kubernetes, microservices, a load balancer,
rolling deployment, a second queue — exactly where they are.

## Scope

Milestone 15 delivers five things, in two independently default-off tranches:

- **Backup and restore** (no dependency on Milestones 12 through 14): a
  scripted, encrypted, off-host daily backup of the database, the artifact
  store, and the browser-profile ciphertext with a manifest; and a restore
  rehearsal that restores into a throwaway database, asserts the schema
  revision, probes the data, records a verdict, and runs automatically on the
  host and on demand from the off-host copy.
- **Health and alerting** (after Milestone 12): a host health check on a
  closed signal list that delivers deduplicated alerts and recoveries through
  the notification outbox, plus an independent external channel for the
  states that take the outbox down with them.
- **Network and host hardening**: cloud and host firewall, SSH hardening,
  reverse-proxy rate limits, and structural proofs that every listener but
  the proxy and SSH is loopback-only.
- **Rollback**: one command that promotes the previous retained release,
  verifies the release identity, refuses to cross a schema boundary without an
  explicit override, and never downgrades the schema; and a pre-migration local
  dump in the release path.
- **Liveness**: systemd watchdog supervision for the worker roles.

The milestone does not include high availability or a second node, an
off-host or managed database, Kubernetes, microservices, a load balancer,
rolling or blue-green deployment, a second queue, the S3-compatible
*artifact-store* adapter (backups use object storage; the artifact port does
not, and that adapter remains roadmap item B10), an OTLP or metrics service, or
a general alerting framework. The single failure domain remains an accepted
limitation; what changes is that its failure is recoverable and noticed.

## The boundary: backup and alerting run beside the application, not inside it

Backup must work when the application is down, mid-failed-migration, or with a
broken virtual environment, and the credential that writes to off-host
storage must not live in an application process. Alerting must work when the
database is the thing that is down. So both are host jobs under systemd
timers, with their own environment files, and neither is a maintenance-worker
sweep:

```text
veetbot-backup.timer -----> backup.sh ---- pg_dump, tar, manifest, age ----> off-host bucket
                                  |                                              |
                                  +---- restore-rehearsal.sh (staging copy)      |
                                                                                 v
veetbot-healthcheck.timer --> healthcheck.sh ---- signals ----> ops_alert --> notify role --> phone
                                  |
                                  +---- dead-man ping / external uptime check (DB-independent)
```

This yields four load-bearing invariants:

1. The backup set is declared by a manifest and structurally tested; nothing
   under the secrets directories, the certificate store, or the release tree
   ever enters the archive, and plaintext staging never survives a run.
2. A backup that has not been restored is not a backup: every host run
   rehearses its own staging copy before upload, and the owner rehearses the
   off-host copy with the escrowed key on a schedule that is recorded.
3. Alerts for degraded states travel the durable outbox; alerts for dead
   states travel a channel that does not depend on the database, the disk,
   or the host being up.
4. Rollback is code-only and migrations are forward-only; crossing a schema
   boundary is a restore plus a rollback, and the startup revision assertion
   remains the fail-closed backstop.

## The backup set

The manifest declares exactly three components:

```text
postgres.pgdump        pg_dump -Fc of the one database, taken over loopback
                       by a host-installed client pinned to the server's
                       major version; includes alembic_version
artifacts.tar.zst      the artifact store directory; small by construction
                       under the thirty-day default retention
browser-profiles.tar   the browser-profile-material volume — already
                       AES-256-GCM ciphertext; its keyring is NOT in the set
manifest.json          release id, alembic revision, PostgreSQL version,
                       per-component sha256 and size, row-count bands for
                       events, sessions, and runs, timestamp, host, and kind
                       (daily | weekly | pre-release)
```

Excluded, by rule and by structural gate: `/etc/veetbot/*.env`,
`/etc/veetbot/secrets/**`, the certificate store, the release tree (which is
reproducible from the repository and CI), and the proxy configuration (which
is versioned). Secrets are handled by a documented **manual escrow**: `make
secret-escrow` streams an `age`-encrypted tarball of the environment files,
the browser keyring, and the certificate material over SSH to the owner's
password manager or offline drive and stamps `/etc/veetbot/.escrow-stamp`; the
health check warns when any secret file is newer than the stamp. The reason is
blast radius: the bucket's access surface is the cloud account, the host's is
SSH, and keeping credentials out of the bucket means a bucket compromise is
not a live-credential compromise. The consequence for the browser keyring is
stated rather than hidden: a restore without the escrowed keyring yields
profiles that fail their authentication tag and must be revoked and re-logged
through the existing login ceremony; no plaintext is ever lost to the bucket.

## Destination, encryption, retention, and schedule

The destination is DigitalOcean Spaces, S3-compatible, in a different region
from the Droplet, written with `rclone` — a single static binary with
multipart upload and server-side lifecycle. Droplet snapshots were compared
and kept only as a zero-code complement: a snapshot is whole-disk and
crash-consistent only (PostgreSQL in a container may be mid-write), includes
every other application on a shared Droplet and the unencrypted secrets under
the provider's keys, cannot restore one database, cannot be rehearsed without
creating a Droplet, and lives only inside the cloud account. The plan's line
"S3-compatible object storage later" (Section 2.2) is used here for backups
first.

Encryption at rest is `age` with an X25519 recipient: the host holds only the
public recipient in its backup environment file; the private identity lives
off-host with the owner, in a password manager and on paper. `age` over a
keyring-based tool because it is one binary with no keyring state, streams,
and authenticates. The consequence is deliberate: the host cannot decrypt its
own off-host objects, so the on-host rehearsal runs on the pre-encryption
staging copy and the off-host copy is rehearsed from the owner's machine.

Retention is seven daily and four weekly objects — Sunday's object is also
copied under a weekly prefix — enforced by a pure retention function in the
script, property-tested, and mirrored by the bucket's lifecycle rules
(`daily/` eight days, `weekly/` thirty-five). The security page states the
consequence: content erased through the deletion contract persists in backups
for up to thirty-five days.

The runner is `veetbot-backup.timer`, daily at 03:30 UTC with a randomized
delay and `Persistent=true`, driving a one-shot `veetbot-backup.service` as a
dedicated `veetbot-backup` system identity — not the `veetbot` application
identity, and never a member of the `docker` group: deployment.md and ADR-0067
keep Docker away from application identities and reserve it for the deploy
and execution identities, and the backup identity is neither — with
`EnvironmentFile=/etc/veetbot/veetbot-backup.env` only: the database URL, the
bucket endpoint and scoped key, the age recipient, and the dead-man ping
address, and never a provider key. The identity holds exactly the access the
set requires and nothing the application identity has: the database over
loopback through the host-installed `pg_dump` (it never enters the compose
container and needs no Docker socket), read-only access to the artifact store
and to the browser-profile ciphertext granted to that identity alone by ACL
rather than by membership in the application group, and write access only to
its staging directory. No privilege escalation is needed. Staging is
`/var/lib/veetbot/backup/<stamp>/`, mode `0700`, owned by the backup identity,
removed after verified upload.

`release.sh` gains one step: `backup.sh --kind pre-release --no-upload`
immediately before `alembic upgrade head`. The pre-release backup follows
every rule above except upload: it is encrypted to the same `age` recipient
before retention, kept under the protected path
`/var/lib/veetbot/backup/pre-release/` (mode `0700`, owned by the backup
identity), its plaintext staging is removed the same way, and the two most recent
are retained with older ones deleted by the same run. No plaintext dump
survives this step either. This is the cheap insurance that makes "restore
the pre-release dump, then roll back the code" a minutes-long operation; the
restore needs the owner's `age` identity, which is acceptable because crossing
a schema boundary is an owner operation, and the open question about a
root-only host copy of the identity applies here too.

## Integrity and the restore rehearsal

After the dump, `pg_restore --list` must be non-empty and name
`alembic_version`; each component is hashed; the manifest is written. After
upload, the objects are checked against the manifest; only then is plaintext
staging removed. Without a configured recipient the script refuses to upload
at all. Success pings the dead-man address.

`restore-rehearsal.sh <backup-dir | object-url>`:

1. Starts a throwaway PostgreSQL from the repository's compose file under a
   distinct project name on a random loopback port, and refuses to run if the
   project name, port, or database URL equals production's.
2. Restores with `pg_restore --exit-on-error`.
3. Asserts `alembic current` equals the manifest's revision — the backup's own
   identity, which a retained backup keeps after later migrations move the
   repository head — and then checks that revision against the code the
   rehearsal runs from. The host's pre-upload rehearsal and CI rehearse the
   backup they just took, so there the revision also equals the checkout's
   head, the same rule the production preflight applies
   (`scripts/check_production_deployment.py`); an older retained backup is
   rehearsed from the release its manifest names, and a revision behind the
   current head is reported in the verdict, never treated as failure.
4. Probes: `alembic_version` has one row; each critical table selects; the
   `events`, `sessions`, and `runs` counts fall inside the bands the backup
   recorded before and after the dump; the artifact archive lists and its hash
   matches.
5. Tears the throwaway database down, always, and writes `verdict.json`.

"Rehearsal passed" means all five, exit zero, and `passed: true` in the
verdict. Three tiers run it: CI's integration job runs the scripts against its
PostgreSQL sidecar with synthetic data, which proves the scripts; every host
backup run rehearses its own staging copy before upload and writes a
`last-success` marker the health check reads; and the owner runs
`make restore-rehearsal BACKUP=<object-url>` monthly on a machine holding the
age identity and records the result in the verification history. Only the
owner's tier proves the off-host copy and the escrow, and the document says
so.

## Health and alerting

The signal list is closed and its thresholds live in versioned configuration.
`agent ops probe --json`, run in the maintenance role's environment, produces
the database-derived signals; the shell produces the host signals:

```text
signal                      source                          threshold
--------------------------  ------------------------------  ---------------------------
readiness_local             GET /health/ready on loopback   200 and X-Veetbot-Release ==
                                                            the current release id
readiness_public            GET /health/ready via the proxy 200
tls_days_left               api. and browser. certificates  < 14 days
units_active                systemctl is-active for every   any inactive
                            unit release.sh restarts, the
                            timers, and docker
units_failed                systemctl --failed              any
worker_watchdog             NRestarts deltas on the worker  any restart since last check
                            units
queue_lag                   oldest QUEUED run age; stale     configured seconds
                            leases
schedule_lag                Milestone 11 due-lag metric      configured seconds
notify_backlog              outbox depth, oldest pending    configured depth / age
disk_free                   /, /var/lib/docker,             85% warn, 95% critical
                            /var/lib/veetbot
postgres_ready              pg_isready                      any failure
backup_fresh                last-success marker age         > 26 hours
rehearsal_verdict           last verdict.json               passed != true
secrets_unescrowed          secret file mtime vs stamp      any newer
reboot_required             /var/run/reboot-required        present
release_rollback            rollback.sh                     informational: one per
                                                            rollback, keyed by the
                                                            promoted release id
```

`veetbot-healthcheck.timer` fires every five minutes and drives a one-shot
`veetbot-healthcheck.service` as the `veetbot` user with its own environment
file (the database URL, thresholds, and the dead-man address; no bearer or
provider key). A failing signal enqueues an `ops_alert` notification — a kind Milestone
12's closed catalog already declares for this producer, with its four ops-only
payload fields — into the Milestone 12 outbox with the tenant- and
episode-scoped key `ops.<tenant_id>.<signal>.<episode>` and a cool-down
(default six hours); a signal that clears enqueues one `ops_recovered` keyed
`ops.<tenant_id>.<signal>.<episode>.recovered`. An *episode* is the
per-signal counter in `/var/lib/veetbot/healthcheck/state.json` that
increments each time a signal fails after having recovered, so a signal that
recovers and fails again is a new alert and a past recovery can never
deduplicate the next episode away. The same state file holds the cool-down. The payload is a closed schema — signal,
severity, release id, and a reason code from a checked-in table — and never an
environment value or a path, the discipline the tool failure vocabulary
already follows. Delivery is the `notify` role over APNs, so the phone rings.

Using the outbox is right for degraded states and wrong for dead states: the
database, the disk, or the host being down takes the outbox with it. The
independent channel is the cheapest viable pair: an external uptime check on
the public readiness URL expecting 200, with certificate-expiry alerting, and a
dead-man's switch that the backup pings on success and that the health check
pings at start and at failure with a one-line summary when it cannot reach the
database — so a database outage still produces an email within minutes. The
cloud provider's built-in Droplet monitoring alerts for CPU, memory, and disk
are enabled as a zero-code complement.

OpenTelemetry stays local. The repository depends on the OpenTelemetry API and
SDK and on no exporter; structured logs go to the journal with persistent
storage and a size cap, the proxy logs rotate, and `OTEL_EXPORTER_OTLP_ENDPOINT`
remains the seam a future exporter would use. A hosted metrics service is not
worth a credential and an egress path for a single-owner host.

## Network and host hardening

- A cloud firewall applied by `deploy/firewall/apply.sh` from a committed
  `deploy/firewall/rules.json`: inbound 22, 80, and 443 over TCP and ICMP
  only; outbound open, because provider APIs, push, Telegram, and the backup
  bucket are all egress, and sandbox egress is already governed by gVisor and
  the egress policy.
- The host firewall with the same three ports as the belt-and-braces layer;
  it is bypassed by published container ports, which is exactly why the
  loopback-only structural gate below matters.
- SSH key-only with root password login disabled, a brute-force jail on the
  SSH service, and unattended security upgrades with automatic reboot off; the
  health check flags a pending reboot.
- Proxy request and connection rate limits on both TLS server blocks, generous
  for the event stream.
- A structural gate keeps the API bind, every published container port, and
  the browser service loopback-only, as the toolchain tests already assert
  for the API and the proxy upstreams.
- Host-only items — the cloud firewall, the jail, the upgrade policy — are a
  checklist with recorded evidence, under ADR-0046's rule that repository
  assets prove only themselves.

## Rollback

`deploy/app/rollback.sh [target-id] [--accept-forward-schema] [--dry-run]`:

1. Takes the same deployment lock `release.sh` takes.
2. Defaults the target to the newest retained release older than the current
   one; requires its directory, release file, virtual environment, and
   revision-tagged sandbox image (rebuilding the image from the retained tree
   if absent).
3. Compares the target's migration heads with the database's current revision;
   if the database is ahead, refuses, naming both revisions, unless
   `--accept-forward-schema` is passed; never invokes a downgrade in either
   path.
4. Promotes through the same atomic symlink swap, retags, restarts the same
   unit list with the same arguments (so the committed sudoers contract needs
   no new rule), verifies the public release header and each unit's process working directory
   exactly as `release.sh` does, appends to a rollback log, and enqueues an
   `ops_alert` with the declared `release_rollback` signal, severity `info`, the
   promoted release id, the `ops.release_rollback` reason code, and key
   `ops.<tenant_id>.release_rollback.<release_id>`.

The policy is recorded in ADR-0065 and matches what the persistence design
already says: rollback is code-only; migrations are forward-only; the
migration round-trip gate proves downgrade *authoring*, not operational
safety (event-log-and-persistence.md:962-973); a rollback across a migration
boundary is "restore the pre-release dump, then roll back the code"; and the
startup revision assertion remains the fail-closed backstop.

## Liveness

Each worker role's unit becomes `Type=notify` with a watchdog interval; a
fifteen-line notify helper sends a keepalive once per loop iteration in
`run_forever`, so a stalled loop is restarted by systemd and counted, and the
health check reports restart deltas. The API keeps its readiness route as its
liveness signal.

## ADR-0046, amended

ADR-0046 is amended in place rather than rewritten: its header records that
decision 4 (Caddy) was superseded by ADR-0048 (Nginx) and that decision 7 — no
required firewall, monitoring, alerts, backups, or restore rehearsal at launch
— remains the current production posture until this milestone completes; an
amendments section carries the dates and names this document as the
specification that will supersede it. Nothing in ADR-0046 is marked superseded
by controls that are only specified, and ADR-0065 will supersede decision 7
only; it does not supersede ADR-0048.

## Configuration and deployment

Both tranches are default-off: `VEETBOT_BACKUP_ENABLED=0` and
`VEETBOT_HEALTHCHECK_ENABLED=0` in the deployment environment template, each
with its own environment file (`veetbot-backup.env`, `veetbot-healthcheck.env`)
and timer unit, validated and installed by `release.sh` the way the schedule
unit is, and the watchdog lines in the worker units. No provider, bearer, or
object-store credential enters an application process that did not already
have it; the bucket credential lives only in the backup environment file. The
deployment page's "Accepted limitations" section is rewritten to the residual
list: one failure domain, recoverable within the backup window, with the
controls above.

## Tracked metrics

Track:

- backup runs, duration, bytes, component counts, upload verification;
- rehearsal verdicts by tier and the age of the last passing one;
- health-check evaluations, signals failing, alerts enqueued, recoveries,
  cool-downs suppressed;
- dead-man pings sent and missed; external uptime-check status as recorded;
- rollbacks performed and refused, with the schema comparison outcome;
- watchdog restarts per unit.

Metrics carry no secret, path, or environment value.

## Build sequence

1. Add `backup.sh`, the manifest, the retention function, and their shell and
   property tests against a compose PostgreSQL with seeded data. **M15.**
2. Add `restore-rehearsal.sh`, the verdict, the corruption regressions, and
   the production-refusal guard; wire the CI integration job. **M15.**
3. Add the backup timer, environment file, release validation, the
   pre-release dump in `release.sh`, and the escrow target. **M15.**
4. Add `agent ops probe`, `healthcheck.sh`, the `ops_alert` kind on the
   Milestone 12 outbox with cool-down state, the dead-man pings, and the
   timer. **M15.**
5. Add `rollback.sh` and its tests, the sudoers reconciliation, and the
   watchdog lines and notify helper for the worker roles. **M15.**
6. Add the firewall rules and apply script, the host firewall and SSH
   checklist, the proxy rate limits, and the loopback structural gate.
   **M15.**
7. Amend ADR-0046, rewrite the deployment page's limitations, add the
   security page's operational-hardening section, and run the full suite,
   deployment-script tests, hosted CI, and the required GitHub CodeRabbit loop
   on one final head; record the owner's first off-host rehearsal. **M15.**

## Hard gates

1. **The backup set is exactly the declared set.** The manifest names the
   database dump, the artifact archive, and the browser-profile ciphertext and
   nothing else; nothing under the secrets directories, the certificate store,
   or the release tree enters the archive. Registered as
   `gate.ops.backup_set_complete`, structural. **M15.**
2. **A backup round-trips.** Against a seeded compose PostgreSQL, `backup.sh`
   emits a custom-format dump that passes `pg_restore --list`, names
   `alembic_version`, and a manifest whose hashes verify. Registered as
   `gate.ops.backup_roundtrip`, case. **M15.**
3. **The restore rehearsal passes on a good backup.** Restoring into a fresh
   database yields `alembic current` equal to the manifest's revision and to
   the head of the release the manifest records, count bands and the artifact
   hash hold, the verdict is `passed`, and the throwaway database is torn
   down. Registered as `gate.ops.restore_rehearsal_passes`, case. **M15.**
4. **The rehearsal detects corruption.** A truncated dump, flipped bytes, and
   a wrong `alembic_version` each fail with a distinct reason and no passing
   verdict. Registered as `gate.ops.restore_rehearsal_detects_corruption`,
   case. **M15.**
5. **Backups are encrypted before they leave the host, and retained ones
   too.** With a recipient the uploaded object is an `age` envelope and
   plaintext staging is removed; a retained pre-release backup is an `age`
   envelope under the protected path with no plaintext beside it; without a
   recipient the upload fails closed. Registered as
   `gate.ops.backup_encrypted_offhost`, case. **M15.**
6. **Retention keeps seven daily and four weekly.** Over generated object
   listings the retention function keeps exactly that set, never deletes the
   newest object, and the lifecycle rules match. Registered as
   `gate.ops.backup_retention_policy`, property. **M15.**
7. **The rehearsal never touches production.** It refuses the production
   compose project name, port, and database URL, and the stub log shows no
   teardown against the production project. Registered as
   `gate.ops.rehearsal_never_touches_production`, case. **M15.**
8. **The health check reports every declared signal.** From a seeded database
   and host fixture the probe reports each signal as a typed field with its
   threshold taken from versioned configuration. Registered as
   `gate.ops.healthcheck_signals`, case. **M15.**
9. **Alerts are enqueued once per cool-down and recover once.** Repeated
   failing evaluations inside the cool-down enqueue one `ops_alert` under the
   tenant- and episode-scoped key; after it, a second; a cleared signal
   enqueues one `ops_recovered` under the episode's recovery key; a signal
   that fails again after recovering opens a new episode and a new alert —
   against the in-memory outbox. Registered as
   `gate.ops.alert_enqueued_deduped`, case. **M15.**
10. **Alert payloads are closed.** Alert bodies match a closed schema with
    table-sourced reason codes, and the secret-pattern families find nothing
    in generated payloads. Registered as `gate.ops.alert_payload_closed`,
    structural. **M15.**
11. **A dead database still raises an alarm.** With the database unreachable
    the health check exits non-zero inside its timeout, logs, and pings the
    dead-man failure address rather than hanging. Registered as
    `gate.ops.db_down_fallback`, case. **M15.**
12. **Rollback promotes the previous release.** With releases A then B,
    rollback promotes A, retags, restarts the unit list, verifies the release
    header as A, keeps B, and refuses when there is no previous release or the
    target is current. Registered as `gate.ops.rollback_promotes_previous`,
    case. **M15.**
13. **Rollback refuses schema drift without the override.** A database
    revision ahead of the target's heads exits non-zero naming both; with the
    override it proceeds; no path ever invokes a downgrade. Registered as
    `gate.ops.rollback_refuses_schema_drift`, case. **M15.**
14. **Units and the sudoers contract reconcile.** Every systemd unit in the
    deployment tree is in the release unit list, the schedule conditional, or
    a declared host-unit list; the rollback unit list equals the release unit
    list; the sudoers contract covers rollback and the timer installation.
    Registered as `gate.ops.units_and_sudoers_reconciled`, structural.
    **M15.**
15. **The public boundary is minimal.** Every published container port and the
    API bind are loopback; the firewall rules admit only the three inbound
    ports; the proxy declares rate limits on both TLS server blocks.
    Registered as `gate.ops.public_boundary_minimal`, structural. **M15.**
16. **Workers run under a watchdog.** Each worker role's loop emits a keepalive
    per iteration and is stopped when the loop stalls under a fake clock; the
    unit files declare the notify type and the watchdog interval. Registered
    as `gate.ops.worker_watchdog`, case. **M15.**

## Open questions

1. Whether the backup and restore tranche should be pulled ahead of Milestones
   12 through 14. It has no dependency on them and the risk it addresses is
   live; the ordering is the owner's.
2. Whether the age identity should also exist as a root-only copy on the host,
   which would allow a nightly full off-host rehearsal at the cost of the host
   being able to read its own backups.
3. Retention: seven daily and four weekly, or also a few monthly; and the
   acceptance of erased content surviving up to thirty-five days in backups.
4. Alert routing: push only, or also Telegram once Milestone 14 exists; the
   outbox makes a second destination a configuration change.
5. Whether an OTLP exporter is ever worth a credential for a single-owner
   host.
