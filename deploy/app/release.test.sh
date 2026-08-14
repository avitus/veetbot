#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_SCRIPT="$SCRIPT_DIR/release.sh"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

BIN_DIR="$TEST_ROOT/bin"
DEPLOY_ROOT="$TEST_ROOT/opt/veetbot"
SYSTEMD_DIR="$TEST_ROOT/systemd"
PROCESS_ROOT="$TEST_ROOT/proc"
ENV_FILE="$TEST_ROOT/veetbot.env"
LOG_FILE="$TEST_ROOT/commands.log"
mkdir -p "$BIN_DIR" "$DEPLOY_ROOT/releases" "$SYSTEMD_DIR" "$PROCESS_ROOT/4242"
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
  'AUTH_SCOPES=session.read' \
  'SANDBOX_MECHANISM=gvisor' \
  'AGENT_ARTIFACT_ROOT=/tmp' \
  'VEETBOT_OPENAI_KEY=synthetic-test-provider-key' >"$ENV_FILE"

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
'
write_stub sudo '
  printf "sudo %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  "$@"
'
write_stub systemctl '
  printf "systemctl %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  if [[ "${1:-}" == show ]]; then printf "4242\n"; fi
'
write_stub curl '
  headers=""
  while (($#)); do
    if [[ "$1" == --dump-header ]]; then headers="$2"; shift 2; else shift; fi
  done
  if [[ -n "$headers" ]]; then
    printf "curl health\n" >>"$VEETBOT_TEST_LOG"
    if [[ "${VEETBOT_TEST_FAIL_HEALTH:-0}" == 1 ]]; then exit 1; fi
    printf "HTTP/1.1 200 OK\r\nX-Veetbot-Release: %s\r\n\r\n" "$VEETBOT_TEST_RELEASE" >"$headers"
  else
    cat >/dev/null
    printf "curl session-index\n" >>"$VEETBOT_TEST_LOG"
    if [[ "${VEETBOT_TEST_FAIL_SESSION_INDEX:-0}" == 1 ]]; then exit 1; fi
  fi
'

make_stage() {
  local release_id="$1"
  local stage="$DEPLOY_ROOT/releases/$release_id"
  mkdir -p \
    "$stage/deploy/systemd" \
    "$stage/execution" \
    "$stage/scripts" \
    "$stage/.venv/bin"
  touch \
    "$stage/pyproject.toml" \
    "$stage/uv.lock" \
    "$stage/alembic.ini" \
    "$stage/docker-compose.yml" \
    "$stage/deploy/docker-compose.production.yml" \
    "$stage/execution/sandbox.Dockerfile" \
    "$stage/scripts/check_production_deployment.py"
  for unit in veetbot-api veetbot-worker veetbot-maintenance; do
    printf '[Service]\nWorkingDirectory=/opt/veetbot/current\n' \
      >"$stage/deploy/systemd/$unit.service"
  done
  printf '#!/usr/bin/env bash\nprintf "alembic %%s\\n" "$*" >>"$VEETBOT_TEST_LOG"\n' \
    >"$stage/.venv/bin/alembic"
  printf '#!/usr/bin/env bash\nprintf "python %%s\\n" "$*" >>"$VEETBOT_TEST_LOG"\n' \
    >"$stage/.venv/bin/python"
  chmod +x "$stage/.venv/bin/alembic" "$stage/.venv/bin/python"
}

run_release() {
  local release_id="$1"
  # CircleCI's BASH_ENV prepends the real uv path in each stub subprocess.
  PATH="$BIN_DIR:$PATH" \
  BASH_ENV=/dev/null \
  VEETBOT_ROOT="$DEPLOY_ROOT" \
  VEETBOT_ENV_FILE="$ENV_FILE" \
  VEETBOT_SYSTEMD_DIR="$SYSTEMD_DIR" \
  VEETBOT_PROCESS_ROOT="$PROCESS_ROOT" \
  VEETBOT_KEEP_RELEASES=2 \
  VEETBOT_HEALTH_TIMEOUT_SECS=2 \
  VEETBOT_TEST_LOG="$LOG_FILE" \
  VEETBOT_TEST_RELEASE="$release_id" \
    "$RELEASE_SCRIPT" "$release_id"
}

if run_release invalid-release >/dev/null 2>&1; then
  printf 'invalid release id unexpectedly succeeded\n' >&2
  exit 1
fi

for old_revision in 0000001 0000002 0000003; do
  mkdir -p "$DEPLOY_ROOT/releases/20260809-12000${old_revision: -1}-$old_revision"
done

release_id="20260810-152233-abcdef0"
make_stage "$release_id"
ln -s "$DEPLOY_ROOT/releases/$release_id" "$PROCESS_ROOT/4242/cwd"
run_release "$release_id"

[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$release_id" ]]
[[ -f "$DEPLOY_ROOT/releases/$release_id/.release.env" ]]
grep -Fq 'alembic upgrade head' "$LOG_FILE"
grep -Fq 'docker build -f execution/sandbox.Dockerfile' "$LOG_FILE"
grep -Fq 'docker compose --env-file' "$LOG_FILE"
grep -Fq -- '--project-name veetbot' "$LOG_FILE"
grep -Fq 'systemctl restart veetbot-maintenance veetbot-worker veetbot-api' "$LOG_FILE"
grep -Fq 'curl session-index' "$LOG_FILE"
[[ ! -d "$DEPLOY_ROOT/releases/20260809-120001-0000001" ]]

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

failed_id="20260810-152244-bcdef01"
make_stage "$failed_id"
if VEETBOT_TEST_FAIL_UV=1 run_release "$failed_id" >/dev/null 2>&1; then
  printf 'failed staged release unexpectedly succeeded\n' >&2
  exit 1
fi
[[ ! -e "$DEPLOY_ROOT/releases/$failed_id" ]]
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$unsupported_id" ]]

unhealthy_id="20260810-152255-cdef012"
make_stage "$unhealthy_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$unhealthy_id" "$PROCESS_ROOT/4242/cwd"
if VEETBOT_TEST_FAIL_HEALTH=1 run_release "$unhealthy_id" \
  >"$TEST_ROOT/unhealthy.out" 2>&1; then
  printf 'unhealthy promoted release unexpectedly succeeded\n' >&2
  exit 1
fi
[[ -d "$DEPLOY_ROOT/releases/$unhealthy_id" ]]
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$DEPLOY_ROOT/releases/$unhealthy_id" ]]
grep -Fq "manual rollback target: $DEPLOY_ROOT/releases/$unsupported_id" \
  "$TEST_ROOT/unhealthy.out"

equal_timestamp_id="20260810-152255-0000000"
make_stage "$equal_timestamp_id"
rm -f -- "$PROCESS_ROOT/4242/cwd"
ln -s "$DEPLOY_ROOT/releases/$equal_timestamp_id" "$PROCESS_ROOT/4242/cwd"
run_release "$equal_timestamp_id"
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == \
  "$DEPLOY_ROOT/releases/$equal_timestamp_id" ]]

printf 'release script tests passed\n'
