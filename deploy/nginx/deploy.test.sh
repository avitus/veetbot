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
GNU_MV="${VEETBOT_TEST_GNU_MV:-$(command -v gmv || command -v mv)}"
if ! "$GNU_MV" --version 2>/dev/null | grep -Fq 'GNU coreutils'; then
  printf 'nginx deployment tests require GNU mv (install coreutils on macOS)\n' >&2
  exit 1
fi
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
  "$VEETBOT_TEST_GNU_MV" "$@"
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
  VEETBOT_TEST_GNU_MV="$GNU_MV" \
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

MISMATCHED_CHECKSUM="$TEST_ROOT/mismatched-checksum.sha256"
printf '%064d  %s\n' 0 "$(basename "$DOCS_ARCHIVE")" >"$MISMATCHED_CHECKSUM"
if VEETBOT_EXPECTED_RELEASE_ID="$next_release_id" run_deploy \
  "$SOURCE_CONFIG" "$DOCS_ARCHIVE" "$MISMATCHED_CHECKSUM" \
  >"$TEST_ROOT/mismatched-checksum.out" 2>&1; then
  printf 'documentation archive with a mismatched checksum unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fqx \
  'nginx deployment failed: documentation archive checksum verification failed' \
  "$TEST_ROOT/mismatched-checksum.out"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]

MISSING_RELEASE_DOCS_SOURCE="$TEST_ROOT/missing-release-docs-source"
MISSING_RELEASE_DOCS_ARCHIVE="$TEST_ROOT/missing-release-docs.tar.gz"
MISSING_RELEASE_DOCS_CHECKSUM="$MISSING_RELEASE_DOCS_ARCHIVE.sha256"
mkdir -p "$MISSING_RELEASE_DOCS_SOURCE"
printf '<h1>Missing release identity</h1>\n' \
  >"$MISSING_RELEASE_DOCS_SOURCE/index.html"
tar -czf "$MISSING_RELEASE_DOCS_ARCHIVE" -C "$MISSING_RELEASE_DOCS_SOURCE" .
(
  cd "$(dirname "$MISSING_RELEASE_DOCS_ARCHIVE")"
  sha256sum "$(basename "$MISSING_RELEASE_DOCS_ARCHIVE")" \
    >"$(basename "$MISSING_RELEASE_DOCS_CHECKSUM")"
)
if VEETBOT_EXPECTED_RELEASE_ID="$next_release_id" run_deploy \
  "$SOURCE_CONFIG" "$MISSING_RELEASE_DOCS_ARCHIVE" \
  "$MISSING_RELEASE_DOCS_CHECKSUM" >"$TEST_ROOT/missing-release.out" 2>&1; then
  printf 'documentation archive without release.txt unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fqx \
  'nginx deployment failed: documentation archive release.txt does not match expected release' \
  "$TEST_ROOT/missing-release.out"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]

MISMATCHED_RELEASE_DOCS_SOURCE="$TEST_ROOT/mismatched-release-docs-source"
MISMATCHED_RELEASE_DOCS_ARCHIVE="$TEST_ROOT/mismatched-release-docs.tar.gz"
MISMATCHED_RELEASE_DOCS_CHECKSUM="$MISMATCHED_RELEASE_DOCS_ARCHIVE.sha256"
mkdir -p "$MISMATCHED_RELEASE_DOCS_SOURCE"
printf '<h1>Mismatched release identity</h1>\n' \
  >"$MISMATCHED_RELEASE_DOCS_SOURCE/index.html"
printf '20260810-152255-cdef012\n' \
  >"$MISMATCHED_RELEASE_DOCS_SOURCE/release.txt"
tar -czf "$MISMATCHED_RELEASE_DOCS_ARCHIVE" \
  -C "$MISMATCHED_RELEASE_DOCS_SOURCE" .
(
  cd "$(dirname "$MISMATCHED_RELEASE_DOCS_ARCHIVE")"
  sha256sum "$(basename "$MISMATCHED_RELEASE_DOCS_ARCHIVE")" \
    >"$(basename "$MISMATCHED_RELEASE_DOCS_CHECKSUM")"
)
if VEETBOT_EXPECTED_RELEASE_ID="$next_release_id" run_deploy \
  "$SOURCE_CONFIG" "$MISMATCHED_RELEASE_DOCS_ARCHIVE" \
  "$MISMATCHED_RELEASE_DOCS_CHECKSUM" >"$TEST_ROOT/mismatched-release.out" 2>&1; then
  printf 'documentation archive with mismatched release.txt unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fqx \
  'nginx deployment failed: documentation archive release.txt does not match expected release' \
  "$TEST_ROOT/mismatched-release.out"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]

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
  >"$TEST_ROOT/unsafe-docs.out" 2>&1; then
  printf 'documentation archive with a symlink unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fqx \
  'nginx deployment failed: documentation archive contains a non-file entry' \
  "$TEST_ROOT/unsafe-docs.out"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]

ABSOLUTE_DOCS_SOURCE="$TEST_ROOT/absolute-entry"
ABSOLUTE_DOCS_ARCHIVE="$TEST_ROOT/absolute-docs.tar.gz"
ABSOLUTE_DOCS_CHECKSUM="$ABSOLUTE_DOCS_ARCHIVE.sha256"
printf 'absolute archive entry\n' >"$ABSOLUTE_DOCS_SOURCE"
tar -czPf "$ABSOLUTE_DOCS_ARCHIVE" "$ABSOLUTE_DOCS_SOURCE"
(
  cd "$(dirname "$ABSOLUTE_DOCS_ARCHIVE")"
  sha256sum "$(basename "$ABSOLUTE_DOCS_ARCHIVE")" \
    >"$(basename "$ABSOLUTE_DOCS_CHECKSUM")"
)
if VEETBOT_EXPECTED_RELEASE_ID="$next_release_id" run_deploy \
  "$SOURCE_CONFIG" "$ABSOLUTE_DOCS_ARCHIVE" "$ABSOLUTE_DOCS_CHECKSUM" \
  >"$TEST_ROOT/absolute-docs.out" 2>&1; then
  printf 'documentation archive with an absolute path unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq \
  'nginx deployment failed: documentation archive contains an unsafe path:' \
  "$TEST_ROOT/absolute-docs.out"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]

PARENT_DOCS_SOURCE="$TEST_ROOT/parent-entry"
PARENT_DOCS_WORK="$TEST_ROOT/parent-docs-work"
PARENT_DOCS_ARCHIVE="$TEST_ROOT/parent-docs.tar.gz"
PARENT_DOCS_CHECKSUM="$PARENT_DOCS_ARCHIVE.sha256"
printf 'parent archive entry\n' >"$PARENT_DOCS_SOURCE"
mkdir -p "$PARENT_DOCS_WORK"
(
  cd "$PARENT_DOCS_WORK"
  tar -czPf "$PARENT_DOCS_ARCHIVE" ../parent-entry
)
(
  cd "$(dirname "$PARENT_DOCS_ARCHIVE")"
  sha256sum "$(basename "$PARENT_DOCS_ARCHIVE")" \
    >"$(basename "$PARENT_DOCS_CHECKSUM")"
)
if VEETBOT_EXPECTED_RELEASE_ID="$next_release_id" run_deploy \
  "$SOURCE_CONFIG" "$PARENT_DOCS_ARCHIVE" "$PARENT_DOCS_CHECKSUM" \
  >"$TEST_ROOT/parent-docs.out" 2>&1; then
  printf 'documentation archive with a parent path unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq \
  'nginx deployment failed: documentation archive contains an unsafe path:' \
  "$TEST_ROOT/parent-docs.out"
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
rm -f -- "$SYSTEMCTL_FAIL_MARKER"
if VEETBOT_EXPECTED_RELEASE_ID="$next_release_id" \
  VEETBOT_TEST_SYSTEMCTL_FAIL_ONCE=1 run_deploy \
    "$SOURCE_CONFIG" "$NEXT_DOCS_ARCHIVE" "$NEXT_DOCS_CHECKSUM" \
    >/dev/null 2>&1; then
  printf 'failed Nginx reload published documentation unexpectedly\n' >&2
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

publish_retained_docs_release() {
  local release_id="$1"
  local application_release="$DEPLOY_ROOT/releases/$release_id"
  local docs_source="$TEST_ROOT/retained-$release_id-source"
  local docs_archive="$TEST_ROOT/retained-$release_id.tar.gz"
  local docs_checksum="$docs_archive.sha256"

  mkdir -p "$application_release" "$docs_source"
  printf 'VEETBOT_RELEASE_ID=%s\n' "$release_id" \
    >"$application_release/.release.env"
  ln -sfn "$application_release" "$DEPLOY_ROOT/current"
  printf '<h1>Retained documentation %s</h1>\n' "$release_id" \
    >"$docs_source/index.html"
  printf '%s\n' "$release_id" >"$docs_source/release.txt"
  tar -czf "$docs_archive" -C "$docs_source" .
  (
    cd "$(dirname "$docs_archive")"
    sha256sum "$(basename "$docs_archive")" >"$(basename "$docs_checksum")"
  )
  VEETBOT_EXPECTED_RELEASE_ID="$release_id" \
    VEETBOT_KEEP_DOCS_RELEASES=2 \
    run_deploy "$SOURCE_CONFIG" "$docs_archive" "$docs_checksum" >/dev/null
}

first_retained_release=20260810-152300-1111111
rollback_retained_release=20260810-152301-2222222
current_retained_release=20260810-152302-3333333
publish_retained_docs_release "$first_retained_release"
publish_retained_docs_release "$rollback_retained_release"
publish_retained_docs_release "$current_retained_release"
[[ "$(readlink "$DOCS_ROOT/current")" == \
  "$DOCS_ROOT/releases/$current_retained_release" ]]
[[ -d "$DOCS_ROOT/releases/$current_retained_release" ]]
[[ -d "$DOCS_ROOT/releases/$rollback_retained_release" ]]
[[ ! -e "$DOCS_ROOT/releases/$first_retained_release" ]]
[[ ! -e "$DOCS_ROOT/releases/20260810-152233-abcdef0" ]]
[[ "$(find "$DOCS_ROOT/releases" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" \
  == 2 ]]

printf 'nginx deployment script tests passed\n'
