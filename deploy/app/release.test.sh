#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_SCRIPT="$SCRIPT_DIR/release.sh"
TEST_ROOT="$(mktemp -d)"
# Permission fixtures below close directories, so reopen them before removal.
trap 'chmod -R u+rwX "$TEST_ROOT" 2>/dev/null || true; rm -rf -- "$TEST_ROOT"' EXIT

BIN_DIR="$TEST_ROOT/bin"
DEPLOY_ROOT="$TEST_ROOT/opt/veetbot"
SYSTEMD_DIR="$TEST_ROOT/systemd"
PROCESS_ROOT="$TEST_ROOT/proc"
ENV_FILE="$TEST_ROOT/veetbot.env"
PROFILE_AUTH_FILE="$TEST_ROOT/browser-profile-auth"
PROFILE_SESSION_FILE="$TEST_ROOT/browser-profile-session-secret"
PROFILE_KEY_DIR="$TEST_ROOT/browser-profile-keys"
SURFACE_ENV_FILE="$TEST_ROOT/veetbot-surface.env"
SURFACE_TELEGRAM_FILE="$TEST_ROOT/telegram-bot-token"
SURFACE_WA_ACCESS_FILE="$TEST_ROOT/whatsapp-access-token"
SURFACE_WA_APP_FILE="$TEST_ROOT/whatsapp-app-secret"
SURFACE_WA_HOOK_FILE="$TEST_ROOT/whatsapp-verify-token"
LOG_FILE="$TEST_ROOT/commands.log"
DOCKER_IMAGES="$TEST_ROOT/docker-images"
mkdir -p "$BIN_DIR" "$DEPLOY_ROOT/releases" "$SYSTEMD_DIR" "$PROCESS_ROOT/4242"
mkdir -m 0700 "$PROFILE_KEY_DIR"
printf '%s\n' 'synthetic-browser-profile-auth-value' >"$PROFILE_AUTH_FILE"
printf '%s\n' 'synthetic-browser-profile-session-secret-value' >"$PROFILE_SESSION_FILE"
printf '%s\n' 'key-v1' >"$PROFILE_KEY_DIR/current"
printf '%s\n' 'c3ludGhldGljLWtleS1ub3QtdXNlZC1ieS1zdHVi' >"$PROFILE_KEY_DIR/key-v1.key"
chmod 0600 "$PROFILE_AUTH_FILE" "$PROFILE_SESSION_FILE" \
  "$PROFILE_KEY_DIR/current" "$PROFILE_KEY_DIR/key-v1.key"
printf '%s%s\n' 'telegram-test-' 'value-1234' >"$SURFACE_TELEGRAM_FILE"
printf '%s%s\n' 'whatsapp-token-' 'value-1234' >"$SURFACE_WA_ACCESS_FILE"
printf '%s%s\n' 'whatsapp-app-' 'value-1234' >"$SURFACE_WA_APP_FILE"
printf '%s%s\n' 'whatsapp-hook-' 'value-1234' >"$SURFACE_WA_HOOK_FILE"
chmod 0600 \
  "$SURFACE_TELEGRAM_FILE" \
  "$SURFACE_WA_ACCESS_FILE" \
  "$SURFACE_WA_APP_FILE" \
  "$SURFACE_WA_HOOK_FILE"
printf '%s\n' \
  "DATABASE_URL=postgresql+asyncpg://agent:test"\
"@127.0.0.1:5432/agent" \
  'DEPLOYMENT_MODE=production' \
  'AUTH_MODE=token' \
  'AUTH_TENANT_ID=test' \
  'AUTH_PRINCIPAL_ID=surface' \
  'AUTH_ROLES=surface' \
  'AUTH_SCOPES=run.read,run.write,surface.read,surface.write' \
  'AGENT_SURFACE_API_ENABLED=0' \
  'AGENT_SURFACE_WORKER_ENABLED=0' \
  'AGENT_SURFACE_WHATSAPP_ENABLED=0' >"$SURFACE_ENV_FILE"
: >"$LOG_FILE"
test_database_url='postgresql+asyncpg://agent:'
test_database_url+='test@127.0.0.1:5432/agent'
printf '%s\n' \
  "DATABASE_URL=$test_database_url" \
  'DEPLOYMENT_MODE=production' \
  'AUTH_MODE=token' \
  'AUTH_TOKEN=synthetic-test-token' \
  'AUTH_TENANT_ID=test' \
  'AUTH_PRINCIPAL_ID=test' \
  'AUTH_SCOPES=notification.read' \
  'SANDBOX_MECHANISM=gvisor' \
  'AGENT_ARTIFACT_ROOT=/tmp' \
  'VEETBOT_OPENAI_KEY=synthetic-test-provider-key' \
  "BROWSER_PROFILE_SERVICE_AUTH_FILE=$PROFILE_AUTH_FILE" \
  "BROWSER_PROFILE_SESSION_SECRET_FILE=$PROFILE_SESSION_FILE" \
  'BROWSER_PROFILE_CEREMONY_BASE_URL=https://browser.example.test' \
  "BROWSER_PROFILE_KEY_DIR=$PROFILE_KEY_DIR" >"$ENV_FILE"

# A negated pipeline never trips errexit, so a forbidden log line must fail
# through an explicit exit.
assert_log_lacks() {
  if grep -Fq -- "$1" "$LOG_FILE"; then
    printf 'forbidden command was run: %s\n' "$1" >&2
    exit 1
  fi
}

write_stub() {
  local name="$1"
  shift
  printf '#!/usr/bin/env bash\nset -Eeuo pipefail\n%s\n' "$*" >"$BIN_DIR/$name"
  chmod +x "$BIN_DIR/$name"
}

write_stub uv '
  printf "uv %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  [[ "${VEETBOT_TEST_FAIL_UV:-0}" != 1 ]]
'
write_stub flock 'exit 0'
write_stub stat '
  file="${!#}"
  owner=veetbot
  if [[ "$file" == *bland-signing ]]; then owner=veetbot-call-ingress; fi
  if [[ "${VEETBOT_TEST_BAD_CALL_OWNER:-}" == "$file" ]]; then owner=unrelated; fi
  mode="$(/usr/bin/stat -c %a "$file" 2>/dev/null || /usr/bin/stat -f %Lp "$file")"
  if [[ -n "${VEETBOT_TEST_PERMISSIONS:-}" ]]; then
    while read -r prescribed_owner _ prescribed_mode prescribed_path; do
      if [[ "$prescribed_path" == "$file" ]]; then
        owner="$prescribed_owner"
        mode="${prescribed_mode#0}"
      fi
    done <"$VEETBOT_TEST_PERMISSIONS"
  fi
  printf "%s:%s\n" "$owner" "$mode"
'
write_stub getfacl '
  file="${!#}"
  [[ -e "$file" ]] || { printf "getfacl: %s: Permission denied\n" "$file" >&2; exit 1; }
  if [[ "${VEETBOT_TEST_BAD_CALL_ACL:-}" == "$file" ]]; then
    printf "user::rw-\nuser:unrelated:r--\ngroup::---\nmask::r--\nother::---\n"
  else
    printf "user::rw-\ngroup::---\nother::---\n"
  fi
'
write_stub mv '
  if [[ "${1:-}" == -Tf ]]; then
    shift
    source="$1"
    target="$2"
    /bin/rm -f -- "$target"
    /bin/mv "$source" "$target"
  else
    /bin/mv "$@"
  fi
'
write_stub readlink '
  if [[ "${1:-}" == -f ]]; then
    shift
    target="$(/usr/bin/readlink "$1")" || exit 1
    if [[ "$target" = /* ]]; then printf "%s\n" "$target"; else
      printf "%s/%s\n" "$(cd "$(dirname "$1")" && pwd)" "$target"
    fi
  else
    /usr/bin/readlink "$@"
  fi
'
write_stub docker '
  printf "docker %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  image_state() { printf "%s/%s" "$VEETBOT_TEST_DOCKER_IMAGES" "$1"; }
  add_tag() {
    local reference="$1" state
    mkdir -p "$VEETBOT_TEST_DOCKER_IMAGES"
    state="$(image_state "${reference%%:*}")"
    grep -Fxq -- "${reference#*:}" "$state" 2>/dev/null \
      || printf "%s\n" "${reference#*:}" >>"$state"
  }
  if [[ "${1:-}" == run && " $* " == *" --entrypoint /bin/rm "* ]]; then
    target="${!#}"
    [[ "$target" == /releases/* ]]
    /bin/rm -rf -- "$VEETBOT_ROOT/releases/${target##*/}"
  elif [[ "${1:-}" == build ]]; then
    while (($#)); do
      if [[ "$1" == -t ]]; then add_tag "$2"; shift 2; else shift; fi
    done
  elif [[ "${1:-}" == tag ]]; then
    add_tag "$3"
  elif [[ "${1:-}" == image && "${2:-}" == ls ]]; then
    state="$(image_state "${!#}")"
    [[ -f "$state" ]] && cat "$state"
    exit 0
  elif [[ "${1:-}" == image && "${2:-}" == rm ]]; then
    reference="$3"
    state="$(image_state "${reference%%:*}")"
    if [[ "$reference" == "${VEETBOT_TEST_UNREMOVABLE_IMAGE:-}" ]]; then
      printf "Error response from daemon: conflict: unable to remove %s\n" "$reference" >&2
      exit 1
    fi
    if ! grep -Fxq -- "${reference#*:}" "$state" 2>/dev/null; then
      printf "Error response from daemon: No such image: %s\n" "$reference" >&2
      exit 1
    fi
    grep -Fxv -- "${reference#*:}" "$state" >"$state.next" || true
    /bin/mv "$state.next" "$state"
  elif [[ "${1:-}" == ps ]]; then
    printf "%s\n" "${VEETBOT_TEST_RUNNING_IMAGES:-}"
  elif [[ "${1:-}" == system && "${2:-}" == df ]]; then
    printf "TYPE   TOTAL  ACTIVE  SIZE  RECLAIMABLE\n"
  fi
'
write_stub rm '
  target="${!#}"
  if [[ -n "${VEETBOT_TEST_UNPRUNABLE_RELEASE:-}" \
    && "$target" == "$VEETBOT_ROOT/releases/$VEETBOT_TEST_UNPRUNABLE_RELEASE" ]]; then
    printf "rm: cannot remove %s: Permission denied\n" "$target" >&2
    exit 1
  fi
  /bin/rm "$@"
'
write_stub sudo '
  printf "sudo %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  "$@"
'
write_stub systemctl '
  printf "systemctl %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  if [[ "${1:-}" == show ]]; then
    if [[ " $* " == *" veetbot-execution "* ]]; then
      printf "4343\n"
    else
      printf "4242\n"
    fi
  fi
'
write_stub curl '
  headers=""
  request_url=""
  while (($#)); do
    if [[ "$1" == --dump-header ]]; then
      headers="$2"
      shift 2
    else
      request_url="$1"
      shift
    fi
  done
  if [[ -n "$headers" ]]; then
    printf "curl health\n" >>"$VEETBOT_TEST_LOG"
    if [[ "${VEETBOT_TEST_FAIL_HEALTH:-0}" == 1 ]]; then exit 1; fi
    printf "HTTP/1.1 200 OK\r\nX-Veetbot-Release: %s\r\n\r\n" \
      "$VEETBOT_TEST_READY_RELEASE" >"$headers"
  else
    cat >"$VEETBOT_TEST_AUTH_HEADERS"
    printf "curl session-index %s\n" "$request_url" >>"$VEETBOT_TEST_LOG"
    if [[ "${VEETBOT_TEST_FAIL_SESSION_INDEX:-0}" == 1 ]]; then exit 1; fi
    printf "%s" "${VEETBOT_TEST_SESSION_STATUS:-200}"
  fi
'

make_stage() {
  local release_id="$1"
  local stage="$DEPLOY_ROOT/releases/$release_id"
  mkdir -p \
    "$stage/deploy/systemd" \
    "$stage/deploy" \
    "$stage/execution" \
    "$stage/scripts" \
    "$stage/.venv/bin"
  touch \
    "$stage/pyproject.toml" \
    "$stage/uv.lock" \
    "$stage/alembic.ini" \
    "$stage/docker-compose.yml" \
    "$stage/deploy/docker-compose.production.yml" \
    "$stage/deploy/browser-profile-service.Dockerfile" \
    "$stage/deploy/veetbot-schedule.env.example" \
    "$stage/deploy/veetbot-notify.env.example" \
    "$stage/deploy/veetbot-surface.env.example" \
    "$stage/deploy/veetbot-call.env.example" \
    "$stage/deploy/veetbot-call-ingress.env.example" \
    "$stage/execution/sandbox.Dockerfile" \
    "$stage/scripts/check_schedule_database_permissions.py" \
    "$stage/scripts/check_production_deployment.py"
  for unit in \
    veetbot-api \
    veetbot-worker \
    veetbot-async-worker \
    veetbot-execution \
    veetbot-maintenance \
    veetbot-schedule \
    veetbot-notify \
    veetbot-surface \
    veetbot-call \
    veetbot-call-ingress; do
    printf '[Service]\nWorkingDirectory=/opt/veetbot/current\n' \
      >"$stage/deploy/systemd/$unit.service"
  done
  printf '%s\n' \
    '[Service]' \
    'WorkingDirectory=/opt/veetbot/current' \
    'EnvironmentFile=/etc/veetbot/veetbot-schedule.env' \
    >"$stage/deploy/systemd/veetbot-schedule.service"
  printf '%s\n' \
    '[Service]' \
    'WorkingDirectory=/opt/veetbot/current' \
    'EnvironmentFile=/etc/veetbot/veetbot-notify.env' \
    >"$stage/deploy/systemd/veetbot-notify.service"
  printf '%s\n' \
    '[Service]' \
    'WorkingDirectory=/opt/veetbot/current' \
    'EnvironmentFile=/etc/veetbot/veetbot-surface.env' \
    >"$stage/deploy/systemd/veetbot-surface.service"
  printf '#!/usr/bin/env bash\nprintf "alembic %%s\\n" "$*" >>"$VEETBOT_TEST_LOG"\n' \
    >"$stage/.venv/bin/alembic"
  printf '#!/usr/bin/env bash\nprintf "python %%s\\n" "$*" >>"$VEETBOT_TEST_LOG"\nprintf "execution socket %%s\\n" "${AGENT_EXECUTION_SERVICE_SOCKET:-missing}" >>"$VEETBOT_TEST_LOG"\nif [[ "${VEETBOT_TEST_FAIL_SCHEDULE_PERMISSION:-0}" == 1 && "${1:-}" == scripts/check_schedule_database_permissions.py ]]; then exit 1; fi\n' \
    >"$stage/.venv/bin/python"
  chmod +x "$stage/.venv/bin/alembic" "$stage/.venv/bin/python"
}

run_release() {
  local release_id="$1"
  # CircleCI's BASH_ENV prepends the real uv path in each stub subprocess.
  PATH="$BIN_DIR:$PATH" \
  BASH_ENV=/dev/null \
  VEETBOT_ROOT="$DEPLOY_ROOT" \
  VEETBOT_ENV_FILE="${VEETBOT_TEST_ENV_FILE:-$ENV_FILE}" \
  VEETBOT_SCHEDULE_ENV_FILE="${VEETBOT_TEST_SCHEDULE_ENV_FILE:-$TEST_ROOT/veetbot-schedule.env}" \
  VEETBOT_NOTIFY_ENV_FILE="${VEETBOT_TEST_NOTIFY_ENV_FILE:-$TEST_ROOT/veetbot-notify.env}" \
  VEETBOT_SURFACE_ENV_FILE="${VEETBOT_TEST_SURFACE_ENV_FILE:-$SURFACE_ENV_FILE}" \
  VEETBOT_CALL_ENV_FILE="${VEETBOT_TEST_CALL_ENV_FILE:-$TEST_ROOT/call-worker.env}" \
  VEETBOT_CALL_INGRESS_ENV_FILE="${VEETBOT_TEST_CALL_INGRESS_ENV_FILE:-$TEST_ROOT/call-ingress.env}" \
  VEETBOT_BROWSER_CONTROL_PLANE_CREDENTIAL_FILE="$PROFILE_AUTH_FILE" \
  VEETBOT_SYSTEMD_DIR="$SYSTEMD_DIR" \
  VEETBOT_PROCESS_ROOT="$PROCESS_ROOT" \
  VEETBOT_KEEP_RELEASES=2 \
  VEETBOT_HEALTH_TIMEOUT_SECS=2 \
  VEETBOT_TEST_LOG="$LOG_FILE" \
  VEETBOT_TEST_AUTH_HEADERS="$TEST_ROOT/session-index-headers" \
  VEETBOT_TEST_RELEASE="$release_id" \
  VEETBOT_TEST_READY_RELEASE="${VEETBOT_TEST_READY_RELEASE:-$release_id}" \
  VEETBOT_TEST_UNPRUNABLE_RELEASE="${VEETBOT_TEST_UNPRUNABLE_RELEASE:-}" \
  VEETBOT_TEST_DOCKER_IMAGES="$DOCKER_IMAGES" \
  VEETBOT_TEST_RUNNING_IMAGES="${VEETBOT_TEST_RUNNING_IMAGES:-}" \
  VEETBOT_TEST_UNREMOVABLE_IMAGE="${VEETBOT_TEST_UNREMOVABLE_IMAGE:-}" \
  VEETBOT_TEST_FAIL_SCHEDULE_PERMISSION="${VEETBOT_TEST_FAIL_SCHEDULE_PERMISSION:-0}" \
  VEETBOT_API_BASE_URL="${VEETBOT_TEST_API_BASE_URL:-http://127.0.0.1:8000/}" \
    "$RELEASE_SCRIPT" "$release_id"
}

if run_release invalid-release >/dev/null 2>&1; then
  printf 'invalid release id unexpectedly succeeded\n' >&2
  exit 1
fi

invalid_base_id="20260810-152230-abcde00"
if VEETBOT_TEST_API_BASE_URL='https://api.example.test/v1?tenant=test' \
  run_release "$invalid_base_id" >"$TEST_ROOT/invalid-base.out" 2>&1; then
  printf 'release with a query-bearing API base URL unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'VEETBOT_API_BASE_URL must be an HTTP(S) URL without a query or fragment' \
  "$TEST_ROOT/invalid-base.out"

for old_revision in 0000001 0000002 0000003; do
  mkdir -p "$DEPLOY_ROOT/releases/20260809-12000${old_revision: -1}-$old_revision"
done

release_id="20260810-152233-abcdef0"
make_stage "$release_id"
ln -s "$DEPLOY_ROOT/releases/$release_id" "$PROCESS_ROOT/4242/cwd"
legacy_unprunable_id="20260809-120001-0000001"
mkdir -p "$DOCKER_IMAGES"
# The oldest sandbox tag has no release directory left, so only tag-based
# retention can remove it.
printf '%s\n' \
  production \
  20260808-000000-0000000 \
  20260809-120001-0000001 \
  20260809-120002-0000002 \
  20260809-120003-0000003 >"$DOCKER_IMAGES/agent-core-sandbox"
printf '%s\n' \
  local \
  20260809-120001-0000001 \
  20260809-120002-0000002 \
  20260809-120003-0000003 >"$DOCKER_IMAGES/veetbot-browser-profile-service"
running_images="$(printf '%s\n' \
  'veetbot-browser-profile-service:20260809-120001-0000001' \
  'agent-core-sandbox:production')"
VEETBOT_TEST_UNPRUNABLE_RELEASE="$legacy_unprunable_id" \
  VEETBOT_TEST_RUNNING_IMAGES="$running_images" \
  VEETBOT_TEST_UNREMOVABLE_IMAGE="agent-core-sandbox:20260809-120002-0000002" \
  run_release "$release_id" >"$TEST_ROOT/first.out" 2>&1
cat "$TEST_ROOT/first.out"

[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$release_id" ]]
[[ -f "$DEPLOY_ROOT/releases/$release_id/.release.env" ]]
grep -Fxq 'AGENT_EXECUTION_SERVICE_SOCKET=/run/veetbot/execution.sock' \
  "$DEPLOY_ROOT/releases/$release_id/.release.env"
grep -Fq 'alembic upgrade head' "$LOG_FILE"
grep -Fxq 'execution socket /run/veetbot/execution.sock' "$LOG_FILE"
grep -Fq 'docker build -f execution/sandbox.Dockerfile' "$LOG_FILE"
grep -Fq 'docker build -f deploy/browser-profile-service.Dockerfile' "$LOG_FILE"
grep -Fq 'docker compose --env-file' "$LOG_FILE"
grep -Fq -- '--project-name veetbot' "$LOG_FILE"
grep -Fq \
  'systemctl restart veetbot-execution veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api' \
  "$LOG_FILE"
grep -Fq 'systemctl disable --now veetbot-schedule' "$LOG_FILE"
grep -Fq 'systemctl disable --now veetbot-notify' "$LOG_FILE"
grep -Fq 'curl session-index' "$LOG_FILE"
grep -Fq 'curl session-index http://127.0.0.1:8000/v1/sessions?limit=1' "$LOG_FILE"
auth_scheme='Bearer'
grep -Fxq "Authorization: $auth_scheme synthetic-test-token" \
  "$TEST_ROOT/session-index-headers"
[[ ! -d "$DEPLOY_ROOT/releases/20260809-120001-0000001" ]]
grep -Fq \
  "docker run --rm --pull=never --network none --read-only --user 0:0 --volume $DEPLOY_ROOT/releases:/releases --entrypoint /bin/rm agent-core-sandbox:$release_id -rf -- /releases/$legacy_unprunable_id" \
  "$LOG_FILE"

# Image retention keeps the newest two timestamped tags per repository, the
# tag any running container uses, and every non-timestamp tag; a removal that
# fails is reported without failing the release.
expected_sandbox_tags="$(printf '%s\n' \
  production 20260809-120002-0000002 20260809-120003-0000003 "$release_id")"
[[ "$(cat "$DOCKER_IMAGES/agent-core-sandbox")" == "$expected_sandbox_tags" ]] || {
  printf 'unexpected sandbox tags after retention:\n' >&2
  cat "$DOCKER_IMAGES/agent-core-sandbox" >&2
  exit 1
}
expected_profile_tags="$(printf '%s\n' \
  local 20260809-120001-0000001 20260809-120003-0000003 "$release_id")"
[[ "$(cat "$DOCKER_IMAGES/veetbot-browser-profile-service")" == "$expected_profile_tags" ]] || {
  printf 'unexpected browser-profile tags after retention:\n' >&2
  cat "$DOCKER_IMAGES/veetbot-browser-profile-service" >&2
  exit 1
}
grep -Fq 'docker image rm agent-core-sandbox:20260808-000000-0000000' "$LOG_FILE"
grep -Fq 'docker image rm agent-core-sandbox:20260809-120001-0000001' "$LOG_FILE"
grep -Fq 'docker image rm veetbot-browser-profile-service:20260809-120002-0000002' "$LOG_FILE"
assert_log_lacks 'docker image rm agent-core-sandbox:production'
assert_log_lacks 'docker image rm veetbot-browser-profile-service:local'
assert_log_lacks 'docker image rm veetbot-browser-profile-service:20260809-120001-0000001'
grep -Fq 'could not remove stale image agent-core-sandbox:20260809-120002-0000002' \
  "$TEST_ROOT/first.out"
grep -Fq 'Released 20260810-152233-abcdef0 successfully.' "$TEST_ROOT/first.out"
grep -Fxq 'docker builder prune --all --force --filter until=48h' "$LOG_FILE"
[[ "$(grep -Fxc 'docker system df' "$LOG_FILE")" == 2 ]]

if run_release "$release_id" >"$TEST_ROOT/active.out" 2>&1; then
  printf 'active release mutation unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'refusing to modify the active release in place' "$TEST_ROOT/active.out"
[[ -d "$DEPLOY_ROOT/releases/$release_id" ]]

stale_id="20260809-010101-1234567"
make_stage "$stale_id"
if run_release "$stale_id" >"$TEST_ROOT/stale.out" 2>&1; then
  printf 'stale release unexpectedly succeeded\n' >&2
  exit 1
fi
[[ ! -e "$DEPLOY_ROOT/releases/$stale_id" ]]
grep -Fq 'refusing stale release' "$TEST_ROOT/stale.out"
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$release_id" ]]

no_auth_env="$TEST_ROOT/no-auth.env"
grep -v '^AUTH_TOKEN=' "$ENV_FILE" >"$no_auth_env"
no_auth_id="20260810-152238-0000000"
make_stage "$no_auth_id"
if VEETBOT_TEST_ENV_FILE="$no_auth_env" run_release "$no_auth_id" \
  >"$TEST_ROOT/no-auth.out" 2>&1; then
  printf 'release without an API probe token unexpectedly succeeded\n' >&2
  exit 1
fi
[[ ! -e "$DEPLOY_ROOT/releases/$no_auth_id" ]]
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$release_id" ]]
grep -Fq 'AUTH_TOKEN is required for the API contract probe' "$TEST_ROOT/no-auth.out"

unsupported_id="20260810-152239-bcdef00"
make_stage "$unsupported_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$unsupported_id" "$PROCESS_ROOT/4242/cwd"
if VEETBOT_TEST_FAIL_SESSION_INDEX=1 run_release "$unsupported_id" \
  >"$TEST_ROOT/unsupported.out" 2>&1; then
  printf 'release without the session index unexpectedly succeeded\n' >&2
  exit 1
fi
[[ -d "$DEPLOY_ROOT/releases/$unsupported_id" ]]
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$unsupported_id" ]]
grep -Fq 'promoted API does not expose the authoritative session index' \
  "$TEST_ROOT/unsupported.out"

redirect_id="20260810-152241-bcdef02"
make_stage "$redirect_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$redirect_id" "$PROCESS_ROOT/4242/cwd"
if VEETBOT_TEST_SESSION_STATUS=302 run_release "$redirect_id" \
  >"$TEST_ROOT/redirect.out" 2>&1; then
  printf 'release with a redirecting session index unexpectedly succeeded\n' >&2
  exit 1
fi
[[ -d "$DEPLOY_ROOT/releases/$redirect_id" ]]
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$redirect_id" ]]
grep -Fq 'promoted API session index returned HTTP 302' "$TEST_ROOT/redirect.out"

failed_id="20260810-152244-bcdef01"
make_stage "$failed_id"
if VEETBOT_TEST_FAIL_UV=1 run_release "$failed_id" >/dev/null 2>&1; then
  printf 'failed staged release unexpectedly succeeded\n' >&2
  exit 1
fi
[[ ! -e "$DEPLOY_ROOT/releases/$failed_id" ]]
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$redirect_id" ]]

unhealthy_id="20260810-152255-cdef012"
make_stage "$unhealthy_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$unhealthy_id" "$PROCESS_ROOT/4242/cwd"
: >"$LOG_FILE"
if VEETBOT_TEST_FAIL_HEALTH=1 run_release "$unhealthy_id" \
  >"$TEST_ROOT/unhealthy.out" 2>&1; then
  printf 'unhealthy promoted release unexpectedly succeeded\n' >&2
  exit 1
fi
[[ -d "$DEPLOY_ROOT/releases/$unhealthy_id" ]]
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$unhealthy_id" ]]
grep -Fq "manual rollback target: $DEPLOY_ROOT/releases/$redirect_id" \
  "$TEST_ROOT/unhealthy.out"
grep -Fq 'systemctl --no-pager --full status veetbot-execution' "$LOG_FILE"

equal_timestamp_id="20260810-152255-0000000"
make_stage "$equal_timestamp_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$equal_timestamp_id" "$PROCESS_ROOT/4242/cwd"
printf '%s\n' production "$unhealthy_id" >"$DOCKER_IMAGES/agent-core-sandbox"
printf '%s\n' "$unhealthy_id" >"$DOCKER_IMAGES/veetbot-browser-profile-service"
: >"$LOG_FILE"
run_release "$equal_timestamp_id"
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == \
  "$DEPLOY_ROOT/releases/$equal_timestamp_id" ]]
# The store already satisfied the retention rule, so the step removes nothing.
assert_log_lacks 'docker image rm'
grep -Fxq 'docker builder prune --all --force --filter until=48h' "$LOG_FILE"
[[ "$(cat "$DOCKER_IMAGES/agent-core-sandbox")" == \
  "$(printf '%s\n' production "$unhealthy_id" "$equal_timestamp_id")" ]]

schedule_env="$TEST_ROOT/schedule.env"
schedule_worker_env="$TEST_ROOT/veetbot-schedule.env"
cp "$ENV_FILE" "$schedule_env"
printf '%s\n' \
  "DATABASE_URL=$test_database_url" \
  'DEPLOYMENT_MODE=production' \
  'AUTH_MODE=token' \
  'AUTH_TENANT_ID=test' \
  'AUTH_PRINCIPAL_ID=test' \
  'AUTH_SCOPES=session.read,schedule.read,schedule.write,schedule.cancel' \
  'AGENT_SCHEDULE_API_ENABLED=1' \
  'AGENT_SCHEDULE_WORKER_ENABLED=1' \
  'AGENT_NOTIFICATION_API_ENABLED=0' \
  'AGENT_NOTIFICATION_DISPATCH_ENABLED=0' >"$schedule_worker_env"
printf '%s\n' \
  'AGENT_SCHEDULE_API_ENABLED=1' \
  'AGENT_SCHEDULE_WORKER_ENABLED=1' >>"$schedule_env"
schedule_permission_id="20260810-152256-0000000"
make_stage "$schedule_permission_id"
if VEETBOT_TEST_FAIL_SCHEDULE_PERMISSION=1 \
  VEETBOT_TEST_ENV_FILE="$schedule_env" \
  VEETBOT_TEST_SCHEDULE_ENV_FILE="$schedule_worker_env" \
  run_release "$schedule_permission_id" \
  >"$TEST_ROOT/schedule-permission.out" 2>&1; then
  printf 'release with an under-privileged schedule role unexpectedly succeeded\n' >&2
  exit 1
fi
[[ ! -e "$DEPLOY_ROOT/releases/$schedule_permission_id" ]]
grep -Fq \
  'schedule database role does not satisfy the materialization contract' \
  "$TEST_ROOT/schedule-permission.out"
schedule_id="20260810-152256-0000001"
make_stage "$schedule_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$schedule_id" "$PROCESS_ROOT/4242/cwd"
VEETBOT_TEST_ENV_FILE="$schedule_env" \
  VEETBOT_TEST_SCHEDULE_ENV_FILE="$schedule_worker_env" \
  run_release "$schedule_id"
grep -Fxq "EnvironmentFile=$schedule_worker_env" \
  "$SYSTEMD_DIR/veetbot-schedule.service"
grep -Fq \
  'systemctl restart veetbot-schedule veetbot-execution veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api' \
  "$LOG_FILE"
grep -Fq 'python scripts/check_schedule_database_permissions.py' "$LOG_FILE"

schedule_notification_mismatch_env="$TEST_ROOT/schedule-notification-mismatch.env"
schedule_notification_mismatch_worker_env="$TEST_ROOT/schedule-notification-mismatch-worker.env"
schedule_notification_mismatch_notify_env="$TEST_ROOT/schedule-notification-mismatch-notify.env"
cp "$schedule_env" "$schedule_notification_mismatch_env"
cp "$schedule_worker_env" "$schedule_notification_mismatch_worker_env"
: >"$schedule_notification_mismatch_notify_env"
printf '%s\n' \
  'AGENT_NOTIFICATION_API_ENABLED=1' \
  'AGENT_NOTIFICATION_DISPATCH_ENABLED=1' >>"$schedule_notification_mismatch_env"
schedule_notification_mismatch_id="20260810-152257-0000002"
make_stage "$schedule_notification_mismatch_id"
if VEETBOT_TEST_ENV_FILE="$schedule_notification_mismatch_env" \
  VEETBOT_TEST_SCHEDULE_ENV_FILE="$schedule_notification_mismatch_worker_env" \
  VEETBOT_TEST_NOTIFY_ENV_FILE="$schedule_notification_mismatch_notify_env" \
  run_release "$schedule_notification_mismatch_id" \
  >"$TEST_ROOT/schedule-notification-mismatch.out" 2>&1; then
  printf 'release with mismatched schedule notification flags unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq \
  'schedule worker notification flags must match the application notification flags' \
  "$TEST_ROOT/schedule-notification-mismatch.out"

surface_env="$TEST_ROOT/surface-enabled.env"
surface_worker_env="$TEST_ROOT/veetbot-surface-enabled.env"
cp "$ENV_FILE" "$surface_env"
printf '%s\n' \
  'AGENT_SURFACE_API_ENABLED=1' \
  'AGENT_SURFACE_WORKER_ENABLED=1' \
  'AGENT_SURFACE_WHATSAPP_ENABLED=1' >>"$surface_env"
printf '%s\n' \
  "DATABASE_URL=$test_database_url" \
  'DEPLOYMENT_MODE=production' \
  'AUTH_MODE=token' \
  'AUTH_TENANT_ID=test' \
  'AUTH_PRINCIPAL_ID=surface' \
  'AUTH_ROLES=surface' \
  'AUTH_SCOPES=run.read,run.write,run.cancel,surface.read,surface.write,approval.read,approval.resolve' \
  'AGENT_SURFACE_API_ENABLED=1' \
  'AGENT_SURFACE_WORKER_ENABLED=1' \
  'AGENT_SURFACE_WHATSAPP_ENABLED=1' \
  "AGENT_SURFACE_TELEGRAM_TOKEN_FILE=$SURFACE_TELEGRAM_FILE" \
  "AGENT_SURFACE_WHATSAPP_TOKEN_FILE=$SURFACE_WA_ACCESS_FILE" \
  "AGENT_SURFACE_WHATSAPP_APP_SECRET_FILE=$SURFACE_WA_APP_FILE" \
  "AGENT_SURFACE_WHATSAPP_VERIFY_TOKEN_FILE=$SURFACE_WA_HOOK_FILE" \
  'AGENT_SURFACE_WHATSAPP_PHONE_NUMBER_ID=1234567890' \
  'AGENT_SURFACE_WHATSAPP_GRAPH_API_VERSION=v23.0' >"$surface_worker_env"
surface_id="20260810-152257-0000003"
make_stage "$surface_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$surface_id" "$PROCESS_ROOT/4242/cwd"
VEETBOT_TEST_ENV_FILE="$surface_env" \
  VEETBOT_TEST_SURFACE_ENV_FILE="$surface_worker_env" \
  run_release "$surface_id"
grep -Fxq "EnvironmentFile=$surface_worker_env" \
  "$SYSTEMD_DIR/veetbot-surface.service"
grep -Fq \
  'systemctl restart veetbot-surface veetbot-execution veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api' \
  "$LOG_FILE"
grep -Fxq 'AGENT_SURFACE_WHATSAPP_ENABLED=1' \
  "$DEPLOY_ROOT/releases/$surface_id/.release.env"

notify_env="$TEST_ROOT/notify.env"
notify_worker_env="$TEST_ROOT/veetbot-notify.env"
cp "$ENV_FILE" "$notify_env"
printf '%s\n' \
  "DATABASE_URL=$test_database_url" \
  'DEPLOYMENT_MODE=production' \
  'AUTH_MODE=token' \
  'AUTH_TENANT_ID=test' \
  'AUTH_PRINCIPAL_ID=notify' \
  'AUTH_ROLES=notify' \
  'AUTH_SCOPES=notification.read' \
  'AGENT_NOTIFICATION_API_ENABLED=1' \
  'AGENT_NOTIFICATION_DISPATCH_ENABLED=1' \
  'PUSH_PROVIDER=apns' \
  'APNS_KEY_FILE=/etc/veetbot/secrets/AuthKey_TEST.p8' \
  'APNS_KEY_ID=KEYID' \
  'APNS_TEAM_ID=TEAMID' \
  'APNS_TOPIC=com.veetbot.app' >"$notify_worker_env"
printf '%s\n' \
  'AGENT_NOTIFICATION_API_ENABLED=1' \
  'AGENT_NOTIFICATION_DISPATCH_ENABLED=1' >>"$notify_env"
notify_id="20260810-152258-0000002"
make_stage "$notify_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$notify_id" "$PROCESS_ROOT/4242/cwd"
VEETBOT_TEST_ENV_FILE="$notify_env" \
  VEETBOT_TEST_NOTIFY_ENV_FILE="$notify_worker_env" \
  run_release "$notify_id"
grep -Fxq "EnvironmentFile=$notify_worker_env" \
  "$SYSTEMD_DIR/veetbot-notify.service"
grep -Fq \
  'systemctl restart veetbot-notify veetbot-execution veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api' \
  "$LOG_FILE"

case_mismatch_id="20260810-152259-abcdef0"
make_stage "$case_mismatch_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$case_mismatch_id" "$PROCESS_ROOT/4242/cwd"
if VEETBOT_TEST_READY_RELEASE=20260810-152259-ABCDEF0 run_release "$case_mismatch_id" \
  >"$TEST_ROOT/case-mismatch.out" 2>&1; then
  printf 'release with a case-mismatched readiness identity unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq "local readiness probe did not report $case_mismatch_id" \
  "$TEST_ROOT/case-mismatch.out"

call_env="$TEST_ROOT/call-enabled.env"
cp "$ENV_FILE" "$call_env"
printf '%s\n' 'AGENT_CALL_ENABLED=1' 'AGENT_CALL_INGRESS_ENABLED=1' 'AGENT_CALL_NOTIFICATIONS_ENABLED=0' \
  "BLAND_CONFIGURATION_FILE=$TEST_ROOT/calls.json" >>"$call_env"
touch "$TEST_ROOT/calls.json" "$TEST_ROOT/bland-key" "$TEST_ROOT/bland-signing"
chmod 0600 "$TEST_ROOT/bland-key" "$TEST_ROOT/bland-signing"
for role in worker ingress; do
  printf '%s\n' 'AGENT_CALL_ENABLED=1' 'AGENT_CALL_INGRESS_ENABLED=1' \
    'AGENT_CALL_NOTIFICATIONS_ENABLED=0' "BLAND_CONFIGURATION_FILE=$TEST_ROOT/calls.json" \
    "AUTH_TENANT_ID=$(sed -n 's/^AUTH_TENANT_ID=//p' "$ENV_FILE")" \
    "AUTH_PRINCIPAL_ID=$(sed -n 's/^AUTH_PRINCIPAL_ID=//p' "$ENV_FILE")" \
    >"$TEST_ROOT/call-$role.env"
done
printf 'BLAND_API_KEY_FILE=%s\n' "$TEST_ROOT/bland-key" >>"$TEST_ROOT/call-worker.env"
printf 'BLAND_WEBHOOK_SECRET_FILE=%s\n' "$TEST_ROOT/bland-signing" >>"$TEST_ROOT/call-ingress.env"
# Refuse exposed credentials before starting services or promoting a release.
invalid_index=0
for private_file in "$TEST_ROOT/bland-key" "$TEST_ROOT/bland-signing"; do
  for fault in mode owner acl; do
    invalid_index=$((invalid_index + 1))
    invalid_call_id="20260810-152300-000003$invalid_index"
    make_stage "$invalid_call_id"
    rm -f -- "$PROCESS_ROOT/4242/cwd"
    ln -s "$DEPLOY_ROOT/releases/$invalid_call_id" "$PROCESS_ROOT/4242/cwd"
    export VEETBOT_TEST_BAD_CALL_OWNER="" VEETBOT_TEST_BAD_CALL_ACL=""
    case "$fault" in
      mode) chmod 0644 "$private_file" ;;
      owner) export VEETBOT_TEST_BAD_CALL_OWNER="$private_file" ;;
      acl) export VEETBOT_TEST_BAD_CALL_ACL="$private_file" ;;
    esac
    : >"$LOG_FILE"
    if VEETBOT_TEST_ENV_FILE="$call_env" run_release "$invalid_call_id" >"$TEST_ROOT/call-invalid.out" 2>&1; then
      printf 'calling release accepted invalid credential %s\n' "$fault" >&2
      exit 1
    fi
    grep -Fq 'calling role private credential file is missing or invalid' "$TEST_ROOT/call-invalid.out"
    assert_log_lacks 'systemctl restart'
    chmod 0600 "$private_file"
    unset VEETBOT_TEST_BAD_CALL_OWNER VEETBOT_TEST_BAD_CALL_ACL
  done
done

call_id="20260810-152300-0000027"
make_stage "$call_id"
for unit in call call-ingress; do
  printf 'EnvironmentFile=/etc/veetbot/veetbot-%s.env\n' "$unit" >>"$DEPLOY_ROOT/releases/$call_id/deploy/systemd/veetbot-$unit.service"
done
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$call_id" "$PROCESS_ROOT/4242/cwd"
VEETBOT_TEST_ENV_FILE="$call_env" run_release "$call_id"
grep -Fxq "EnvironmentFile=$TEST_ROOT/call-worker.env" "$SYSTEMD_DIR/veetbot-call.service"
grep -Fxq "EnvironmentFile=$TEST_ROOT/call-ingress.env" "$SYSTEMD_DIR/veetbot-call-ingress.service"
grep -Fxq 'AGENT_CALL_INGRESS_ENABLED=1' "$DEPLOY_ROOT/releases/$call_id/.release.env"

# The calling preflight runs as the unprivileged deploy identity, without sudo.
# Replay the permission commands docs/bland-setup.md prescribes against a
# private copy of /etc, then release as each documented deploy identity: the
# target veetbot-deploy account, a veetbot group member, and the veetbot
# account CircleCI uses on the current host. The suite runs unprivileged and
# owns every fixture, so the kernel consults only owner permission bits. Each
# path therefore receives, in the owner class, exactly the bits its prescribed
# owner, group and mode grant that identity, and the stat stub reports the
# prescribed owner and mode.
HOST_ROOT="$TEST_ROOT/host"
BLAND_GUIDE="$SCRIPT_DIR/../../docs/bland-setup.md"
prescribed_permissions="$TEST_ROOT/prescribed-permissions"
guide_permissions="$TEST_ROOT/guide-permissions"
effective_permissions="$TEST_ROOT/effective-permissions"
DEPLOY_IDENTITY="veetbot-deploy"
DEPLOY_IDENTITY_GROUPS="veetbot-deploy veetbot docker"
HOST_IDENTITY="veetbot"
HOST_IDENTITY_GROUPS="veetbot docker"

guide_failure() {
  printf 'docs/bland-setup.md calling permissions: %s\n' "$*" >&2
  exit 1
}

prescribe() {
  local owner="$1" group="$2" mode="$3" path="$4"
  [[ "$path" == /etc || "$path" == /etc/* ]] || guide_failure "path outside /etc: $path"
  [[ -e "$HOST_ROOT$path" ]] || guide_failure "no fixture for $path"
  [[ "$mode" =~ ^0[0-7]{3}$ ]] || guide_failure "mode must be four octal digits: $mode"
  printf '%s %s %s %s\n' "$owner" "$group" "$mode" "$HOST_ROOT$path" >>"$prescribed_permissions"
}

# Print "owner group mode" for the latest prescription of a path.
prescription_of() {
  awk -v path="$HOST_ROOT$1" '$4 == path { entry = $1 " " $2 " " $3 } END { print entry }' \
    "$prescribed_permissions"
}

replay_guide_command() {
  local command="$1" owner="root" group="root" mode="" target current
  local -a words
  read -r -a words <<<"$command"
  [[ "${words[0]:-}" == sudo ]] || guide_failure "unsupported command: $command"
  case "${words[1]:-}" in
    install)
      [[ "${words[2]:-}" == -d ]] || guide_failure "unsupported command: $command"
      set -- "${words[@]:3}"
      while (($# > 1)); do
        case "$1" in
          -m) mode="$2" ;;
          -o) owner="$2" ;;
          -g) group="$2" ;;
          *) guide_failure "unsupported command: $command" ;;
        esac
        shift 2
      done
      (($# == 1)) || guide_failure "unsupported command: $command"
      prescribe "$owner" "$group" "$mode" "$1"
      ;;
    chown)
      [[ "${words[2]:-}" == *:* ]] || guide_failure "chown must name owner:group: $command"
      for target in "${words[@]:3}"; do
        read -r _ _ mode <<<"$(prescription_of "$target")"
        prescribe "${words[2]%%:*}" "${words[2]#*:}" "$mode" "$target"
      done
      ;;
    chmod)
      for target in "${words[@]:3}"; do
        read -r owner group _ <<<"$(prescription_of "$target")"
        prescribe "$owner" "$group" "${words[2]}" "$target"
      done
      ;;
    setfacl)
      # Removing extended entries leaves the base ACL the stub reports.
      [[ "${words[2]:-}" == -b ]] || guide_failure "unsupported command: $command"
      for target in "${words[@]:3}"; do
        current="$(prescription_of "$target")"
        [[ -n "$current" ]] || guide_failure "no fixture for $target"
      done
      ;;
    *) guide_failure "unsupported command: $command" ;;
  esac
}

# Grant each path, in the owner class, the bits its latest prescription gives
# the identity. Descendants sort first, so closing a directory never blocks a
# later change beneath it.
apply_permissions_as() {
  local identity="$1" groups=" $2 " owner group mode path bits
  chmod -R u+rwX "$HOST_ROOT"
  awk '{ entry[$4] = $0 } END { for (path in entry) print entry[path] }' \
    "$prescribed_permissions" | LC_ALL=C sort -r -k4,4 >"$effective_permissions"
  while read -r owner group mode path; do
    if [[ "$owner" == "$identity" ]]; then
      bits="${mode:1:1}"
    elif [[ "$groups" == *" $group "* ]]; then
      bits="${mode:2:1}"
    else
      bits="${mode:3:1}"
    fi
    chmod "0${bits}00" "$path"
  done <"$effective_permissions"
}

release_as() {
  local release_id="$1" identity="$2" groups="$3" unit
  apply_permissions_as "$identity" "$groups"
  make_stage "$release_id"
  for unit in call call-ingress; do
    printf 'EnvironmentFile=/etc/veetbot/veetbot-%s.env\n' "$unit" \
      >>"$DEPLOY_ROOT/releases/$release_id/deploy/systemd/veetbot-$unit.service"
  done
  rm -f -- "$PROCESS_ROOT/4242/cwd"
  ln -s "$DEPLOY_ROOT/releases/$release_id" "$PROCESS_ROOT/4242/cwd"
  : >"$LOG_FILE"
  VEETBOT_TEST_PERMISSIONS="$effective_permissions" \
    VEETBOT_TEST_ENV_FILE="$HOST_ROOT/etc/veetbot/veetbot.env" \
    VEETBOT_TEST_CALL_ENV_FILE="$HOST_ROOT/etc/veetbot/veetbot-call.env" \
    VEETBOT_TEST_CALL_INGRESS_ENV_FILE="$HOST_ROOT/etc/veetbot/veetbot-call-ingress.env" \
    run_release "$release_id" </dev/null >"$TEST_ROOT/release-as.out" 2>&1
}

mkdir -p "$HOST_ROOT/etc/veetbot/secrets" "$HOST_ROOT/etc/veetbot-call-ingress"
host_key="$HOST_ROOT/etc/veetbot/secrets/bland-api-key"
host_signing="$HOST_ROOT/etc/veetbot-call-ingress/bland-webhook-secret"
touch "$host_key" "$host_signing"
cp "$call_env" "$HOST_ROOT/etc/veetbot/veetbot.env"
grep -v '^BLAND_API_KEY_FILE=' "$TEST_ROOT/call-worker.env" \
  >"$HOST_ROOT/etc/veetbot/veetbot-call.env"
printf 'BLAND_API_KEY_FILE=%s\n' "$host_key" >>"$HOST_ROOT/etc/veetbot/veetbot-call.env"
grep -v '^BLAND_WEBHOOK_SECRET_FILE=' "$TEST_ROOT/call-ingress.env" \
  >"$HOST_ROOT/etc/veetbot/veetbot-call-ingress.env"
printf 'BLAND_WEBHOOK_SECRET_FILE=%s\n' "$host_signing" \
  >>"$HOST_ROOT/etc/veetbot/veetbot-call-ingress.env"
# docs/deployment.md prescribes the application environment and the shared
# secret directory. Every other path starts root-owned and private, as sudo
# creates it, so the guide must grant any access the preflight needs.
: >"$prescribed_permissions"
prescribe root root 0755 /etc
prescribe root root 0755 /etc/veetbot
prescribe root veetbot 0640 /etc/veetbot/veetbot.env
prescribe root root 0711 /etc/veetbot/secrets
prescribe root root 0600 /etc/veetbot/secrets/bland-api-key
prescribe root root 0600 /etc/veetbot/veetbot-call.env
prescribe root root 0600 /etc/veetbot/veetbot-call-ingress.env
prescribe root root 0700 /etc/veetbot-call-ingress
prescribe root root 0600 /etc/veetbot-call-ingress/bland-webhook-secret

guide_commands="$(awk '
  /^## / { section = $0 }
  section == "## Separate the two service roles" && /^```bash$/ { block = 1; next }
  block && /^```$/ { exit }
  block { print }
' "$BLAND_GUIDE")"
[[ -n "$guide_commands" ]] || guide_failure "no bash block in 'Separate the two service roles'"
while IFS= read -r guide_line; do
  while [[ "$guide_line" == *\\ ]] && IFS= read -r continuation; do
    guide_line="${guide_line%\\}$continuation"
  done
  [[ -z "$guide_line" ]] || replay_guide_command "$guide_line"
done <<<"$guide_commands"
cp "$prescribed_permissions" "$guide_permissions"

permission_probe="$TEST_ROOT/permission-probe"
: >"$permission_probe"
chmod 0000 "$permission_probe"
if [[ -r "$permission_probe" ]]; then
  printf 'skipping deploy-identity calling permission cases: this user bypasses file permissions\n' >&2
else
  identity_index=0
  for identity in "$HOST_IDENTITY:$HOST_IDENTITY_GROUPS" "$DEPLOY_IDENTITY:$DEPLOY_IDENTITY_GROUPS"; do
    identity_index=$((identity_index + 1))
    identity_release_id="20260810-152301-000000$identity_index"
    if ! release_as "$identity_release_id" "${identity%%:*}" "${identity#*:}"; then
      cat "$TEST_ROOT/release-as.out" >&2
      printf 'calling release failed for deploy identity %s with the guide permissions\n' \
        "${identity%%:*}" >&2
      exit 1
    fi
    grep -Fxq "EnvironmentFile=$HOST_ROOT/etc/veetbot/veetbot-call-ingress.env" \
      "$SYSTEMD_DIR/veetbot-call-ingress.service"
    grep -Fxq 'systemctl restart veetbot-call-ingress' "$LOG_FILE"
  done

  # Layouts from the earlier guide fail before any service change and name the
  # path the deploy identity cannot reach.
  legacy_index=0
  while IFS='|' read -r identity legacy_permission expected; do
    legacy_index=$((legacy_index + 1))
    cp "$guide_permissions" "$prescribed_permissions"
    read -r -a legacy_words <<<"$legacy_permission"
    prescribe "${legacy_words[@]}"
    legacy_release_id="20260810-152302-000000$legacy_index"
    if release_as "$legacy_release_id" "${identity%%:*}" "${identity#*:}"; then
      printf 'calling release accepted legacy permissions: %s\n' "$legacy_permission" >&2
      exit 1
    fi
    if ! grep -Fxq "release failed: $expected" "$TEST_ROOT/release-as.out"; then
      cat "$TEST_ROOT/release-as.out" >&2
      printf 'missing diagnostic: %s\n' "$expected" >&2
      exit 1
    fi
    assert_log_lacks 'systemctl restart'
  done <<EOF
$HOST_IDENTITY:$HOST_IDENTITY_GROUPS|veetbot-call-ingress veetbot-call-ingress 0600 /etc/veetbot/veetbot-call-ingress.env|calling role environment is not readable by the deploy user: $HOST_ROOT/etc/veetbot/veetbot-call-ingress.env
$HOST_IDENTITY:$HOST_IDENTITY_GROUPS|veetbot-call-ingress veetbot-call-ingress 0700 /etc/veetbot-call-ingress|calling role credential directory is not traversable by the deploy user: $HOST_ROOT/etc/veetbot-call-ingress
$DEPLOY_IDENTITY:$DEPLOY_IDENTITY_GROUPS|veetbot veetbot 0600 /etc/veetbot/veetbot-call.env|calling role environment is not readable by the deploy user: $HOST_ROOT/etc/veetbot/veetbot-call.env
$DEPLOY_IDENTITY:$DEPLOY_IDENTITY_GROUPS|veetbot veetbot 0700 /etc/veetbot/secrets|calling role credential directory is not traversable by the deploy user: $HOST_ROOT/etc/veetbot/secrets
EOF
  cp "$guide_permissions" "$prescribed_permissions"
  chmod -R u+rwX "$HOST_ROOT"
fi

printf 'release script tests passed\n'
