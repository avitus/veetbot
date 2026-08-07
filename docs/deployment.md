---
title: Production Deployment
---

# Production deployment on a DigitalOcean Droplet

This runbook deploys one release to an Ubuntu Droplet, uses DigitalOcean
Managed PostgreSQL, runs the API, durable worker, and maintenance worker under
systemd, terminates TLS with Caddy, and executes generated code with gVisor.
ADR-0046 records why this is the first supported production topology.

The repository does not deploy itself and startup never runs migrations. An
operator promotes an immutable commit, runs the migration, validates the host,
and then restarts the three processes.

## Evidence checklist

Checked entries are satisfied by versioned repository assets. Host and account
entries stay unchecked until an operator records their output for the actual
Droplet.

### Release assets

- [x] Locked Python dependency graph (`uv.lock`).
- [x] Production environment template (`deploy/veetbot.env.example`).
- [x] Separate API, worker, and maintenance systemd units (`deploy/systemd/`).
- [x] HTTPS and SSE-aware Caddy template (`deploy/Caddyfile.example`).
- [x] Production preflight command (`scripts/check_production_deployment.py`).
- [x] Explicit `make production-check` operator target.
- [x] gVisor-backed sandbox selection is wired through the composition root.
- [x] Database migrations are separate from process startup.
- [x] `make check` passes on the deployment branch (formatting, linting, strict
  typing, 311 static tests, 134 contract tests, and documentation validation).
- [x] `make test` passes against PostgreSQL 16 (533 non-live tests).
- [x] `make test-sandbox` passes with the real Docker adapter (10 sandbox
  security tests). The target Droplet must repeat these with gVisor.
- [ ] Tag the exact reviewed commit selected for production.
- [ ] Run the selected live-provider smoke test against the release.

### DigitalOcean account and network

- [ ] Attach a Cloud Firewall: SSH only from administrator CIDRs; HTTP and HTTPS
  from intended clients; never expose 5432 or 8000.
- [ ] Enable VPC networking, Droplet backups, and the metrics agent.
- [ ] Create CPU, load, memory, and disk alerts with an attended destination.
- [ ] Create the DNS A/AAAA record for the production hostname.
- [ ] Provision PostgreSQL 16 with automated backups and restrict its trusted
  sources to the application Droplet or VPC.
- [ ] Restore a database backup into a separate target and record the result.

### Host bootstrap

- [ ] Create a non-root `veetbot` service account and use key-only SSH.
- [ ] Install Python 3.12, `uv`, Docker Engine, Caddy, and the stable `runsc`
  package; enable unattended security updates.
- [ ] Verify `docker run --rm --runtime=runsc hello-world`.
- [ ] Create `/opt/veetbot/releases`, `/etc/veetbot`, and
  `/var/lib/veetbot/artifacts`; make the last two accessible only to the
  service account as appropriate.
- [ ] Install the release at `/opt/veetbot/releases/<commit>` and atomically
  point `/opt/veetbot/current` to it.
- [ ] Run `uv sync --frozen` and build the production sandbox image:
  `docker build -f execution/sandbox.Dockerfile -t agent-core-sandbox:production .`.

### Configuration and start

- [ ] Copy `deploy/veetbot.env.example` to `/etc/veetbot/veetbot.env`, replace
  every `REQUIRED_` value, set only necessary provider credentials, and set
  mode `0600`.
- [ ] Use a `postgresql+asyncpg://` database URL with TLS and verify the managed
  database's certificate policy from the Droplet.
- [ ] Run `uv run alembic upgrade head` as a distinct release step.
- [ ] From the release directory, load the production environment and run
  `uv run python scripts/check_production_deployment.py`.
- [ ] Install the three units from `deploy/systemd/`, run `systemctl daemon-reload`,
  enable them, and start maintenance, worker, then API.
- [ ] Replace the hostname in `deploy/Caddyfile.example`, install it as
  `/etc/caddy/Caddyfile`, validate it, and reload Caddy.

### Acceptance and recovery

- [ ] `/health/live` and `/health/ready` return 200 through HTTPS.
- [ ] Port 8000 is unreachable from the public internet.
- [ ] An unauthenticated protected request is rejected and an authenticated run
  completes through the separate worker.
- [ ] SSE delivers events without proxy buffering.
- [ ] Approval, cancellation, worker restart/resume, artifact authorization,
  memory, and knowledge smoke tests pass with production identity.
- [ ] A real generated-code call runs under `runsc`; the sandbox security suite
  passes on the Droplet.
- [ ] Logs contain no bearer token, provider key, prompt body, reasoning, or raw
  tool result.
- [ ] Stop and restore procedures have been rehearsed, including application
  rollback and database restoration. Alembic downgrade is not the production
  database rollback mechanism.

## Release sequence

Run these from the immutable release directory after loading the protected
environment file:

```bash
: "${PRODUCTION_HOSTNAME:?set PRODUCTION_HOSTNAME}"
uv sync --frozen
docker build -f execution/sandbox.Dockerfile -t agent-core-sandbox:production .
uv run alembic upgrade head
uv run python scripts/check_production_deployment.py
sudo systemctl restart veetbot-maintenance veetbot-worker veetbot-api
curl --fail --silent --show-error --connect-timeout 5 --max-time 10 \
  --retry 12 --retry-delay 5 --retry-all-errors \
  "https://${PRODUCTION_HOSTNAME}/health/live"
curl --fail --silent --show-error --connect-timeout 5 --max-time 10 \
  --retry 12 --retry-delay 5 --retry-all-errors \
  "https://${PRODUCTION_HOSTNAME}/health/ready"
```

Do not expose the service if the preflight command, readiness probe, sandbox
runtime test, or restore rehearsal has not passed.
