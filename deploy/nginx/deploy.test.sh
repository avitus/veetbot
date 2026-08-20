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
INSTALL_FAIL_MARKER="$TEST_ROOT/install-failed"
SYMLINK_FAIL_MARKER="$TEST_ROOT/symlink-failed"
SOURCE_CONFIG="$TEST_ROOT/candidate.conf"
DOCS_ROOT="$DEPLOY_ROOT/docs"
DOCS_SOURCE="$TEST_ROOT/docs-source"
DOCS_ARCHIVE="$TEST_ROOT/veetbot-docs.tar.gz"
DOCS_CHECKSUM="$DOCS_ARCHIVE.sha256"
mkdir -p \
  "$BIN_DIR" \
  "$DEPLOY_ROOT/shared" \
  "$DOCS_SOURCE" \
  "$(dirname "$AVAILABLE")" \
  "$(dirname "$ENABLED")"
: >"$LOG_FILE"
printf '<h1>Veetbot documentation</h1>\n' >"$DOCS_SOURCE/index.html"
printf '20260810-152233-abcdef0\n' >"$DOCS_SOURCE/release.txt"
tar -czf "$DOCS_ARCHIVE" -C "$DOCS_SOURCE" .
(
  cd "$(dirname "$DOCS_ARCHIVE")"
  sha256sum "$(basename "$DOCS_ARCHIVE")" >"$(basename "$DOCS_CHECKSUM")"
)

write_stub() {
  local name="$1"
  shift
  printf '#!/usr/bin/env bash\nset -Eeuo pipefail\n%s\n' "$*" >"$BIN_DIR/$name"
  chmod +x "$BIN_DIR/$name"
}

write_stub sudo '
  printf "sudo %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  if [[ "${VEETBOT_TEST_INSTALL_FAIL_ONCE:-0}" == 1 \
    && "${1:-}" == install && " $* " == *" -m 0644 "* \
    && ! -e "$VEETBOT_TEST_INSTALL_FAIL_MARKER" ]]; then
    touch "$VEETBOT_TEST_INSTALL_FAIL_MARKER"
    exit 1
  fi
  if [[ "${VEETBOT_TEST_SYMLINK_FAIL_ONCE:-0}" == 1 \
    && "${1:-}" == ln && ! -e "$VEETBOT_TEST_SYMLINK_FAIL_MARKER" ]]; then
    touch "$VEETBOT_TEST_SYMLINK_FAIL_MARKER"
    exit 1
  fi
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
write_stub mv '
  if [[ "${1:-}" == -Tf ]]; then
    rm -f -- "$3"
    /bin/mv -f "$2" "$3"
  else
    /bin/mv "$@"
  fi
'

run_deploy() {
  PATH="$BIN_DIR:$PATH" \
  VEETBOT_ROOT="$DEPLOY_ROOT" \
  VEETBOT_NGINX_AVAILABLE="$AVAILABLE" \
  VEETBOT_NGINX_ENABLED="$ENABLED" \
  VEETBOT_NGINX_BACKUP_DIR="$BACKUPS" \
  VEETBOT_DOCS_ROOT="$DOCS_ROOT" \
  VEETBOT_TEST_LOG="$LOG_FILE" \
  VEETBOT_TEST_FAIL_MARKER="$FAIL_MARKER" \
  VEETBOT_TEST_SYSTEMCTL_FAIL_MARKER="$SYSTEMCTL_FAIL_MARKER" \
  VEETBOT_TEST_INSTALL_FAIL_MARKER="$INSTALL_FAIL_MARKER" \
  VEETBOT_TEST_SYMLINK_FAIL_MARKER="$SYMLINK_FAIL_MARKER" \
    "$DEPLOY_SCRIPT" \
      "${1:-$SOURCE_CONFIG}" \
      "${2:-}" \
      "${3:-}"
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
VEETBOT_EXPECTED_RELEASE_ID=20260810-152233-abcdef0 run_deploy \
  "$SOURCE_CONFIG" "$DOCS_ARCHIVE" "$DOCS_CHECKSUM"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]
cmp -s \
  "$DOCS_SOURCE/index.html" \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0/index.html"
grep -Fqx \
  '20260810-152233-abcdef0' \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0/release.txt"

printf 'server { return 418; }\n' >"$SOURCE_CONFIG"
if VEETBOT_EXPECTED_RELEASE_ID=20260810-152244-bcdef01 run_deploy \
  "$SOURCE_CONFIG" "$DOCS_ARCHIVE" "$DOCS_CHECKSUM" >"$TEST_ROOT/stale.out"; then
  printf 'stale Nginx deployment unexpectedly reported publication\n' >&2
  exit 1
else
  stale_status=$?
fi
[[ "$stale_status" == 3 ]] || {
  printf 'stale Nginx deployment returned status %s, expected 3\n' \
    "$stale_status" >&2
  exit 1
}
grep -Fq 'Skipping stale Nginx deployment' "$TEST_ROOT/stale.out"
grep -Fq 'return 204' "$AVAILABLE"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]

next_release_id=20260810-152244-bcdef01
mkdir -p "$DEPLOY_ROOT/releases/$next_release_id"
printf 'VEETBOT_RELEASE_ID=%s\n' "$next_release_id" \
  >"$DEPLOY_ROOT/releases/$next_release_id/.release.env"
ln -sfn "$DEPLOY_ROOT/releases/$next_release_id" "$DEPLOY_ROOT/current"

UNSAFE_DOCS_SOURCE="$TEST_ROOT/unsafe-docs-source"
UNSAFE_DOCS_ARCHIVE="$TEST_ROOT/unsafe-docs.tar.gz"
UNSAFE_DOCS_CHECKSUM="$UNSAFE_DOCS_ARCHIVE.sha256"
mkdir -p "$UNSAFE_DOCS_SOURCE"
printf '<h1>Unsafe documentation</h1>\n' >"$UNSAFE_DOCS_SOURCE/index.html"
ln -s /etc/passwd "$UNSAFE_DOCS_SOURCE/external-link"
tar -czf "$UNSAFE_DOCS_ARCHIVE" -C "$UNSAFE_DOCS_SOURCE" .
(
  cd "$(dirname "$UNSAFE_DOCS_ARCHIVE")"
  sha256sum "$(basename "$UNSAFE_DOCS_ARCHIVE")" \
    >"$(basename "$UNSAFE_DOCS_CHECKSUM")"
)
if VEETBOT_EXPECTED_RELEASE_ID="$next_release_id" run_deploy \
  "$SOURCE_CONFIG" "$UNSAFE_DOCS_ARCHIVE" "$UNSAFE_DOCS_CHECKSUM" \
  >/dev/null 2>&1; then
  printf 'documentation archive with a symlink unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]

NEXT_DOCS_SOURCE="$TEST_ROOT/next-docs-source"
NEXT_DOCS_ARCHIVE="$TEST_ROOT/next-docs.tar.gz"
NEXT_DOCS_CHECKSUM="$NEXT_DOCS_ARCHIVE.sha256"
mkdir -p "$NEXT_DOCS_SOURCE"
printf '<h1>Next documentation</h1>\n' >"$NEXT_DOCS_SOURCE/index.html"
printf '%s\n' "$next_release_id" >"$NEXT_DOCS_SOURCE/release.txt"
tar -czf "$NEXT_DOCS_ARCHIVE" -C "$NEXT_DOCS_SOURCE" .
(
  cd "$(dirname "$NEXT_DOCS_ARCHIVE")"
  sha256sum "$(basename "$NEXT_DOCS_ARCHIVE")" \
    >"$(basename "$NEXT_DOCS_CHECKSUM")"
)
printf 'server { return 200; }\n' >"$AVAILABLE"
printf 'server { return 503; }\n' >"$SOURCE_CONFIG"
rm -f -- "$FAIL_MARKER"
if VEETBOT_EXPECTED_RELEASE_ID="$next_release_id" \
  VEETBOT_TEST_NGINX_FAIL_ONCE=1 run_deploy \
    "$SOURCE_CONFIG" "$NEXT_DOCS_ARCHIVE" "$NEXT_DOCS_CHECKSUM" \
    >/dev/null 2>&1; then
  printf 'failed Nginx validation published documentation unexpectedly\n' >&2
  exit 1
fi
grep -Fq 'return 200' "$AVAILABLE"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]

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

printf 'server { return 202; }\n' >"$AVAILABLE"
printf 'server { return 504; }\n' >"$SOURCE_CONFIG"
rm -f -- "$INSTALL_FAIL_MARKER"
if VEETBOT_TEST_INSTALL_FAIL_ONCE=1 run_deploy >/dev/null 2>&1; then
  printf 'failed Nginx candidate install unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'return 202' "$AVAILABLE"
[[ "$(readlink "$ENABLED")" == "$AVAILABLE" ]]

printf 'server { return 203; }\n' >"$AVAILABLE"
printf 'server { return 505; }\n' >"$SOURCE_CONFIG"
rm -f -- "$SYMLINK_FAIL_MARKER"
if VEETBOT_TEST_SYMLINK_FAIL_ONCE=1 run_deploy >/dev/null 2>&1; then
  printf 'failed Nginx symlink activation unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'return 203' "$AVAILABLE"
[[ "$(readlink "$ENABLED")" == "$AVAILABLE" ]]

printf 'nginx deployment script tests passed\n'
