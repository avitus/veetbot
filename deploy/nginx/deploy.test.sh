#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy.sh"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

BIN_DIR="$TEST_ROOT/bin"
DEPLOY_ROOT="$TEST_ROOT/opt/veetbot"
AVAILABLE="$TEST_ROOT/sites-available/veetbot"
ENABLED="$TEST_ROOT/sites-enabled/veetbot"
BACKUPS="$TEST_ROOT/backups"
LOG_FILE="$TEST_ROOT/commands.log"
FAIL_MARKER="$TEST_ROOT/nginx-failed"
SYSTEMCTL_FAIL_MARKER="$TEST_ROOT/systemctl-failed"
SOURCE_CONFIG="$TEST_ROOT/candidate.conf"
mkdir -p "$BIN_DIR" "$DEPLOY_ROOT/shared" "$(dirname "$AVAILABLE")" "$(dirname "$ENABLED")"
: >"$LOG_FILE"

write_stub() {
  local name="$1"
  shift
  printf '#!/usr/bin/env bash\nset -Eeuo pipefail\n%s\n' "$*" >"$BIN_DIR/$name"
  chmod +x "$BIN_DIR/$name"
}

write_stub sudo '
  printf "sudo %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  "$@"
'
write_stub flock 'exit 0'
write_stub nginx '
  printf "nginx %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  if [[ "${VEETBOT_TEST_NGINX_FAIL_ONCE:-0}" == 1 && ! -e "$VEETBOT_TEST_FAIL_MARKER" ]]; then
    touch "$VEETBOT_TEST_FAIL_MARKER"
    exit 1
  fi
'
write_stub systemctl '
  printf "systemctl %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  if [[ "${VEETBOT_TEST_SYSTEMCTL_FAIL_ONCE:-0}" == 1 && ! -e "$VEETBOT_TEST_SYSTEMCTL_FAIL_MARKER" ]]; then
    touch "$VEETBOT_TEST_SYSTEMCTL_FAIL_MARKER"
    exit 1
  fi
'

run_deploy() {
  PATH="$BIN_DIR:$PATH" \
  VEETBOT_ROOT="$DEPLOY_ROOT" \
  VEETBOT_NGINX_AVAILABLE="$AVAILABLE" \
  VEETBOT_NGINX_ENABLED="$ENABLED" \
  VEETBOT_NGINX_BACKUP_DIR="$BACKUPS" \
  VEETBOT_TEST_LOG="$LOG_FILE" \
  VEETBOT_TEST_FAIL_MARKER="$FAIL_MARKER" \
  VEETBOT_TEST_SYSTEMCTL_FAIL_MARKER="$SYSTEMCTL_FAIL_MARKER" \
    "$DEPLOY_SCRIPT" "${1:-$SOURCE_CONFIG}"
}

if run_deploy "$TEST_ROOT/missing.conf" >/dev/null 2>&1; then
  printf 'missing Nginx source unexpectedly succeeded\n' >&2
  exit 1
fi

printf 'server { return 204; }\n' >"$SOURCE_CONFIG"
run_deploy
cmp -s "$SOURCE_CONFIG" "$AVAILABLE"
[[ "$(readlink "$ENABLED")" == "$AVAILABLE" ]]
grep -Fq 'nginx -t' "$LOG_FILE"
grep -Fq 'systemctl reload nginx' "$LOG_FILE"

mkdir -p "$DEPLOY_ROOT/releases/20260810-152233-abcdef0"
printf 'VEETBOT_RELEASE_ID=20260810-152233-abcdef0\n' \
  >"$DEPLOY_ROOT/releases/20260810-152233-abcdef0/.release.env"
ln -s "$DEPLOY_ROOT/releases/20260810-152233-abcdef0" "$DEPLOY_ROOT/current"
printf 'server { return 418; }\n' >"$SOURCE_CONFIG"
VEETBOT_EXPECTED_RELEASE_ID=20260810-152244-bcdef01 run_deploy \
  >"$TEST_ROOT/stale.out"
grep -Fq 'Skipping stale Nginx deployment' "$TEST_ROOT/stale.out"
grep -Fq 'return 204' "$AVAILABLE"

printf 'server { return 200; }\n' >"$AVAILABLE"
printf 'server { return 503; }\n' >"$SOURCE_CONFIG"
rm -f -- "$FAIL_MARKER"
if VEETBOT_TEST_NGINX_FAIL_ONCE=1 run_deploy >/dev/null 2>&1; then
  printf 'invalid Nginx candidate unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'return 200' "$AVAILABLE"
[[ "$(find "$BACKUPS" -type f | wc -l | tr -d ' ')" -ge 1 ]]

printf 'server { return 201; }\n' >"$AVAILABLE"
printf 'server { return 502; }\n' >"$SOURCE_CONFIG"
rm -f -- "$SYSTEMCTL_FAIL_MARKER"
if VEETBOT_TEST_SYSTEMCTL_FAIL_ONCE=1 run_deploy >/dev/null 2>&1; then
  printf 'failed Nginx reload unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'return 201' "$AVAILABLE"

printf 'nginx deployment script tests passed\n'
