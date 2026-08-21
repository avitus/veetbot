# ADR-0048: Atomic CircleCI production delivery

- **Status:** Accepted
- **Date:** 2026-08-10

> **Updated by ADR-0067:** delivery also installs, starts, verifies, and restarts
> the credential-free execution service; references to three systemd processes
> are superseded.

## Context

ADR-0046 selected immutable releases below `/opt/veetbot`, a `current` symlink,
three systemd processes, explicit Alembic migrations, and a single DigitalOcean
Droplet. It deliberately stopped at an operator-run launch checklist. The live
Droplet already uses Nginx, and releases need a repeatable delivery path with an
observable commit identity.

The target behavior follows the useful parts of
[avitus/mankunku](https://github.com/avitus/mankunku): gate a `main` deployment
on CI, stage a timestamp-and-revision release, serialize host changes, promote a
single symlink, verify the exact revision publicly, retain a bounded release
set, and validate proxy configuration with a restorable backup.

Veetbot has different runtime boundaries. It uses Python and `uv`, three
systemd units instead of PM2, Alembic instead of a hosted database migration
job, and a release-specific gVisor sandbox image. It has no browser bundle or
end-to-end web test to package.

## Decision

1. CircleCI remains one `.circleci/config.yml`. The existing verification
   partitions remain authoritative. The static half of `make check` adds
   isolated deployment-script tests. Three post-gate delivery jobs package the
   source, deploy the application, and deploy Nginx; they add no milestone gate.
2. Delivery runs only for `main` after static, contract, integration, and
   sandbox jobs pass. The source archive is produced from the tested commit by
   `git archive`, accompanied by a SHA-256 checksum and its release identity,
   and transferred over SSH. Application and Nginx jobs share a CircleCI serial
   group as well as the server lock; a failed-job rerun reuses the packaged
   identity and never extracts over an already active release.
3. A release is named `YYYYMMDD-HHMMSS-<revision>`. The server stages it below
   `/opt/veetbot/releases`, takes an exclusive deployment lock, refuses a
   release older than the active timestamp, installs a release-local locked
   environment, builds a revision-tagged sandbox image, starts PostgreSQL,
   applies Alembic migrations, and runs production preflight checks before
   changing `/opt/veetbot/current`.
4. Promotion replaces the `current` symlink, retags the release image as
   `agent-core-sandbox:production`, installs the checked-in systemd units, and
   restarts the API, worker, and maintenance worker. The release succeeds only
   when all units are active, every main process runs from the promoted release,
   and the local readiness probe reports the release identity.
5. `VEETBOT_RELEASE_ID` is written into a release-local environment file. The
   unauthenticated health probes preserve their existing minimal bodies and add
   `X-Veetbot-Release` so CircleCI can require the exact revision through
   `https://api.veetbot.com`.
6. Five releases are retained. A failure before promotion removes only the
   validated staged release. A failure after promotion is left visible for
   operator diagnosis; there is no automatic rollback because a completed
   database migration may not be backward compatible.
7. Nginx configuration is versioned under `nginx/`. Its delivery job follows
   the application job and takes the same host deployment lock. It verifies that
   the matching application release is still active before reconciling the
   virtual host, so an older concurrent pipeline cannot overwrite a newer proxy
   revision. The configuration is backed up, installed, checked with `nginx -t`,
   and reloaded. Validation or reload failure restores the previous file and
   attempts to reload it.
8. The `veetbot-production` CircleCI context owns only deployment coordinates
   and the pinned SSH host-key record. The SSH private key is attached through
   CircleCI's project key facility and is dedicated to Veetbot rather than
   shared with another deployment. API bearer tokens, database credentials, and
   model-provider credentials remain solely in
   `/etc/veetbot/veetbot.env` on the server.
9. The production API listens only on `127.0.0.1:8000`; Nginx is the sole public
   HTTP entry point.

This decision supersedes ADR-0046's choice of Caddy for this host and the fixed
total-job wording in ADR-0025. It preserves ADR-0046's host-native topology and
ADR-0025's verification partitions and single CircleCI configuration file.

## Consequences

- A merge to `main` becomes a production-changing action after all required
  checks pass. Branch protection, CircleCI context restrictions, deploy-key
  rotation, and review of deployment scripts are therefore security controls.
- The deploy account can execute reviewed release and Nginx operations with
  elevated host authority. Its SSH key and sudo policy must not be shared with
  application clients.
- Releases and Python environments roll back by symlink, but the PostgreSQL
  schema does not roll back automatically. Operators must judge compatibility
  before selecting an older release.
- The Nginx source assumes the existing Let's Encrypt certificate paths for
  `api.veetbot.com`; certificate issuance remains an operator prerequisite.
- This replicates Mankunku's delivery invariants without adopting its dynamic
  path-filter setup configuration, PM2 process model, Node build cache,
  frontend asset pool, or Supabase-specific migration workflow. Reapplying the
  small Nginx site on each successful release makes concurrent-pipeline ordering
  explicit and safe.
- No engineering-plan security requirement or milestone gate is weakened.
