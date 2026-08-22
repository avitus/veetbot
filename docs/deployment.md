---
title: Production Deployment
---

# Atomic DigitalOcean deployment

Veetbot deploys to one Ubuntu Droplet at `api.veetbot.com`. PostgreSQL, the API,
the durable worker, the maintenance worker, Docker with gVisor, and Nginx share
that host. CircleCI packages the tested `main` commit and promotes an immutable
release below `/opt/veetbot/releases`. The same pipeline publishes the complete
MkDocs site at `docs.veetbot.com` from a checksummed artifact tied to that
release.

The production deployment scripts require the Ubuntu GNU userland: Bash, GNU
coreutils (including `mv -T` and `sha256sum`), GNU tar and findutils, and
util-linux `flock`. Local macOS deployment-script tests require the matching
Homebrew tools:

```bash
brew install coreutils gnu-tar findutils util-linux
```

The harness maps `gmv`, `gsha256sum`, `gtar`, `gfind`, and the Homebrew
util-linux `flock` into the production command names during both fixture
creation and deployment. Privileged Nginx and systemd operations remain stubbed.

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
   restarts the credential-free execution service and all application units;
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

Create separate deployment, application, and sandbox-execution identities plus
the persistent paths:

```bash
sudo useradd --system --create-home --shell /bin/bash veetbot-deploy
sudo useradd --system --no-create-home --shell /usr/sbin/nologin veetbot
sudo useradd --system --no-create-home --shell /usr/sbin/nologin veetbot-exec
sudo usermod -g veetbot veetbot-exec
sudo usermod -aG veetbot veetbot-deploy
sudo usermod -aG docker veetbot-deploy
sudo usermod -aG docker veetbot-exec
sudo mkdir -p \
  /opt/veetbot/releases \
  /opt/veetbot/docs/releases \
  /opt/veetbot/shared/uv-cache \
  /etc/veetbot \
  /var/lib/veetbot/artifacts
sudo chown -R veetbot-deploy:veetbot /opt/veetbot
sudo chown -R veetbot:veetbot /var/lib/veetbot
sudo chmod 0700 /var/lib/veetbot/artifacts
```

The `veetbot-deploy` account owns `/opt/veetbot`, including the documentation
releases. Nginx and the application group receive read-only traversal through
the ordinary directory modes. The API and worker identities are not members of
the Docker group and their units expose no Docker socket; application services
have no Docker-socket access. Only the credential-free `veetbot-exec` execution
service receives Docker access at application runtime and owns sandbox
lifecycle. The separate deploy identity retains delivery-time Docker access
because the audited release contract builds and tags both images, reconciles the
browser-profile service, and prunes release images. That trusted operator
boundary is the explicit tradeoff in ADR-0067 decision 1; it is never an
application systemd identity. Consequently neither an application process nor
an application-launched container can modify the documentation release. On a
single host, workers reach the service at `/run/veetbot/execution.sock`. On a
multi-host deployment, move the service to the dedicated sandbox host described
by ADR-0008 and replace `/run/veetbot/execution.sock` with an authenticated
network transport while preserving the `ExecutionEnvironment` port.

The deploy key must log in as `veetbot-deploy`, so that account needs an executable
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

# One token, two source files: the container refuses secrets it does not own
# (uid 65532), while systemd copies the deploy-owned control credential into
# each application unit's private credential directory.
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
sudo chown veetbot-deploy:veetbot-deploy \
  /etc/veetbot/secrets/browser-control-plane-credential
sudo chmod 0600 /etc/veetbot/secrets/browser-profile-service-auth \
  /etc/veetbot/secrets/browser-profile-session-secret \
  /etc/veetbot/secrets/browser-control-plane-credential \
  /etc/veetbot/secrets/browser-profile-keys/v1.key \
  /etc/veetbot/secrets/browser-profile-keys/current
```

The service's fail-closed loader rejects any secret file with group or other
permission bits, a wrong owner, a symlink, or multi-line content; key files
must be base64 of exactly 32 bytes named `<version>.key`, with `current`
naming an existing version. The profile-service mount paths are in
`deploy/veetbot.env.example`. The application control credential is deliberately
absent from that shared environment: the checked-in application units use
systemd `LoadCredential`, while the deployment preflight reads the protected
source as `veetbot-deploy`.

The committed Nginx virtual hosts expect Let's Encrypt certificates at
`/etc/letsencrypt/live/api.veetbot.com/`,
`/etc/letsencrypt/live/browser.veetbot.com/`, and
`/etc/letsencrypt/live/docs.veetbot.com/`. The Nginx installer changes only
Veetbot's `sites-available` and `sites-enabled` entries; it preserves other
virtual hosts.

Before merging the documentation-hosting change to `main`:

1. Add a DigitalOcean DNS `A` record for `docs.veetbot.com` with the same target
   as `api.veetbot.com`, and wait for public resolution.
2. Provision the dedicated certificate at the exact path above. Prefer
   Certbot's DigitalOcean DNS plugin so issuance and renewal do not depend on a
   temporary HTTP virtual host. Keep its API token in a root-owned `0600`
   credentials file outside the repository and CircleCI.
3. Run `sudo nginx -t` without changing the existing production site.

The deployment deliberately does not bootstrap around a missing certificate:
strict Nginx validation must fail rather than publish a plaintext or
misidentified documentation endpoint.

## CircleCI setup

Generate a Veetbot-only Ed25519 deploy key on a protected operator machine:

```bash
umask 077
ssh-keygen -t ed25519 -N '' -f veetbot-circleci -C veetbot-circleci-production
ssh-keygen -lf veetbot-circleci.pub
```

Install only the public key for the `veetbot-deploy` account, using the restricted
`authorized_keys` options described above. Add the private key to the Veetbot
CircleCI project's SSH keys and do not add the Mankunku key or any personal key
to this project. The deployment jobs intentionally load the project's attached
keys, so the dedicated Veetbot key must be the only one present. Store and
rotate it independently from every other application on the Droplet.

Create a restricted CircleCI context named `veetbot-production` with:

| Variable | Value |
| --- | --- |
| `DEPLOY_HOST` | SSH hostname or IP for the Droplet |
| `DEPLOY_USER` | Dedicated deploy account, normally `veetbot-deploy` |
| `DEPLOY_KNOWN_HOSTS` | Pinned OpenSSH known-hosts record verified out of band |
| `DEPLOY_PORT` | Optional SSH port; defaults to `22` |
| `PRODUCTION_URL` | Optional public origin; defaults to `https://api.veetbot.com` |
| `DOCS_URL` | Optional docs origin; defaults to `https://docs.veetbot.com` |

Obtain the public host-key record from the server or provider console and verify
its fingerprint through a second trusted channel before placing it in the
context. Do not treat an unverified `ssh-keyscan` response as identity proof.

The context does not contain the API bearer token, database password, or model
provider keys. Those stay in the protected server environment. Restrict context
use to the repository and protected `main` branch.

## Automatic delivery

An ordinary branch or pull request runs verification only. On `main`, after all
four required verification lanes pass:

- `package-release` archives the exact tested commit, builds MkDocs in strict
  mode, and records both artifacts' SHA-256 values;
- `deploy-app` stages the archive, verifies the checksum, and executes the
  locked server release; and
- `deploy-nginx` follows a successful application release, takes the same host
  lock, atomically promotes `/opt/veetbot/docs/current`, and reconciles the
  versioned virtual hosts. If a newer pipeline has already promoted another
  application release, the older proxy job detects the release-identity
  mismatch, reports a distinct stale outcome, and exits without overwriting the
  newer site or config. CircleCI skips that older job's documentation identity
  probe because the newer release remains authoritative.

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
the public readiness endpoint for that exact identity. The Nginx job then
requires `https://docs.veetbot.com/release.txt` to return the same identity. A
public probe failure is therefore a post-promotion CircleCI failure boundary.

The manual and nightly `live-model` workflows remain tests; they do not deploy.

## Verification

After the first successful pipeline, verify the active revision and services:

```bash
curl --fail --show-error --dump-header - --output /dev/null \
  https://api.veetbot.com/health/ready
curl --fail --show-error https://docs.veetbot.com/release.txt
ssh veetbot-deploy@api.veetbot.com '
  set -eu
  app_release="$(readlink -f /opt/veetbot/current)"
  . "$app_release/.release.env"
  docs_release="$(readlink -f /opt/veetbot/docs/current)"
  test "$(cat "$docs_release/release.txt")" = "$VEETBOT_RELEASE_ID"
  printf "%s\n" "$docs_release"
'
ssh veetbot-deploy@api.veetbot.com \
  'systemctl is-active veetbot-execution veetbot-api veetbot-worker veetbot-async-worker veetbot-maintenance'
```

Then make an authenticated API request, submit a run, confirm the worker
completes it with a real provider, and run one generated-code task through
`runsc`. Reboot once and confirm PostgreSQL, Nginx, the execution service, and
all application units return.

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
docs_target="/opt/veetbot/docs/releases/$target_id"
test -d "$target"
test -f "$target/.release.env"
grep -Fqx "VEETBOT_RELEASE_ID=$target_id" "$target/.release.env"
test -d "$docs_target"
test -f "$docs_target/release.txt"
test "$(cat "$docs_target/release.txt")" = "$target_id"
test -L /opt/veetbot/current
test -L /opt/veetbot/docs/current
previous_target="$(readlink -f /opt/veetbot/current)"
previous_docs_target="$(readlink -f /opt/veetbot/docs/current)"
docker image inspect "agent-core-sandbox:$target_id" >/dev/null
previous_production_image="$(
  docker image inspect --format '{{.Id}}' agent-core-sandbox:production
)"
test -n "$previous_production_image"

app_next="/opt/veetbot/.rollback-$target_id-$$"
docs_next="/opt/veetbot/docs/.rollback-$target_id-$$"
app_restore="/opt/veetbot/.rollback-restore-$$"
docs_restore="/opt/veetbot/docs/.rollback-restore-$$"
health_headers=""
ln -s "$target" "$app_next"
ln -s "$docs_target" "$docs_next"

rollback_pending=0
rollback_on_exit() {
  status=$?
  trap - EXIT
  if test "$status" -ne 0 && test "$rollback_pending" -eq 1; then
    set +e
    rm -f -- "$app_restore" "$docs_restore"
    ln -s "$previous_target" "$app_restore"
    ln -s "$previous_docs_target" "$docs_restore"
    mv -Tf "$app_restore" /opt/veetbot/current
    mv -Tf "$docs_restore" /opt/veetbot/docs/current
    docker tag "$previous_production_image" agent-core-sandbox:production
    sudo systemctl restart \
      veetbot-execution veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api
  fi
  if test -n "$health_headers"; then
    rm -f -- "$health_headers"
  fi
  rm -f -- "$app_next" "$docs_next" "$app_restore" "$docs_restore"
  exit "$status"
}
trap rollback_on_exit EXIT

rollback_pending=1
docker tag "agent-core-sandbox:$target_id" agent-core-sandbox:production
mv -Tf "$app_next" /opt/veetbot/current
app_next=""
mv -Tf "$docs_next" /opt/veetbot/docs/current
docs_next=""
sudo systemctl restart \
  veetbot-execution veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api
health_headers="$(mktemp /opt/veetbot/shared/rollback-health.XXXXXX)"
curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  --dump-header "$health_headers" --output /dev/null \
  http://127.0.0.1:8000/health/ready
awk -F ': *' -v expected="$target_id" '
  tolower($1) == "x-veetbot-release" {
    sub(/\r$/, "", $2)
    if ($2 == expected) found = 1
  }
  END { exit found ? 0 : 1 }
' "$health_headers"
rm -f -- "$health_headers"
health_headers=""
test "$(cat /opt/veetbot/docs/current/release.txt)" = "$target_id"
for unit in \
  veetbot-execution \
  veetbot-maintenance \
  veetbot-worker \
  veetbot-async-worker \
  veetbot-api; do
  sudo systemctl is-active --quiet "$unit"
  pid="$(sudo systemctl show --property MainPID --value "$unit")"
  test "$pid" -gt 0
  process_cwd="$(readlink -f "/proc/$pid/cwd")"
  test "$process_cwd" = "$target"
done
rollback_pending=0
trap - EXIT
```

The returned `X-Veetbot-Release` must equal `target_id`, and each unit's
`MainPID` working directory must resolve to `target`. The matching documentation
release is a precondition, so a missing or mismatched `release.txt` fails before
either symlink changes. Image validation and tagging also precede both pointer
switches. If the second switch, service restart, or readiness check fails, the
exit trap restores both previous pointers and the previous production image tag,
then restarts the previous release; `deploy/app/rollback.test.sh` failure-injects
the second switch to verify that recovery. If the older release image was
pruned, rebuild it from that retained source tree before switching.

Nginx backups are independent. To recover one, copy the selected file from
`/etc/nginx/veetbot-backups` to `/etc/nginx/sites-available/veetbot`, run
`sudo nginx -t`, and reload Nginx.

## Accepted limitations

Milestone 15 ([operational-hardening.md](plan/operational-hardening.md),
ADR-0065) specifies the backup, restore rehearsal, alerting, firewall,
rollback, and watchdog controls that shrink this list to the residual single
failure domain; until that milestone lands, the limitations below stand.

This is still a single-server failure domain with no required cloud firewall,
off-host database, backup, restore rehearsal, monitoring, load balancer, rolling
deployment, or high availability. Loss of the Droplet may mean unrecoverable
data loss. The API binds port 8000 only on loopback, so remote clients must use
the Nginx TLS hostname. A firewall is still recommended to contain any unrelated
service that is accidentally bound to a public interface.
