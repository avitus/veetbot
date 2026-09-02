#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

RELEASE_ID="${1:-}"
DEPLOY_ROOT="${VEETBOT_ROOT:-/opt/veetbot}"
ENV_FILE="${VEETBOT_ENV_FILE:-/etc/veetbot/veetbot.env}"
SCHEDULE_ENV_FILE="${VEETBOT_SCHEDULE_ENV_FILE:-/etc/veetbot/veetbot-schedule.env}"
NOTIFY_ENV_FILE="${VEETBOT_NOTIFY_ENV_FILE:-/etc/veetbot/veetbot-notify.env}"
BROWSER_CONTROL_CREDENTIAL_FILE="${VEETBOT_BROWSER_CONTROL_PLANE_CREDENTIAL_FILE:-/etc/veetbot/secrets/browser-control-plane-credential}"
SYSTEMD_DIR="${VEETBOT_SYSTEMD_DIR:-/etc/systemd/system}"
PROCESS_ROOT="${VEETBOT_PROCESS_ROOT:-/proc}"
KEEP_RELEASES="${VEETBOT_KEEP_RELEASES:-5}"
LOCK_WAIT_SECS="${VEETBOT_DEPLOY_LOCK_WAIT_SECS:-900}"
HEALTH_URL="${VEETBOT_HEALTH_URL:-http://127.0.0.1:8000/health/ready}"
API_BASE_URL="${VEETBOT_API_BASE_URL:-http://127.0.0.1:8000}"
HEALTH_TIMEOUT_SECS="${VEETBOT_HEALTH_TIMEOUT_SECS:-60}"
RELEASE_PATTERN='^[0-9]{8}-[0-9]{6}-[0-9a-f]{7,40}$'
EXECUTION_SERVICE_SOCKET=/run/veetbot/execution.sock
UNITS=(veetbot-execution veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api)

fail() {
  printf 'release failed: %s\n' "$*" >&2
  exit 1
}

environment_flag() {
  local file="$1"
  local name="$2"
  awk -v key="$name" '
    index($0, key "=") == 1 {
      value = substr($0, length(key) + 2)
      found = 1
    }
    END { print found ? value : "0" }
  ' "$file"
}

[[ "$RELEASE_ID" =~ $RELEASE_PATTERN ]] || fail \
  "release id must be YYYYMMDD-HHMMSS plus a 7-40 character lowercase hex revision"
[[ "$DEPLOY_ROOT" = /* && "$DEPLOY_ROOT" != / ]] || fail \
  "VEETBOT_ROOT must be a non-root absolute path"
[[ "$KEEP_RELEASES" =~ ^[1-9][0-9]*$ ]] || fail \
  "VEETBOT_KEEP_RELEASES must be a positive integer"
[[ "$LOCK_WAIT_SECS" =~ ^[1-9][0-9]*$ ]] || fail \
  "VEETBOT_DEPLOY_LOCK_WAIT_SECS must be a positive integer"
[[ "$HEALTH_TIMEOUT_SECS" =~ ^[1-9][0-9]*$ ]] || fail \
  "VEETBOT_HEALTH_TIMEOUT_SECS must be a positive integer"
[[ "$API_BASE_URL" =~ ^https?://[^/?#[:space:]]+(/[^?#[:space:]]*)?$ ]] || fail \
  "VEETBOT_API_BASE_URL must be an HTTP(S) URL without a query or fragment"
while [[ "$API_BASE_URL" == */ ]]; do
  API_BASE_URL="${API_BASE_URL%/}"
done

RELEASES_DIR="$DEPLOY_ROOT/releases"
SHARED_DIR="$DEPLOY_ROOT/shared"
STAGE="$RELEASES_DIR/$RELEASE_ID"
CURRENT="$DEPLOY_ROOT/current"
RELEASE_IMAGE="agent-core-sandbox:$RELEASE_ID"
PRODUCTION_IMAGE="agent-core-sandbox:production"
PROFILE_RELEASE_IMAGE="veetbot-browser-profile-service:$RELEASE_ID"
PREVIOUS_RELEASE=""
PROMOTED=0
HEALTH_HEADERS=""

mkdir -p "$RELEASES_DIR" "$SHARED_DIR/uv-cache"
[[ -d "$STAGE" ]] || fail "staged release does not exist: $STAGE"
[[ -f "$ENV_FILE" ]] || fail "production environment does not exist: $ENV_FILE"

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$HEALTH_HEADERS" ]]; then
    rm -f -- "$HEALTH_HEADERS"
  fi
  if (( status != 0 && PROMOTED == 0 )) && [[ -d "$STAGE" ]]; then
    local active=""
    active="$(readlink -f "$CURRENT" 2>/dev/null || true)"
    if [[ "$active" != "$STAGE" && "$(basename "$STAGE")" =~ $RELEASE_PATTERN ]]; then
      rm -rf -- "$STAGE"
    fi
  fi
  if (( status != 0 && PROMOTED == 1 )); then
    printf 'post-promotion unit status follows:\n' >&2
    for unit in "${UNITS[@]}"; do
      systemctl --no-pager --full status "$unit" >&2 || true
    done
  fi
  if (( status != 0 && PROMOTED == 1 )) && [[ -n "$PREVIOUS_RELEASE" ]]; then
    printf 'release failed after promotion; current still points at %s\n' "$STAGE" >&2
    printf 'release was promoted but did not verify; manual rollback target: %s\n' \
      "$PREVIOUS_RELEASE" >&2
  elif (( status != 0 && PROMOTED == 1 )); then
    printf 'release failed after promotion; current still points at %s\n' "$STAGE" >&2
    printf 'no previous release is available for rollback\n' >&2
  fi
  exit "$status"
}
trap cleanup EXIT

exec 9>"$SHARED_DIR/deploy.lock"
printf 'Waiting up to %ss for the Veetbot deployment lock...\n' "$LOCK_WAIT_SECS"
flock -w "$LOCK_WAIT_SECS" 9 || fail "timed out waiting for the deployment lock"

ACTIVE_RELEASE="$(readlink -f "$CURRENT" 2>/dev/null || true)"
ACTIVE_RELEASE_ID="$(basename "$ACTIVE_RELEASE" 2>/dev/null || true)"
RELEASE_TIMESTAMP="${RELEASE_ID:0:15}"
ACTIVE_RELEASE_TIMESTAMP="${ACTIVE_RELEASE_ID:0:15}"
if [[ "$ACTIVE_RELEASE_ID" =~ $RELEASE_PATTERN \
  && "$RELEASE_TIMESTAMP" < "$ACTIVE_RELEASE_TIMESTAMP" ]]; then
  fail "refusing stale release $RELEASE_ID because $ACTIVE_RELEASE_ID is already active"
fi
if [[ "$RELEASE_ID" == "$ACTIVE_RELEASE_ID" ]]; then
  fail "refusing to modify the active release in place: $RELEASE_ID"
fi

for required in \
  pyproject.toml \
  uv.lock \
  alembic.ini \
  docker-compose.yml \
  deploy/docker-compose.production.yml \
  deploy/browser-profile-service.Dockerfile \
  deploy/veetbot-schedule.env.example \
  deploy/veetbot-notify.env.example \
  deploy/systemd/veetbot-api.service \
  deploy/systemd/veetbot-worker.service \
  deploy/systemd/veetbot-async-worker.service \
  deploy/systemd/veetbot-execution.service \
  deploy/systemd/veetbot-maintenance.service \
  deploy/systemd/veetbot-schedule.service \
  deploy/systemd/veetbot-notify.service \
  execution/sandbox.Dockerfile \
  scripts/check_schedule_database_permissions.py \
  scripts/check_production_deployment.py; do
  [[ -f "$STAGE/$required" ]] || fail "staged release is missing $required"
done

if [[ -L "$CURRENT" ]]; then
  PREVIOUS_RELEASE="$ACTIVE_RELEASE"
fi

umask 027
printf 'VEETBOT_RELEASE_ID=%s\nAGENT_EXECUTION_SERVICE_SOCKET=%s\n' \
  "$RELEASE_ID" "$EXECUTION_SERVICE_SOCKET" >"$STAGE/.release.env"

cd "$STAGE"
export UV_CACHE_DIR="$SHARED_DIR/uv-cache"
uv sync --frozen --no-dev
docker build -f execution/sandbox.Dockerfile -t "$RELEASE_IMAGE" .
docker build -f deploy/browser-profile-service.Dockerfile -t "$PROFILE_RELEASE_IMAGE" .

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export AGENT_EXECUTION_SERVICE_SOCKET="$EXECUTION_SERVICE_SOCKET"
export BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE="$BROWSER_CONTROL_CREDENTIAL_FILE"
[[ -n "${AUTH_TOKEN:-}" ]] || fail "AUTH_TOKEN is required for the API contract probe"
[[ "${BROWSER_PROFILE_SERVICE_AUTH_FILE:-}" = /* ]] || fail \
  "BROWSER_PROFILE_SERVICE_AUTH_FILE must be an absolute path"
[[ -f "$BROWSER_PROFILE_SERVICE_AUTH_FILE" ]] || fail \
  "BROWSER_PROFILE_SERVICE_AUTH_FILE must name an existing regular file"
[[ ! -L "$BROWSER_PROFILE_SERVICE_AUTH_FILE" ]] || fail \
  "BROWSER_PROFILE_SERVICE_AUTH_FILE must not be a symlink"
[[ "${BROWSER_PROFILE_SESSION_SECRET_FILE:-}" = /* ]] || fail \
  "BROWSER_PROFILE_SESSION_SECRET_FILE must be an absolute path"
[[ -f "$BROWSER_PROFILE_SESSION_SECRET_FILE" ]] || fail \
  "BROWSER_PROFILE_SESSION_SECRET_FILE must name an existing regular file"
[[ ! -L "$BROWSER_PROFILE_SESSION_SECRET_FILE" ]] || fail \
  "BROWSER_PROFILE_SESSION_SECRET_FILE must not be a symlink"
[[ "$BROWSER_CONTROL_CREDENTIAL_FILE" = /* ]] || fail \
  "VEETBOT_BROWSER_CONTROL_PLANE_CREDENTIAL_FILE must be an absolute path: $BROWSER_CONTROL_CREDENTIAL_FILE"
[[ -f "$BROWSER_CONTROL_CREDENTIAL_FILE" ]] || fail \
  "VEETBOT_BROWSER_CONTROL_PLANE_CREDENTIAL_FILE must name an existing regular file: $BROWSER_CONTROL_CREDENTIAL_FILE"
[[ ! -L "$BROWSER_CONTROL_CREDENTIAL_FILE" ]] || fail \
  "VEETBOT_BROWSER_CONTROL_PLANE_CREDENTIAL_FILE must not be a symlink: $BROWSER_CONTROL_CREDENTIAL_FILE"
[[ "${BROWSER_PROFILE_CEREMONY_BASE_URL:-}" =~ ^https://[^/?#[:space:]]+/?$ ]] || fail \
  "BROWSER_PROFILE_CEREMONY_BASE_URL must be one HTTPS origin"
[[ "${BROWSER_PROFILE_KEY_DIR:-}" = /* ]] || fail \
  "BROWSER_PROFILE_KEY_DIR must be an absolute path"
[[ -d "$BROWSER_PROFILE_KEY_DIR" ]] || fail \
  "BROWSER_PROFILE_KEY_DIR must name an existing directory"
[[ ! -L "$BROWSER_PROFILE_KEY_DIR" ]] || fail \
  "BROWSER_PROFILE_KEY_DIR must not be a symlink"
[[ "${AGENT_SCHEDULE_API_ENABLED:-0}" =~ ^[01]$ ]] || fail \
  "AGENT_SCHEDULE_API_ENABLED must be 0 or 1"
[[ "${AGENT_SCHEDULE_WORKER_ENABLED:-0}" =~ ^[01]$ ]] || fail \
  "AGENT_SCHEDULE_WORKER_ENABLED must be 0 or 1"
[[ "${AGENT_SCHEDULE_API_ENABLED:-0}" == "${AGENT_SCHEDULE_WORKER_ENABLED:-0}" ]] || fail \
  "schedule API and worker flags must be enabled or disabled together"
if [[ "${AGENT_SCHEDULE_WORKER_ENABLED:-0}" == "1" ]]; then
  [[ "$SCHEDULE_ENV_FILE" =~ ^/[^[:space:]]+$ ]] || fail \
    "VEETBOT_SCHEDULE_ENV_FILE must be an absolute path without whitespace"
  [[ -f "$SCHEDULE_ENV_FILE" ]] || fail \
    "schedule worker environment does not exist: $SCHEDULE_ENV_FILE"
  [[ ! -L "$SCHEDULE_ENV_FILE" ]] || fail \
    "schedule worker environment must not be a symlink"
  UNITS=(veetbot-schedule "${UNITS[@]}")
fi
[[ "${AGENT_NOTIFICATION_API_ENABLED:-0}" =~ ^[01]$ ]] || fail \
  "AGENT_NOTIFICATION_API_ENABLED must be 0 or 1"
[[ "${AGENT_NOTIFICATION_DISPATCH_ENABLED:-0}" =~ ^[01]$ ]] || fail \
  "AGENT_NOTIFICATION_DISPATCH_ENABLED must be 0 or 1"
[[ "${AGENT_NOTIFICATION_API_ENABLED:-0}" == \
  "${AGENT_NOTIFICATION_DISPATCH_ENABLED:-0}" ]] || fail \
  "notification API and dispatch flags must be enabled or disabled together"
if [[ "${AGENT_SCHEDULE_WORKER_ENABLED:-0}" == "1" ]]; then
  SCHEDULE_NOTIFICATION_API_FLAG="$(
    environment_flag "$SCHEDULE_ENV_FILE" AGENT_NOTIFICATION_API_ENABLED
  )"
  SCHEDULE_NOTIFICATION_DISPATCH_FLAG="$(
    environment_flag "$SCHEDULE_ENV_FILE" AGENT_NOTIFICATION_DISPATCH_ENABLED
  )"
  [[ "$SCHEDULE_NOTIFICATION_API_FLAG" =~ ^[01]$ ]] || fail \
    "schedule worker AGENT_NOTIFICATION_API_ENABLED must be 0 or 1"
  [[ "$SCHEDULE_NOTIFICATION_DISPATCH_FLAG" =~ ^[01]$ ]] || fail \
    "schedule worker AGENT_NOTIFICATION_DISPATCH_ENABLED must be 0 or 1"
  [[ "$SCHEDULE_NOTIFICATION_API_FLAG" == \
    "$SCHEDULE_NOTIFICATION_DISPATCH_FLAG" ]] || fail \
    "schedule worker notification API and dispatch flags must change together"
  [[ "$SCHEDULE_NOTIFICATION_API_FLAG" == \
    "${AGENT_NOTIFICATION_API_ENABLED:-0}" ]] || fail \
    "schedule worker notification flags must match the application notification flags"
fi
if [[ "${AGENT_NOTIFICATION_DISPATCH_ENABLED:-0}" == "1" ]]; then
  [[ "$NOTIFY_ENV_FILE" =~ ^/[^[:space:]]+$ ]] || fail \
    "VEETBOT_NOTIFY_ENV_FILE must be an absolute path without whitespace"
  [[ -f "$NOTIFY_ENV_FILE" ]] || fail \
    "notification worker environment does not exist: $NOTIFY_ENV_FILE"
  [[ ! -L "$NOTIFY_ENV_FILE" ]] || fail \
    "notification worker environment must not be a symlink"
  UNITS=(veetbot-notify "${UNITS[@]}")
fi
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-veetbot}"
export BROWSER_PROFILE_SERVICE_IMAGE="$PROFILE_RELEASE_IMAGE"
docker compose --env-file "$ENV_FILE" \
  --project-name "$COMPOSE_PROJECT_NAME" \
  -f docker-compose.yml -f deploy/docker-compose.production.yml \
  up -d --wait --wait-timeout "$HEALTH_TIMEOUT_SECS" postgres browser-profile-service

export VEETBOT_RELEASE_ID="$RELEASE_ID"
export AGENT_SANDBOX_IMAGE="$RELEASE_IMAGE"
"$STAGE/.venv/bin/alembic" upgrade head
"$STAGE/.venv/bin/python" scripts/check_production_deployment.py
if [[ "${AGENT_SCHEDULE_WORKER_ENABLED:-0}" == "1" ]]; then
  if ! (
    set -a
    # shellcheck disable=SC1090
    . "$SCHEDULE_ENV_FILE"
    set +a
    "$STAGE/.venv/bin/python" scripts/check_schedule_database_permissions.py
  ); then
    fail "schedule database role does not satisfy the materialization contract"
  fi
fi

sudo install -d -m 0755 "$SYSTEMD_DIR"
sudo install -m 0644 "$STAGE/deploy/systemd/"*.service "$SYSTEMD_DIR/"
if [[ "${AGENT_SCHEDULE_WORKER_ENABLED:-0}" == "1" ]]; then
  awk -v environment_file="$SCHEDULE_ENV_FILE" '
    /^EnvironmentFile=/ { print "EnvironmentFile=" environment_file; next }
    { print }
  ' "$STAGE/deploy/systemd/veetbot-schedule.service" \
    >"$STAGE/.veetbot-schedule.service"
  sudo install -m 0644 "$STAGE/.veetbot-schedule.service" \
    "$SYSTEMD_DIR/veetbot-schedule.service"
fi
if [[ "${AGENT_NOTIFICATION_DISPATCH_ENABLED:-0}" == "1" ]]; then
  awk -v environment_file="$NOTIFY_ENV_FILE" '
    /^EnvironmentFile=/ { print "EnvironmentFile=" environment_file; next }
    { print }
  ' "$STAGE/deploy/systemd/veetbot-notify.service" \
    >"$STAGE/.veetbot-notify.service"
  sudo install -m 0644 "$STAGE/.veetbot-notify.service" \
    "$SYSTEMD_DIR/veetbot-notify.service"
fi
sudo systemctl daemon-reload
if [[ "${AGENT_SCHEDULE_WORKER_ENABLED:-0}" == "0" ]]; then
  sudo systemctl disable --now veetbot-schedule >/dev/null 2>&1 || true
fi
if [[ "${AGENT_NOTIFICATION_DISPATCH_ENABLED:-0}" == "0" ]]; then
  sudo systemctl disable --now veetbot-notify >/dev/null 2>&1 || true
fi

NEXT_CURRENT="$DEPLOY_ROOT/.current-$RELEASE_ID"
rm -f -- "$NEXT_CURRENT"
ln -s "$STAGE" "$NEXT_CURRENT"
mv -Tf "$NEXT_CURRENT" "$CURRENT"
PROMOTED=1
docker tag "$RELEASE_IMAGE" "$PRODUCTION_IMAGE"
sudo systemctl enable --now "${UNITS[@]}"
sudo systemctl restart "${UNITS[@]}"

HEALTH_HEADERS="$(mktemp "$SHARED_DIR/health.XXXXXX")"
healthy=0
for ((attempt = 1; attempt <= HEALTH_TIMEOUT_SECS; attempt++)); do
  : >"$HEALTH_HEADERS"
  if curl --fail --silent --show-error \
    --connect-timeout 2 --max-time 5 \
    --dump-header "$HEALTH_HEADERS" --output /dev/null \
    "$HEALTH_URL"; then
    if awk -F ': *' -v expected="$RELEASE_ID" '
      tolower($1) == "x-veetbot-release" {
        sub(/\r$/, "", $2)
        if ($2 == expected) found = 1
      }
      END { exit found ? 0 : 1 }
    ' "$HEALTH_HEADERS"; then
      healthy=1
      break
    fi
  fi
  sleep 1
done
rm -f -- "$HEALTH_HEADERS"
HEALTH_HEADERS=""
(( healthy == 1 )) || fail "the local readiness probe did not report $RELEASE_ID"

if ! session_index_status="$(
  printf 'Authorization: %s %s\n' Bearer "$AUTH_TOKEN" \
    | curl --silent --show-error \
      --connect-timeout 2 --max-time 5 --header @- --output /dev/null \
      --write-out '%{http_code}' "$API_BASE_URL/v1/sessions?limit=1"
)"; then
  fail "the promoted API does not expose the authoritative session index"
fi
[[ "$session_index_status" =~ ^2[0-9][0-9]$ ]] || fail \
  "the promoted API session index returned HTTP $session_index_status"

for unit in "${UNITS[@]}"; do
  sudo systemctl is-active --quiet "$unit" || fail "$unit is not active"
  pid="$(sudo systemctl show --property MainPID --value "$unit")"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || fail "$unit has no main process"
  if [[ "$unit" == veetbot-execution ]]; then
    # DynamicUser deliberately prevents the deploy identity from dereferencing
    # this process's /proc cwd. The checked-in ExecStart uses current, and the
    # successful restart plus active MainPID proves that systemd re-executed it.
    continue
  fi
  process_cwd="$(readlink -f "$PROCESS_ROOT/$pid/cwd" 2>/dev/null || true)"
  [[ "$process_cwd" == "$STAGE" ]] || fail "$unit is not running from $STAGE"
done

kept=0
while IFS= read -r candidate; do
  [[ "$candidate" =~ $RELEASE_PATTERN ]] || continue
  candidate_path="$RELEASES_DIR/$candidate"
  [[ -d "$candidate_path" ]] || continue
  kept=$((kept + 1))
  if (( kept > KEEP_RELEASES )) && [[ "$candidate_path" != "$STAGE" ]]; then
    if ! rm -rf -- "$candidate_path"; then
      printf 'Direct pruning of %s failed; retrying with the trusted deployment container.\n' \
        "$candidate" >&2
      docker run --rm --pull=never --network none --read-only --user 0:0 \
        --volume "$RELEASES_DIR:/releases" \
        --entrypoint /bin/rm "$RELEASE_IMAGE" \
        -rf -- "/releases/$candidate"
      [[ ! -e "$candidate_path" && ! -L "$candidate_path" ]] || fail \
        "the trusted deployment container could not prune $candidate_path"
    fi
    docker image rm "agent-core-sandbox:$candidate" >/dev/null 2>&1 || true
  fi
done < <(
  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort -r
)

trap - EXIT
printf 'Released %s successfully.\n' "$RELEASE_ID"
if [[ -n "$PREVIOUS_RELEASE" && "$PREVIOUS_RELEASE" != "$STAGE" ]]; then
  printf 'Manual rollback target: %s\n' "$PREVIOUS_RELEASE"
  printf 'Rollback requires repointing current, retagging that release image, and restarting all enabled units.\n'
fi
