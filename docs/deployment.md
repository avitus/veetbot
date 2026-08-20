---
title: Production Deployment
---

# Atomic DigitalOcean deployment

Veetbot deploys to one Ubuntu Droplet at `api.veetbot.com`. PostgreSQL, the API,
the durable worker, the maintenance worker, Docker with gVisor, and Nginx share
that host. CircleCI packages the tested `main` commit and promotes an immutable
release below `/opt/veetbot/releases`.

The flow adapts the useful deployment invariants from
[avitus/mankunku](https://github.com/avitus/mankunku) to Veetbot's Python and
systemd runtime. ADR-0048 records the differences and security boundary.

## Release flow

```text
static + contract + integration + sandbox
                    |
                    v
          package tested commit
                    |
                    v
        SSH stage + checksum verify
                    |
                    v
 lock -> uv sync -> image -> PostgreSQL -> migrate -> preflight
                    |
                    v
      current symlink + systemd restart
                    |
                    v
 release.sh: local release header + API contract probe
                    |
                    v
 CircleCI: public release header
```

Each release is named `YYYYMMDD-HHMMSS-<7-character-commit>`. The server:

1. takes `/opt/veetbot/shared/deploy.lock`;
2. refuses a timestamped release older than the currently active release;
3. installs a release-local `.venv` from `uv.lock` using a shared download
   cache;
4. builds `agent-core-sandbox:<release-id>`;
5. ensures the local PostgreSQL service is running;
6. applies `alembic upgrade head` and runs the production preflight;
7. switches `/opt/veetbot/current`, tags the sandbox image as `production`, and
   restarts all three systemd units;
8. requires the local readiness probe to return
   `X-Veetbot-Release: <release-id>`, makes an authenticated request to the
   authoritative session index, and requires every process to run from the
   promoted directory; and
9. retains the five newest valid releases.

A successful server release returns control to CircleCI, which polls the public
TLS readiness endpoint until it reports the same release ID. Exhausting that
bounded public-probe budget fails the CircleCI job after promotion; it is not a
failure inside `deploy/app/release.sh`.

A pre-promotion failure removes only its staged directory. A post-promotion
failure remains visible for diagnosis and manual rollback. Database migrations
are never downgraded automatically.

## One-time host preparation

Inventory the shared Droplet before changing it:

```bash
sudo ss -ltnp
sudo systemctl --type=service --state=running
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker --version
docker compose version
nginx -v
uv --version
df -h
free -h
```

Confirm port 8000 is free, choose a free loopback PostgreSQL port, and record all
existing Docker containers. Reuse the existing Docker and Nginx installations;
do not replace services used by other applications on the Droplet.

Create the service/deploy account and persistent paths:

```bash
sudo useradd --system --create-home --shell /bin/bash veetbot
sudo usermod -aG docker veetbot
sudo mkdir -p \
  /opt/veetbot/releases \
  /opt/veetbot/shared/uv-cache \
  /etc/veetbot \
  /var/lib/veetbot/artifacts
sudo chown -R veetbot:veetbot /opt/veetbot /var/lib/veetbot
sudo chmod 0700 /var/lib/veetbot/artifacts
```

The deploy key must log in as this account, so the account needs an executable
shell. Restrict that key in `authorized_keys` to the CircleCI source and disable
port, agent, and X11 forwarding. Install the committed sudo contract
`deploy/sudoers/veetbot-deploy` as `/etc/sudoers.d/veetbot-deploy` with mode
0440, replacing its placeholder user `deploy` with this account, and validate
with `visudo -c -f /etc/sudoers.d/veetbot-deploy`. The contract is the exact
union of the sudo commands `deploy/app/release.sh` and `deploy/nginx/deploy.sh`
invoke, and a repository test reconciles the scripts against it, so a script
gaining a sudo command without a rule fails CI rather than the production
release. That authority is effectively host administration; do not reuse the
key for application clients or interactive users.

Install stable `runsc` if it is not already registered. Preserve the existing
Docker configuration before the required daemon restart and verify all previous
containers afterward:

```bash
if sudo test -f /etc/docker/daemon.json; then
  sudo cp -a /etc/docker/daemon.json /etc/docker/daemon.json.before-runsc
fi
sudo runsc install
sudo systemctl restart docker
sudo docker info --format '{{json .Runtimes}}'
sudo docker ps
docker run --rm --runtime=runsc hello-world
```

Copy `deploy/veetbot.env.example` to `/etc/veetbot/veetbot.env`, replace every
`REQUIRED_` value, select the free PostgreSQL port in both locations, and add
`VEETBOT_OPENAI_KEY` for the shipped production `balanced` policy. A reviewed
model-policy overlay may instead retarget `balanced` to another configured
provider.

To enable the recommended public-web split, add the two provider credentials
and select each capability independently in that same root-owned file:

```text
WEB_SEARCH_PROVIDER=tavily
WEB_FETCH_PROVIDER=firecrawl
TAVILY_API_KEY=<production Tavily key>
FIRECRAWL_API_KEY=<production Firecrawl key>
```

Do not put the real values in `.env.example`, the production template, CircleCI
configuration, a commit, or a PR. The worker reads this environment when the
release restarts its systemd unit; the selectors default to `disabled` when the
capability is not intended for that deployment.

Run `docker compose ls` before the first automated release. If an existing
Veetbot PostgreSQL container was created under a Compose project name other than
`veetbot`, set `COMPOSE_PROJECT_NAME` to that exact existing name. Changing it
silently selects a different named volume and therefore an empty database.

```bash
sudo chown root:veetbot /etc/veetbot/veetbot.env
sudo chmod 0640 /etc/veetbot/veetbot.env
```

When scheduled runs are enabled, separately copy
`deploy/veetbot-schedule.env.example` to
`/etc/veetbot/veetbot-schedule.env`, fill its required database and identity
values, and protect it with the same ownership and mode. The schedule unit never
loads the shared environment because that file contains API, model-provider,
web-provider, and browser-profile credentials that the scheduler must not see.
The tenant-scoped database role must not have PostgreSQL `BYPASSRLS` authority:
schedule child-table isolation depends on the forced row-level-security policy
on `schedules` applying inside each child-policy lookup. Reserve `BYPASSRLS`
roles for explicit administration and never use one for API, worker, or
scheduler tenant access.

```bash
sudo chown root:veetbot /etc/veetbot/veetbot-schedule.env
sudo chmod 0640 /etc/veetbot/veetbot-schedule.env
```

Do not add `VEETBOT_RELEASE_ID` to that shared file. The release script writes
it to `/opt/veetbot/current/.release.env`, which only the API systemd unit loads.

### Browser profile service host prerequisites

The release script refuses to run until the browser-profile secrets exist,
even while `BROWSER_PROVIDER=disabled`, because the production compose file
always starts the hardened profile service and bind-mounts these paths
read-only. Provision them once:

```bash
umask 077
sudo install -d -m 0711 /etc/veetbot/secrets

# One token, two files: the container refuses secrets it does not own
# (uid 65532), and the agent units read their copy as the veetbot user.
credential="$(openssl rand -hex 32)"
printf '%s\n' "$credential" | sudo tee /etc/veetbot/secrets/browser-profile-service-auth >/dev/null
printf '%s\n' "$credential" | sudo tee /etc/veetbot/secrets/browser-control-plane-credential >/dev/null
unset credential
openssl rand -hex 32 | sudo tee /etc/veetbot/secrets/browser-profile-session-secret >/dev/null

# Keyring: the service does not generate keys; its mount is read-only.
sudo install -d -m 0700 /etc/veetbot/secrets/browser-profile-keys
openssl rand -base64 32 | sudo tee /etc/veetbot/secrets/browser-profile-keys/v1.key >/dev/null
printf 'v1\n' | sudo tee /etc/veetbot/secrets/browser-profile-keys/current >/dev/null

sudo chown 65532:65532 /etc/veetbot/secrets/browser-profile-service-auth \
  /etc/veetbot/secrets/browser-profile-session-secret
sudo chown -R 65532:65532 /etc/veetbot/secrets/browser-profile-keys
sudo chown veetbot:veetbot /etc/veetbot/secrets/browser-control-plane-credential
sudo chmod 0600 /etc/veetbot/secrets/browser-profile-service-auth \
  /etc/veetbot/secrets/browser-profile-session-secret \
  /etc/veetbot/secrets/browser-control-plane-credential \
  /etc/veetbot/secrets/browser-profile-keys/v1.key \
  /etc/veetbot/secrets/browser-profile-keys/current
```

The service's fail-closed loader rejects any secret file with group or other
permission bits, a wrong owner, a symlink, or multi-line content; key files
must be base64 of exactly 32 bytes named `<version>.key`, with `current`
naming an existing version. The environment variables that point at these
paths are in `deploy/veetbot.env.example`; add them to
`/etc/veetbot/veetbot.env` alongside the other values.

The committed Nginx virtual host expects existing Let's Encrypt certificates
at `/etc/letsencrypt/live/api.veetbot.com/` **and**
`/etc/letsencrypt/live/browser.veetbot.com/`; the browser origin needs its DNS
record and certificate before the first Nginx deployment or `nginx -t` fails.
Issue or renew both certificates before deploying. The Nginx installer changes
only Veetbot's `sites-available` and `sites-enabled` entries; it preserves
other virtual hosts.

## CircleCI setup

Generate a Veetbot-only Ed25519 deploy key on a protected operator machine:

```bash
umask 077
ssh-keygen -t ed25519 -N '' -f veetbot-circleci -C veetbot-circleci-production
ssh-keygen -lf veetbot-circleci.pub
```

Install only the public key for the `veetbot` account, using the restricted
`authorized_keys` options described above. Add the private key to the Veetbot
CircleCI project's SSH keys and do not add the Mankunku key or any personal key
to this project. The deployment jobs intentionally load the project's attached
keys, so the dedicated Veetbot key must be the only one present. Store and
rotate it independently from every other application on the Droplet.

Create a restricted CircleCI context named `veetbot-production` with:

| Variable | Value |
| --- | --- |
| `DEPLOY_HOST` | SSH hostname or IP for the Droplet |
| `DEPLOY_USER` | Dedicated deploy account, normally `veetbot` |
| `DEPLOY_KNOWN_HOSTS` | Pinned OpenSSH known-hosts record verified out of band |
| `DEPLOY_PORT` | Optional SSH port; defaults to `22` |
| `PRODUCTION_URL` | Optional public origin; defaults to `https://api.veetbot.com` |

Obtain the public host-key record from the server or provider console and verify
its fingerprint through a second trusted channel before placing it in the
context. Do not treat an unverified `ssh-keyscan` response as identity proof.

The context does not contain the API bearer token, database password, or model
provider keys. Those stay in the protected server environment. Restrict context
use to the repository and protected `main` branch.

## Automatic delivery

An ordinary branch or pull request runs verification only. On `main`, after all
four required verification lanes pass:

- `package-release` archives the exact tested commit and records its SHA-256;
- `deploy-app` stages the archive, verifies the checksum, and executes the
  locked server release; and
- `deploy-nginx` follows a successful application release, takes the same host
  lock, and reconciles the versioned virtual host. If a newer pipeline has
  already promoted another application release, the older proxy job detects the
  release-identity mismatch and exits without overwriting the newer config.

Both deployment jobs use CircleCI's shared production serial group in addition
to the server lock. The release ID is created with the packaged artifact and
reused by failed-job reruns. A rerun of an already active release verifies it
without extracting over the live source directory.

The Nginx installer stores the prior Veetbot configuration under
`/etc/nginx/veetbot-backups`, runs `nginx -t`, and reloads Nginx. Validation or
reload failure restores the prior file. So does a failure while installing the
candidate file or activating its symlink. `deploy/app/release.sh` requires the
local readiness header to identify the staged release and the authenticated
session-index route to respond successfully; after it returns, CircleCI polls
the public readiness endpoint for that exact identity. A public probe failure
is therefore a post-promotion CircleCI failure boundary.

The manual and nightly `live-model` workflows remain tests; they do not deploy.

## Verification

After the first successful pipeline, verify the active revision and services:

```bash
curl --fail --show-error --dump-header - --output /dev/null \
  https://api.veetbot.com/health/ready
ssh veetbot@api.veetbot.com 'readlink -f /opt/veetbot/current'
ssh veetbot@api.veetbot.com \
  'systemctl is-active veetbot-api veetbot-worker veetbot-maintenance'
```

Then make an authenticated API request, submit a run, confirm the worker
completes it with a real provider, and run one generated-code task through
`runsc`. Reboot once and confirm PostgreSQL, Nginx, and all three application
units return.

## Manual rollback

Choose a retained target only after checking that its code is compatible with
the current database schema. Never run an automatic Alembic downgrade as part
of rollback. Run the complete block below in one shell so file descriptor 9
holds the same deployment lock from before the symlink change through readiness
verification.

```bash
set -euo pipefail
exec 9>/opt/veetbot/shared/deploy.lock
if ! flock -w 900 9; then
  echo "Could not acquire /opt/veetbot/shared/deploy.lock" >&2
  exit 1
fi

target_id=YYYYMMDD-HHMMSS-abcdef0
target="/opt/veetbot/releases/$target_id"
test -d "$target"
test -f "$target/.release.env"
ln -s "$target" "/opt/veetbot/.rollback-$target_id"
mv -Tf "/opt/veetbot/.rollback-$target_id" /opt/veetbot/current
docker tag "agent-core-sandbox:$target_id" agent-core-sandbox:production
sudo systemctl restart veetbot-maintenance veetbot-worker veetbot-api
curl --fail --show-error --dump-header - --output /dev/null \
  http://127.0.0.1:8000/health/ready
```

The returned `X-Veetbot-Release` must equal `target_id`, and each unit's
`MainPID` working directory must resolve to `target`. If the older release image
was pruned, rebuild it from that retained source tree before switching.

Nginx backups are independent. To recover one, copy the selected file from
`/etc/nginx/veetbot-backups` to `/etc/nginx/sites-available/veetbot`, run
`sudo nginx -t`, and reload Nginx.

## Accepted limitations

This is still a single-server failure domain with no required cloud firewall,
off-host database, backup, restore rehearsal, monitoring, load balancer, rolling
deployment, or high availability. Loss of the Droplet may mean unrecoverable
data loss. The API binds port 8000 only on loopback, so remote clients must use
the Nginx TLS hostname. A firewall is still recommended to contain any unrelated
service that is accidentally bound to a public interface.
