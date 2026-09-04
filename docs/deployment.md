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

## Public website

The visitor-facing homepage and Google OAuth policy pages are a separate static
site defined under `website/`:

- `https://www.veetbot.com/`
- `https://www.veetbot.com/privacy`
- `https://www.veetbot.com/tos`

The source is a Next.js static export with no account system, analytics,
database, object storage, or Veetbot application credential. Run
`make website-install test-website` locally. CircleCI repeats that build in a
credential-free Node job and passes only the generated `website/out` tree to
release packaging.

The application, documentation, and website artifacts receive the same release
identity. The Nginx deployment validates and atomically promotes the website at
`/opt/veetbot/shared/website/current`; configuration validation or reload
failure restores both static-site pointers and the prior configuration. ADR-0084
records the boundary and rationale.

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

Release pruning normally runs as the deployment identity. Older releases may
contain service-owned Python bytecode from deployments that predate the strict
read-only systemd filesystem policy. If direct removal of such a release fails,
the script retries with the just-built sandbox image as uid 0 in a one-shot,
network-disabled container. The container has a read-only root filesystem, only
the releases directory is mounted writable, and its fixed entrypoint removes
only the validated release name selected by the retention loop. This fallback
uses the deployment identity's existing Docker trust boundary; application
identities still receive no Docker access.

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

!!! warning "Current production host (verified 2026-09-03)"

    The Droplet was not provisioned with the separate `veetbot-deploy`
    identity below. CircleCI logs in as the `veetbot` service account: its key
    is in `/home/veetbot/.ssh/authorized_keys`, `veetbot` owns `/opt/veetbot`
    and `/opt/veetbot/shared/deploy.lock`, and the sudo contract is installed
    for `veetbot`. Because that account is also the `User=` of the API, worker,
    and maintenance units, it carries the deploy identity's Docker-group
    membership into every application process, which is the boundary ADR-0067
    decision 1 draws to keep application services away from the Docker socket.
    Until the host is reconciled, read every `veetbot-deploy` below as
    `veetbot` when operating the current Droplet, and treat "create the
    `veetbot-deploy` account, move the CircleCI key and sudo contract to it,
    and remove `veetbot` from the `docker` group" as the open operations item
    that restores the documented boundary. The account name is not visible to
    CI: `DEPLOY_USER` in the `veetbot-production` context selects it.

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
  /opt/veetbot/shared/docs/releases \
  /opt/veetbot/shared/website/releases \
  /opt/veetbot/shared/uv-cache \
  /etc/veetbot \
  /var/lib/veetbot/artifacts
sudo chown -R veetbot-deploy:veetbot /opt/veetbot
sudo chown -R veetbot:veetbot /var/lib/veetbot
sudo chmod 0700 /var/lib/veetbot/artifacts
```

`veetbot-execution.service` also sets `DynamicUser=yes`. A fully prepared host
uses the static `veetbot-exec` account above; on an older host that does not yet
have it, systemd allocates the same dedicated service identity so the atomic
release can complete without temporarily running the execution service as an
application user.

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

To enable the initial Keenable comparison, add all three provider credentials
and route half of each capability to Keenable in that same root-owned file:

```text
WEB_SEARCH_PROVIDERS=tavily:50,keenable:50
WEB_FETCH_PROVIDERS=firecrawl:50,keenable:50
TAVILY_API_KEY=<production Tavily key>
FIRECRAWL_API_KEY=<production Firecrawl key>
KEENABLE_API_KEY=<production Keenable key>
```

Do not put the real values in `.env.example`, the production template, CircleCI
configuration, a commit, or a PR. The worker reads this environment when the
release restarts its systemd unit. Plural selector entries must be unique
positive integer percentages summing to 100. The legacy singular selectors
remain valid for one-provider deployments, and both capabilities stay disabled
when neither form enables them.

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

Provision the `veetbot_schedule` login independently with a generated password,
then grant only the tables its materialization transaction and schema-head check
use. The release runs `scripts/check_schedule_database_permissions.py` through
the protected schedule environment before promotion and refuses a role that is
administrative, inherits authority, can replicate or bypass row-level security,
can switch to any other role, is missing any privilege below, or has any other
effective table or column-level privilege in the `public` schema. The check is
an exact allowlist, including grants inherited from another role or `PUBLIC`,
rather than a presence-only checklist. Its privilege vocabulary intentionally
matches the deployed PostgreSQL 16 release; PostgreSQL 17's `MAINTAIN`
privilege is not accepted while production remains pinned to version 16. The
checker reads `server_version_num` from the connected server and fails before
the privilege comparison unless that server is PostgreSQL 16.

```sql
ALTER ROLE veetbot_schedule
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
-- Do not grant veetbot_schedule membership in any other role: NOINHERIT does
-- not prevent it from acquiring that role's authority with SET ROLE.
GRANT CONNECT ON DATABASE agent TO veetbot_schedule;
GRANT USAGE ON SCHEMA public TO veetbot_schedule;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM veetbot_schedule;
GRANT SELECT ON
  agents,
  alembic_version,
  checkpoints,
  derived_event_keys,
  events,
  notification_outbox,
  process_events,
  projection_watermarks,
  runs,
  schedule_occurrences,
  schedule_revisions,
  schedules,
  session_history_items,
  sessions
TO veetbot_schedule;
GRANT INSERT ON
  checkpoints,
  derived_event_keys,
  events,
  notification_outbox,
  process_events,
  projection_watermarks,
  runs,
  schedule_occurrences,
  session_history_items,
  sessions
TO veetbot_schedule;
GRANT UPDATE ON
  projection_watermarks,
  runs,
  schedules,
  sessions
TO veetbot_schedule;
GRANT DELETE ON
  checkpoints,
  projection_watermarks,
  session_history_items
TO veetbot_schedule;
```

Run the revocation before reapplying the allowlist whenever this role is
repaired. If the validator still reports a surplus effective privilege, remove
the grant from the role membership, column grant, ownership, `PUBLIC`, or
default-privilege source that supplies it; revoking a direct table grant cannot
mask authority supplied by a different source.

The projection grants are part of checkpoint seeding, not optional reporting:
without them a due schedule can retry until its misfire window expires without
ever committing the session and run.

```bash
sudo chown root:veetbot /etc/veetbot/veetbot-schedule.env
sudo chmod 0640 /etc/veetbot/veetbot-schedule.env
```

Do not add `VEETBOT_RELEASE_ID` to that shared file. The release script writes
it and the fixed host-native execution-service socket to
`/opt/veetbot/current/.release.env`, which every credential-bearing application
unit loads after the shared environment. This release-local wiring lets an
existing host adopt the execution service without rewriting the protected
shared environment before the first compatible release.

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
`/etc/letsencrypt/live/docs.veetbot.com/`. The homepage certificate covering
both `veetbot.com` and `www.veetbot.com` lives under
`/etc/letsencrypt/live/veetbot.com/`. The Nginx installer changes only
Veetbot's `sites-available` and `sites-enabled` entries; it preserves other
virtual hosts.

Before merging a new static-host configuration to `main`:

1. Confirm that `docs.veetbot.com` resolves to the same address as
   `api.veetbot.com`. For the public website, the apex `A` record must target
   that address and `www` must be a CNAME to the apex. Wait for public
   resolution.
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
| `DEPLOY_USER` | Dedicated deploy account, `veetbot-deploy` once provisioned; `veetbot` on the current host |
| `DEPLOY_KNOWN_HOSTS` | Pinned OpenSSH known-hosts record verified out of band |
| `DEPLOY_PORT` | Optional SSH port; defaults to `22` |
| `PRODUCTION_URL` | Optional public origin; defaults to `https://api.veetbot.com` |
| `DOCS_URL` | Optional docs origin; defaults to `https://docs.veetbot.com` |
| `WEBSITE_URL` | Optional website origin; defaults to `https://www.veetbot.com` |

Obtain the public host-key record from the server or provider console and verify
its fingerprint through a second trusted channel before placing it in the
context. Do not treat an unverified `ssh-keyscan` response as identity proof.

The context does not contain the API bearer token, database password, or model
provider keys. Those stay in the protected server environment. Restrict context
use to the repository and protected `main` branch.

### macOS TestFlight delivery

The `apple-testflight` job uses two Apple credential boundaries that are
deliberately separate from `veetbot-production`.

First, upload an active Apple distribution signing identity and the macOS App
Store provisioning profile for `com.veetbot.apple` to CircleCI's code-signing
store, then create a signing bundle named `veetbot-app-store`. The profile must
carry the production entitlements used by the Release target, including APNs.
CircleCI installs that identity and profile into its temporary managed
keychain. Do not add the installer identity to this bundle: CircleCI exposes
neither the keychain password nor the source PKCS#12, so `productbuild` cannot
receive non-interactive access to its private key.

The archive selects `Apple Distribution` for the app signature. For the
installer package, the job discovers the single installed
`3rd Party Mac Developer Installer` certificate and selects its SHA-1
fingerprint explicitly for `productbuild`. Keep those roles separate: the App
Store provisioning profile contains the app distribution certificate, not the
installer certificate.

Create a second restricted context named `veetbot-apple-signing` with:

| Variable | Value |
| --- | --- |
| `APPLE_INSTALLER_CERTIFICATE_BASE64` | Single-line base64 of a password-protected PKCS#12 containing the Mac Installer Distribution private key, leaf certificate, and Apple WWDR G3 intermediate |
| `APPLE_INSTALLER_CERTIFICATE_PASSWORD` | The strong, unique PKCS#12 password |

Restrict this context to the Veetbot project and the expression
`(pipeline.git.branch == "main" or pipeline.git.branch == "dev") and not
job.ssh.enabled and not (pipeline.config_source starts-with "api")`. The job
decodes the PKCS#12 under a mode-restricted temporary directory, imports it into
a fresh random-password keychain, grants only that imported key the
`apple-tool:`, `apple:`, and `codesign:` partitions, passes the exact keychain to
`productbuild`, and deletes both files on every exit. Keep the original private
key in the owner's protected credential store only if certificate recovery is
required; never place the PKCS#12, password, or decoded key in the repository,
a project variable, cache, workspace, artifact, or log.

The `apple-signing-smoke` job proves this complete signing path on trusted
`dev` pushes before a release pull request is opened. It runs the same
repository-owned archive and package script as `apple-testflight`, including
the archived identity checks, application-signature verification,
`productbuild`, and `pkgutil` verification. It receives neither the
`veetbot-apple-testflight` context nor an App Store Connect API key, does not
invoke `altool`, persists no artifact, and therefore cannot publish its package.
It does receive `veetbot-apple-signing`, so access to `dev` and changes to the
CircleCI workflow are nevertheless release-signing security boundaries. Keep
`dev` restricted to trusted maintainers and require the smoke job to pass on the
exact revision proposed for `main`.

Second, create a restricted context named `veetbot-apple-testflight` with:

| Variable | Value |
| --- | --- |
| `APP_STORE_CONNECT_API_KEY_ID` | Ten-character App Store Connect key identifier |
| `APP_STORE_CONNECT_ISSUER_ID` | App Store Connect issuer UUID |
| `APP_STORE_CONNECT_API_KEY_BASE64` | Base64 encoding of the downloaded `.p8` private key |

The non-secret Apple team identifier remains authoritative in the checked-in
Xcode project. The archive uses that Release build setting; do not duplicate
the identifier in the restricted context.

Encode the private key without creating another plaintext copy:

```bash
APP_STORE_CONNECT_API_KEY_ID=REPLACE_WITH_KEY_ID
base64 -i "AuthKey_${APP_STORE_CONNECT_API_KEY_ID}.p8" -o -
```

Paste that output as the context value. Add a CircleCI project restriction for
this repository to the context and restrict it to the protected `main` delivery
path. Treat the managed signing bundle, both Apple contexts, and any CircleCI
role that can reference or replace them as publication authority. The API key
needs only the App Store Connect role and resource access required to upload
Veetbot and manage its signing; it grants no server, model-provider, database,
or Veetbot API authority.
Keep the original `.p8` in the owner's protected credential store because App
Store Connect does not offer it for download a second time.

Before enabling the job, confirm that CircleCI's next `pipeline.number` is
greater than the latest accepted macOS TestFlight build number for the current
marketing version. Then configure the intended TestFlight group for automatic
distribution and enable automatic updates in TestFlight on each Mac. Once CI
owns the build counter, do not make a manual upload with a build number ahead of
that counter.

## Automatic delivery

An ordinary branch or pull request runs verification only. A `dev` push also
runs the non-publishing Apple signing smoke described above. On `main`, after
all six required verification lanes pass:

- `public-site` installs the locked Node dependencies, builds, tests, and lints
  the static export, and exposes only that output to downstream packaging;
- `package-release` archives the exact tested commit, builds MkDocs in strict
  mode, stamps the website output, and records all three artifacts' SHA-256
  values;
- `deploy-app` stages the archive, verifies the checksum, and executes the
  locked server release; and
- `deploy-nginx` follows a successful application release, takes the same host
  lock, atomically promotes `/opt/veetbot/shared/docs/current` and
  `/opt/veetbot/shared/website/current`, and reconciles the versioned virtual
  hosts. If a newer pipeline has already promoted another
  application release, the older proxy job detects the release-identity
  mismatch, reports a distinct stale outcome, and exits without overwriting the
  newer sites or config. CircleCI skips that older job's documentation and
  website identity probes because the newer release remains authoritative; and
- `apple-testflight` follows a successful application release in a separate
  serial group, archives and verifies the macOS application with
  `pipeline.number` as its build number, creates the signed installer package
  directly with `productbuild`, verifies the package signature, and uploads it
  to App Store Connect with `altool` and progress logging. This keeps signing
  and packaging separate from Apple API delivery.

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
requires both `https://docs.veetbot.com/release.txt` and
`https://www.veetbot.com/release.txt` to return the same identity. A public
probe failure is therefore a post-promotion CircleCI failure boundary.
The release verifies every application process through its `/proc` working
directory. The execution service instead relies on its checked-in `ExecStart`,
the completed restart, and an active main PID because `DynamicUser=yes`
deliberately prevents the deploy identity from dereferencing that process's
`/proc` working directory. A post-promotion failure prints bounded systemd
status for each managed unit before reporting the manual rollback target.

The manual and nightly `live-model` workflows remain tests; they do not deploy.
The TestFlight upload job does not claim that Apple's asynchronous processing or
device installation has completed; verify those external states in App Store
Connect after the first automated delivery.

## Verification

After the first successful pipeline, verify the active revision and services.
`DEPLOY_USER` is the account CircleCI logs in as: `veetbot` on the current
host, `veetbot-deploy` once the host is reconciled:

```bash
DEPLOY_USER=veetbot
curl --fail --show-error --dump-header - --output /dev/null \
  https://api.veetbot.com/health/ready
curl --fail --show-error https://docs.veetbot.com/release.txt
curl --fail --show-error https://www.veetbot.com/release.txt
ssh "$DEPLOY_USER@api.veetbot.com" '
  set -eu
  app_release="$(readlink -f /opt/veetbot/current)"
  . "$app_release/.release.env"
  docs_release="$(readlink -f /opt/veetbot/shared/docs/current)"
  website_release="$(readlink -f /opt/veetbot/shared/website/current)"
  test "$(cat "$docs_release/release.txt")" = "$VEETBOT_RELEASE_ID"
  test "$(cat "$website_release/release.txt")" = "$VEETBOT_RELEASE_ID"
  printf "%s\n%s\n" "$docs_release" "$website_release"
'
ssh "$DEPLOY_USER@api.veetbot.com" \
  'systemctl is-active veetbot-execution veetbot-api veetbot-worker veetbot-async-worker veetbot-maintenance'
```

Then make an authenticated API request, submit a run, confirm the worker
completes it with a real provider, and run one generated-code task through
`runsc`. Reboot once and confirm PostgreSQL, Nginx, the execution service, and
all application units return.

## The SMS capture ceremony

Milestone 24 ([device-channel-and-sms.md](plan/device-channel-and-sms.md)) lets
the owner's iPhone act as a Veetbot SMS channel: outbound texts ride the
system compose sheet, and inbound texts reach Veetbot only because the owner
wires a Shortcuts personal automation to the app's "Forward Message to
Veetbot" App Intent. Nothing enables this by default. The owner completes the
following one-time, per-device ceremony after the app is installed and paired
([apple-client.md](apple-client.md)), on the Milestone 18 bootstrap-ceremony
precedent (`plan/email-integration.md`, "Credentials and the bootstrap
ceremony"): a documented, owner-run, one-time setup step with its own
verification, rather than something CI or the server can perform for them.

1. Confirm prerequisites on the owner's iPhone: iOS 17 or later — the
   Shortcuts "When I get a message" personal automation trigger requires it;
   the device already paired to Veetbot and push-verified through
   `POST /v1/devices` and the authenticated
   `POST /v1/devices/{device_id}/test-notification` route; and, in the app's
   connection settings, the "SMS Integration" toggle
   (`sms-integration.enabled`) turned on. That one toggle both opens the
   compose-sheet send path and adds `device.sms.send` to this device's
   registered capabilities — turning it off later removes the capability and
   the App Intent silently stops forwarding.
2. In the Shortcuts app: Automation tab → the add control → Create Personal
   Automation → "When I get a message" → set the automation to run
   immediately, not "Ask Before Running" (it must fire unattended) → add the
   "Forward Message to Veetbot" action → map the trigger's Sender variable to
   the action's Sender parameter and its Content variable to the action's
   Message parameter → finish and confirm the automation is enabled.
3. Verify end to end: from a second phone, send the owner's number a short,
   distinctive test text, then confirm capture actually happened —
   - In Shortcuts, open the automation's run history and confirm it ran
     without a logged failure. This is what catches iOS having silently
     disabled the automation before the message ever reached the app.
   - In Veetbot, confirm the standing SMS triage session shows the forwarded
     message (device-channel-and-sms.md, "SMS ingest," which routes each
     message into one standing session per device and channel): a new
     conversation is seeded, or the existing standing `(device, sms)` session
     continues, with content matching the test text sent rather than an
     empty or truncated body.

   Only a forwarded message with its real sender and body confirms, on this
   owner's specific iOS version, the design's two open questions: that the
   automation actually delivers both sender and body to the App Intent, and
   that the App Intent's Keychain read succeeds with the app backgrounded,
   which an immediately-run automation leaves it. Treat the ceremony as
   incomplete until this step passes.

Capture is deliberately best-effort, exactly as designed: iOS can silently
disable a personal automation with no notice to the owner or to Veetbot, and
the App Intent swallows every forwarding failure — the integration toggled
off, the device unresolved, a network error — into a silent no-op rather than
surfacing an error on the owner's phone. There is no retry and no alert when
a text is silently dropped; the only signal is the absence of the triage
session update step 3 above confirms. Re-run step 3 after an iOS upgrade, or
whenever inbound capture seems to have gone quiet, to confirm it is still
live. The send side has its own fragility: invocation expiry is judged
against the device's own clock, so a phone whose clock is materially fast
can silently expire a still-live request before the owner ever sees it.
And a result the app could not post to the server survives only in that
process's memory, so a crash or force-quit between the owner's Send tap
and a successful result post can re-present an already-sent message the
next time the app fetches, bounded by the server's five-minute invocation
expiry.

## Manual rollback

Schema-head equality is not enough for versioned JSONB discriminators: once any
schedule revision has a `definition.cadence.kind` of `MONTHLY` or `YEARLY`, a
release from before Milestone 20 cannot deserialize the database and is not a
valid code-only rollback target. The runbook below detects the target's cadence
support and checks the stored values while all schedule writers are stopped.
If it refuses the target, roll forward instead, or restore a database snapshot
from before the first such revision and then roll the code back. Never run an
automatic Alembic downgrade as part of rollback. Run the complete block in one
shell so file descriptor 9 holds the same deployment lock from before writer
quiescence through readiness verification.

```bash
set -euo pipefail
environment_file="${VEETBOT_ENV_FILE:-/etc/veetbot/veetbot.env}"
exec 9>/opt/veetbot/shared/deploy.lock
if ! flock -w 900 9; then
  echo "Could not acquire /opt/veetbot/shared/deploy.lock" >&2
  exit 1
fi

target_id=YYYYMMDD-HHMMSS-abcdef0
target="/opt/veetbot/releases/$target_id"
docs_target="/opt/veetbot/shared/docs/releases/$target_id"
website_target="/opt/veetbot/shared/website/releases/$target_id"
test -d "$target"
test -f "$target/.release.env"
test -x "$target/.venv/bin/python"
grep -Fqx "VEETBOT_RELEASE_ID=$target_id" "$target/.release.env"
test -d "$docs_target"
test -f "$docs_target/release.txt"
test "$(cat "$docs_target/release.txt")" = "$target_id"
test -d "$website_target"
test -f "$website_target/release.txt"
test "$(cat "$website_target/release.txt")" = "$target_id"
test -L /opt/veetbot/current
test -L /opt/veetbot/shared/docs/current
test -L /opt/veetbot/shared/website/current
previous_target="$(readlink -f /opt/veetbot/current)"
previous_docs_target="$(readlink -f /opt/veetbot/shared/docs/current)"
previous_website_target="$(readlink -f /opt/veetbot/shared/website/current)"
test -d "$previous_website_target"
test -f "$previous_website_target/release.txt"
test -f "$environment_file"
docker image inspect "agent-core-sandbox:$target_id" >/dev/null
previous_production_image="$(
  docker image inspect --format '{{.Id}}' agent-core-sandbox:production
)"
test -n "$previous_production_image"

managed_units=(
  veetbot-execution
  veetbot-maintenance
  veetbot-worker
  veetbot-async-worker
  veetbot-api
)
for optional_unit in veetbot-schedule veetbot-notify; do
  if sudo systemctl is-enabled --quiet "$optional_unit"; then
    managed_units+=("$optional_unit")
  fi
done

target_calendar_compatibility="$({
  cd "$target"
  "$target/.venv/bin/python" - <<'PY'
from agent_core.domain.schedules import CadenceKind

required = {"MONTHLY", "YEARLY"}
available = {kind.value for kind in CadenceKind}
print("compatible" if required <= available else "incompatible")
PY
})"
case "$target_calendar_compatibility" in
  compatible | incompatible) ;;
  *)
    echo "Could not determine the target's schedule cadence compatibility" >&2
    exit 1
    ;;
esac

app_next="/opt/veetbot/.rollback-$target_id-$$"
docs_next="/opt/veetbot/shared/docs/.rollback-$target_id-$$"
website_next="/opt/veetbot/shared/website/.rollback-$target_id-$$"
app_restore="/opt/veetbot/.rollback-restore-$$"
docs_restore="/opt/veetbot/shared/docs/.rollback-restore-$$"
website_restore="/opt/veetbot/shared/website/.rollback-restore-$$"
health_headers=""

rollback_pending=0
rollback_on_exit() {
  status=$?
  trap - EXIT
  if test "$status" -ne 0 && test "$rollback_pending" -eq 1; then
    set +e
    rm -f -- "$app_restore" "$docs_restore" "$website_restore"
    ln -s "$previous_target" "$app_restore"
    ln -s "$previous_docs_target" "$docs_restore"
    ln -s "$previous_website_target" "$website_restore"
    mv -Tf "$app_restore" /opt/veetbot/current
    mv -Tf "$docs_restore" /opt/veetbot/shared/docs/current
    mv -Tf "$website_restore" /opt/veetbot/shared/website/current
    docker tag "$previous_production_image" agent-core-sandbox:production
    sudo systemctl restart "${managed_units[@]}"
  fi
  if test -n "$health_headers"; then
    rm -f -- "$health_headers"
  fi
  rm -f -- "$app_next" "$docs_next" "$website_next" \
    "$app_restore" "$docs_restore" "$website_restore"
  exit "$status"
}
trap rollback_on_exit EXIT

rollback_pending=1
sudo systemctl stop "${managed_units[@]}"
if test "$target_calendar_compatibility" = incompatible; then
  incompatible_schedule_revisions="$({
    set -a
    # shellcheck disable=SC1090
    . "$environment_file"
    set +a
    : "${POSTGRES_USER:?POSTGRES_USER is required}"
    : "${POSTGRES_DB:?POSTGRES_DB is required}"
    timeout --signal=TERM --kill-after=5s 20s \
      docker compose \
        --env-file "$environment_file" \
        --project-directory "$previous_target" \
        --project-name "${COMPOSE_PROJECT_NAME:-veetbot}" \
        -f "$previous_target/docker-compose.yml" \
        -f "$previous_target/deploy/docker-compose.production.yml" \
        exec -T \
        --env PGCONNECT_TIMEOUT=5 \
        --env 'PGOPTIONS=-c statement_timeout=5000' \
        postgres psql \
        --no-psqlrc --tuples-only --no-align \
        --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
        --command "SELECT count(*) FROM schedule_revisions WHERE definition #>> '{cadence,kind}' IN ('MONTHLY','YEARLY');" \
      | tr -d '[:space:]'
  })"
  case "$incompatible_schedule_revisions" in
    '' | *[!0-9]*)
      echo "Could not validate stored schedule cadence values" >&2
      exit 1
      ;;
  esac
  if test "$incompatible_schedule_revisions" -ne 0; then
    echo "Refusing a pre-Milestone-20 target: incompatible schedule revisions exist" >&2
    exit 1
  fi
fi
ln -s "$target" "$app_next"
ln -s "$docs_target" "$docs_next"
ln -s "$website_target" "$website_next"
docker tag "agent-core-sandbox:$target_id" agent-core-sandbox:production
mv -Tf "$app_next" /opt/veetbot/current
app_next=""
mv -Tf "$docs_next" /opt/veetbot/shared/docs/current
docs_next=""
mv -Tf "$website_next" /opt/veetbot/shared/website/current
website_next=""
sudo systemctl restart "${managed_units[@]}"
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
test "$(cat /opt/veetbot/shared/docs/current/release.txt)" = "$target_id"
test "$(cat /opt/veetbot/shared/website/current/release.txt)" = "$target_id"
for unit in "${managed_units[@]}"; do
  sudo systemctl is-active --quiet "$unit"
  pid="$(sudo systemctl show --property MainPID --value "$unit")"
  test "$pid" -gt 0
  process_cwd="$(readlink -f "/proc/$pid/cwd")"
  test "$process_cwd" = "$target"
done
rollback_pending=0
trap - EXIT
```

The returned `X-Veetbot-Release` and both public surfaces' `release.txt` files
must equal `target_id`, and each unit's
`MainPID` working directory must resolve to `target`. The matching documentation
releases are preconditions, so a missing or mismatched `release.txt` fails before
any symlink changes. Image validation and tagging also precede all three pointer
switches. If either public-surface switch, service restart, or readiness check
fails, the exit trap restores all three previous pointers and the previous production image tag,
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
