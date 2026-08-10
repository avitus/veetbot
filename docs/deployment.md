---
title: Production Deployment
---

# Minimal DigitalOcean launch

This is the shortest supported path to launch Veetbot on one Ubuntu Droplet.
The Droplet runs PostgreSQL, the API, the durable worker, the maintenance worker,
Caddy, Docker, and gVisor. There is no load balancer, managed database, cloud
firewall requirement, monitoring requirement, backup requirement, or
high-availability layer in this initial topology.

Token authentication and gVisor remain mandatory because production startup
refuses development authentication and ordinary Docker/fake sandboxes. Caddy
provides the normal HTTPS endpoint. Without a firewall the API's direct port
8000 is also publicly reachable; clients must not use it because doing so sends
the bearer token over plaintext HTTP.

## What is already done

- [x] Locked application dependencies.
- [x] Production environment template.
- [x] Single-node PostgreSQL restart overlay.
- [x] API, worker, and maintenance systemd units.
- [x] Caddy HTTPS/SSE proxy template.
- [x] Production preflight command and `make production-check` target.
- [x] gVisor is wired into the production composition.
- [x] `make check` passes: formatting, linting, strict typing, 313 static tests,
  134 contract tests, and documentation validation.
- [x] All 533 non-live tests pass against PostgreSQL 16.
- [x] All 10 Docker sandbox security tests pass locally. The Droplet must still
  prove the same image runs with gVisor.

## Minimum launch checklist

### DigitalOcean

- [ ] Point a domain or subdomain at the Droplet's public IP.
- [ ] Confirm inbound TCP ports 22, 80, and 443 are reachable. No DigitalOcean
  Cloud Firewall is required by this runbook.

### Install the host

- [ ] Install Python 3.12, `uv`, Git, Docker Engine, Caddy, and stable `runsc`.
- [ ] Create the service account and directories:

  ```bash
  sudo useradd --system --create-home --shell /usr/sbin/nologin veetbot
  sudo usermod -aG docker veetbot
  sudo mkdir -p /opt/veetbot/releases /etc/veetbot /var/lib/veetbot/artifacts
  sudo chown -R veetbot:veetbot /opt/veetbot /var/lib/veetbot
  sudo chmod 0700 /var/lib/veetbot/artifacts
  ```

- [ ] Verify gVisor:

  ```bash
  docker run --rm --runtime=runsc hello-world
  ```

### Install Veetbot

- [ ] Clone this branch or the release tag into
  `/opt/veetbot/releases/<commit>`, then create the active symlink:

  ```bash
  sudo ln -sfn "/opt/veetbot/releases/<commit>" /opt/veetbot/current
  cd /opt/veetbot/current
  uv sync --frozen
  docker build -f execution/sandbox.Dockerfile \
    -t agent-core-sandbox:production .
  ```

- [ ] Copy `deploy/veetbot.env.example` to `/etc/veetbot/veetbot.env`. Replace
  every `REQUIRED_` value, add the one model-provider key you will use, and
  protect the file:

  ```bash
  sudo chown root:veetbot /etc/veetbot/veetbot.env
  sudo chmod 0640 /etc/veetbot/veetbot.env
  ```

### Start PostgreSQL and migrate

- [ ] Start the repository's PostgreSQL 16 container using the protected
  production environment. It binds only to loopback:

  ```bash
  cd /opt/veetbot/current
  sudo docker compose --env-file /etc/veetbot/veetbot.env \
    -f docker-compose.yml -f deploy/docker-compose.production.yml \
    up -d postgres
  sudo -u veetbot sh -c '
    cd /opt/veetbot/current
    set -a
    . /etc/veetbot/veetbot.env
    set +a
    .venv/bin/alembic upgrade head
    .venv/bin/python scripts/check_production_deployment.py
  '
  ```

### Start the application

- [ ] Install and start the three systemd units:

  ```bash
  sudo cp deploy/systemd/*.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now veetbot-maintenance veetbot-worker veetbot-api
  ```

- [ ] Replace `agent.example.com` in `deploy/Caddyfile.example`, install it,
  and reload Caddy:

  ```bash
  sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
  sudo caddy validate --config /etc/caddy/Caddyfile
  sudo systemctl reload caddy
  ```

### Confirm launch

- [ ] Both health probes return 200 through the public hostname:

  ```bash
  : "${PRODUCTION_HOSTNAME:?set PRODUCTION_HOSTNAME}"
  curl --fail --show-error --connect-timeout 5 --max-time 10 \
    "https://${PRODUCTION_HOSTNAME}/health/live"
  curl --fail --show-error --connect-timeout 5 --max-time 10 \
    "https://${PRODUCTION_HOSTNAME}/health/ready"
  ```

- [ ] An authenticated API request succeeds.
- [ ] Submit one run and confirm the worker completes it.
- [ ] Submit one generated-code task and confirm it executes with `runsc`.
- [ ] Reboot the Droplet once and confirm PostgreSQL and all four services return.

## Explicitly deferred launch protections

This minimal release accepts a single-server failure domain and possible total
data loss. Cloud firewalling, restricted SSH source ranges, off-host database,
backups, restore rehearsal, monitoring, alerts, load balancing, rolling deploys,
and high availability are deferred. Direct public access to port 8000 can expose
the bearer token over plaintext HTTP, and any other accidentally listening
service may also be reachable. Add network filtering before the deployment
handles data or availability that cannot be recreated.
