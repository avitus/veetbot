#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

BIN_DIR="$TEST_ROOT/bin"
DEPLOY_ROOT="$TEST_ROOT/opt/veetbot"
DOCS_ROOT="$DEPLOY_ROOT/shared/docs"
WEBSITE_ROOT="$DEPLOY_ROOT/shared/website"
PROCESS_ROOT="$TEST_ROOT/proc"
LOG_FILE="$TEST_ROOT/commands.log"
DOCKER_STATE="$TEST_ROOT/docker-production-image"
FAIL_MARKER="$TEST_ROOT/docs-switch-failed"
WEBSITE_FAIL_MARKER="$TEST_ROOT/website-switch-failed"
SERVICES_STOPPED="$TEST_ROOT/services-stopped"
ENV_FILE="$TEST_ROOT/veetbot.env"
RAW_RUNBOOK="$TEST_ROOT/manual-rollback.raw.sh"
RUNBOOK="$TEST_ROOT/manual-rollback.sh"
PREVIOUS_ID=20260810-152200-1111111
TARGET_ID=20260810-152233-abcdef0

mkdir -p \
  "$BIN_DIR" \
  "$DEPLOY_ROOT/releases/$PREVIOUS_ID" \
  "$DEPLOY_ROOT/releases/$TARGET_ID/.venv/bin" \
  "$DEPLOY_ROOT/releases/$TARGET_ID" \
  "$DEPLOY_ROOT/shared" \
  "$PROCESS_ROOT/4242" \
  "$DOCS_ROOT/releases/$PREVIOUS_ID" \
  "$DOCS_ROOT/releases/$TARGET_ID" \
  "$WEBSITE_ROOT/releases/$PREVIOUS_ID" \
  "$WEBSITE_ROOT/releases/$TARGET_ID"
printf 'VEETBOT_RELEASE_ID=%s\n' "$PREVIOUS_ID" \
  >"$DEPLOY_ROOT/releases/$PREVIOUS_ID/.release.env"
printf 'VEETBOT_RELEASE_ID=%s\n' "$TARGET_ID" \
  >"$DEPLOY_ROOT/releases/$TARGET_ID/.release.env"
printf '%s\n' \
  'POSTGRES_USER=agent' \
  'POSTGRES_DB=agent' \
  'COMPOSE_PROJECT_NAME=veetbot' >"$ENV_FILE"
printf '%s\n' "$PREVIOUS_ID" >"$DOCS_ROOT/releases/$PREVIOUS_ID/release.txt"
printf '%s\n' "$TARGET_ID" >"$DOCS_ROOT/releases/$TARGET_ID/release.txt"
printf '%s\n' "$PREVIOUS_ID" >"$WEBSITE_ROOT/releases/$PREVIOUS_ID/release.txt"
printf '%s\n' "$TARGET_ID" >"$WEBSITE_ROOT/releases/$TARGET_ID/release.txt"
touch \
  "$DOCS_ROOT/releases/$PREVIOUS_ID/index.html" \
  "$DOCS_ROOT/releases/$TARGET_ID/index.html"
touch \
  "$WEBSITE_ROOT/releases/$PREVIOUS_ID/index.html" \
  "$WEBSITE_ROOT/releases/$TARGET_ID/index.html"
ln -s "$DEPLOY_ROOT/releases/$PREVIOUS_ID" "$DEPLOY_ROOT/current"
ln -s "$DOCS_ROOT/releases/$PREVIOUS_ID" "$DOCS_ROOT/current"
ln -s "$WEBSITE_ROOT/releases/$PREVIOUS_ID" "$WEBSITE_ROOT/current"
ln -s "$DEPLOY_ROOT/releases/$TARGET_ID" "$PROCESS_ROOT/4242/cwd"
printf '%s\n' previous-image-id >"$DOCKER_STATE"
: >"$LOG_FILE"

write_stub() {
  local name="$1"
  shift
  printf '#!/usr/bin/env bash\nset -Eeuo pipefail\n%s\n' "$*" >"$BIN_DIR/$name"
  chmod +x "$BIN_DIR/$name"
}

write_stub flock 'exit 0'
write_stub timeout '
  printf "timeout %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  while (($#)); do
    case "$1" in
      --signal=* | --kill-after=*) shift ;;
      *s) shift; break ;;
      *) exit 1 ;;
    esac
  done
  "$@"
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
write_stub mv '
  if [[ "${1:-}" == -Tf ]]; then
    shift
    source="$1"
    target="$2"
    if [[ "${VEETBOT_TEST_FAIL_DOCS_SWITCH_ONCE:-0}" == 1 \
      && "$target" == "$VEETBOT_TEST_DOCS_CURRENT" \
      && ! -e "$VEETBOT_TEST_FAIL_MARKER" ]]; then
      : >"$VEETBOT_TEST_FAIL_MARKER"
      exit 1
    fi
    if [[ "${VEETBOT_TEST_FAIL_WEBSITE_SWITCH_ONCE:-0}" == 1 \
      && "$target" == "$VEETBOT_TEST_WEBSITE_CURRENT" \
      && ! -e "$VEETBOT_TEST_WEBSITE_FAIL_MARKER" ]]; then
      : >"$VEETBOT_TEST_WEBSITE_FAIL_MARKER"
      exit 1
    fi
    /bin/rm -f -- "$target"
    /bin/mv "$source" "$target"
  else
    /bin/mv "$@"
  fi
'
write_stub docker '
  printf "docker %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
    shift 2
    if [[ "${1:-}" == --format ]]; then
      [[ "${3:-}" == agent-core-sandbox:production ]]
      cat "$VEETBOT_TEST_DOCKER_STATE"
    else
      [[ "${1:-}" == "agent-core-sandbox:$VEETBOT_TEST_TARGET_ID" ]]
    fi
  elif [[ "${1:-}" == tag ]]; then
    source="$2"
    [[ "$3" == agent-core-sandbox:production ]]
    if [[ "$source" == "agent-core-sandbox:$VEETBOT_TEST_TARGET_ID" ]]; then
      printf "%s\n" target-image-id >"$VEETBOT_TEST_DOCKER_STATE"
    elif [[ "$source" == previous-image-id ]]; then
      printf "%s\n" previous-image-id >"$VEETBOT_TEST_DOCKER_STATE"
    else
      exit 1
    fi
  elif [[ "${1:-}" == compose ]]; then
    if [[ "${VEETBOT_TEST_REQUIRE_QUIESCED_QUERY:-0}" == 1 ]]; then
      [[ -f "$VEETBOT_TEST_SERVICES_STOPPED" ]]
    fi
    printf "%s\n" "${VEETBOT_TEST_INCOMPATIBLE_SCHEDULE_REVISIONS:-0}"
  else
    exit 1
  fi
'
write_stub sudo '
  printf "sudo %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  "$@"
'
write_stub systemctl '
  printf "systemctl %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  if [[ "${1:-}" == stop ]]; then
    : >"$VEETBOT_TEST_SERVICES_STOPPED"
  elif [[ "${1:-}" == restart ]]; then
    rm -f -- "$VEETBOT_TEST_SERVICES_STOPPED"
  elif [[ "${1:-}" == show ]]; then
    printf "4242\n"
  fi
'
write_stub curl '
  printf "curl %s\n" "$*" >>"$VEETBOT_TEST_LOG"
  headers=""
  while (($#)); do
    if [[ "$1" == --dump-header ]]; then
      headers="$2"
      shift 2
    else
      shift
    fi
  done
  [[ -n "$headers" ]]
  printf "HTTP/1.1 200 OK\r\nX-Veetbot-Release: %s\r\n\r\n" \
    "$VEETBOT_TEST_READY_RELEASE" >"$headers"
'
printf '#!/usr/bin/env bash\nset -Eeuo pipefail\nprintf "%%s\\n" "${VEETBOT_TEST_TARGET_CALENDAR_COMPATIBILITY:-incompatible}"\n' \
  >"$DEPLOY_ROOT/releases/$TARGET_ID/.venv/bin/python"
chmod +x "$DEPLOY_ROOT/releases/$TARGET_ID/.venv/bin/python"

awk '
  /^## Manual rollback$/ { in_section = 1; next }
  in_section && /^```bash$/ { in_block = 1; next }
  in_block && /^```$/ { exit }
  in_block { print }
' "$REPOSITORY_ROOT/docs/deployment.md" >"$RAW_RUNBOOK"
sed \
  -e "s|^target_id=.*|target_id=$TARGET_ID|" \
  -e "s|/opt/veetbot|$DEPLOY_ROOT|g" \
  -e "s|/proc|$PROCESS_ROOT|g" \
  "$RAW_RUNBOOK" >"$RUNBOOK"
chmod +x "$RUNBOOK"

run_rollback() {
  PATH="$BIN_DIR:$PATH" \
  BASH_ENV=/dev/null \
  VEETBOT_TEST_LOG="$LOG_FILE" \
  VEETBOT_TEST_DOCKER_STATE="$DOCKER_STATE" \
  VEETBOT_TEST_SERVICES_STOPPED="$SERVICES_STOPPED" \
  VEETBOT_TEST_TARGET_ID="$TARGET_ID" \
  VEETBOT_TEST_READY_RELEASE="${VEETBOT_TEST_READY_RELEASE:-$TARGET_ID}" \
  VEETBOT_TEST_DOCS_CURRENT="$DOCS_ROOT/current" \
  VEETBOT_TEST_FAIL_MARKER="$FAIL_MARKER" \
  VEETBOT_TEST_WEBSITE_CURRENT="$WEBSITE_ROOT/current" \
  VEETBOT_TEST_WEBSITE_FAIL_MARKER="$WEBSITE_FAIL_MARKER" \
  VEETBOT_ENV_FILE="$ENV_FILE" \
    "$RUNBOOK"
}

if VEETBOT_TEST_INCOMPATIBLE_SCHEDULE_REVISIONS=1 \
  VEETBOT_TEST_REQUIRE_QUIESCED_QUERY=1 \
  run_rollback >"$TEST_ROOT/incompatible-schedule.out" 2>&1; then
  printf 'rollback across incompatible schedule revisions unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(basename "$(readlink -f "$DEPLOY_ROOT/current")")" == "$PREVIOUS_ID" ]]
[[ "$(basename "$(readlink -f "$DOCS_ROOT/current")")" == "$PREVIOUS_ID" ]]
[[ "$(basename "$(readlink -f "$WEBSITE_ROOT/current")")" == "$PREVIOUS_ID" ]]
grep -Fq "docker compose --env-file $ENV_FILE" "$LOG_FILE"
grep -Fq 'systemctl stop veetbot-execution' "$LOG_FILE"
grep -Fq 'timeout --signal=TERM --kill-after=5s 20s docker compose' "$LOG_FILE"
grep -Fq -- '--env PGCONNECT_TIMEOUT=5' "$LOG_FILE"
grep -Fq -- '--env PGOPTIONS=-c statement_timeout=5000' "$LOG_FILE"
: >"$LOG_FILE"

if VEETBOT_TEST_FAIL_DOCS_SWITCH_ONCE=1 run_rollback \
  >"$TEST_ROOT/failed.out" 2>&1; then
  printf 'rollback with a failed documentation switch unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(basename "$(readlink -f "$DEPLOY_ROOT/current")")" == "$PREVIOUS_ID" ]] || {
  printf 'application pointer was not restored\n' >&2
  exit 1
}
[[ "$(basename "$(readlink -f "$DOCS_ROOT/current")")" == "$PREVIOUS_ID" ]] || {
  printf 'documentation pointer was not restored\n' >&2
  exit 1
}
[[ "$(cat "$DOCKER_STATE")" == previous-image-id ]] || {
  printf 'production image tag was not restored\n' >&2
  exit 1
}
grep -Fq 'docker tag previous-image-id agent-core-sandbox:production' "$LOG_FILE"

rm -f -- "$WEBSITE_FAIL_MARKER"
: >"$LOG_FILE"
if VEETBOT_TEST_FAIL_WEBSITE_SWITCH_ONCE=1 run_rollback \
  >"$TEST_ROOT/website-failed.out" 2>&1; then
  printf 'rollback with a failed website switch unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(basename "$(readlink -f "$DEPLOY_ROOT/current")")" == "$PREVIOUS_ID" ]] || {
  printf 'application pointer was not restored after website failure\n' >&2
  exit 1
}
[[ "$(basename "$(readlink -f "$DOCS_ROOT/current")")" == "$PREVIOUS_ID" ]] || {
  printf 'documentation pointer was not restored after website failure\n' >&2
  exit 1
}
[[ "$(basename "$(readlink -f "$WEBSITE_ROOT/current")")" == "$PREVIOUS_ID" ]] || {
  printf 'website pointer was not restored\n' >&2
  exit 1
}
[[ "$(cat "$DOCKER_STATE")" == previous-image-id ]]

rm -f -- "$FAIL_MARKER"
: >"$LOG_FILE"
if VEETBOT_TEST_READY_RELEASE=20260810-152244-bbbbbbb run_rollback \
  >"$TEST_ROOT/mismatched.out" 2>&1; then
  printf 'rollback with a mismatched readiness identity unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(basename "$(readlink -f "$DEPLOY_ROOT/current")")" == "$PREVIOUS_ID" ]]
[[ "$(basename "$(readlink -f "$DOCS_ROOT/current")")" == "$PREVIOUS_ID" ]]
[[ "$(basename "$(readlink -f "$WEBSITE_ROOT/current")")" == "$PREVIOUS_ID" ]]
[[ "$(cat "$DOCKER_STATE")" == previous-image-id ]]

: >"$LOG_FILE"
if VEETBOT_TEST_READY_RELEASE=20260810-152233-ABCDEF0 run_rollback \
  >"$TEST_ROOT/case-mismatched.out" 2>&1; then
  printf 'rollback with a case-mismatched readiness identity unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(basename "$(readlink -f "$DEPLOY_ROOT/current")")" == "$PREVIOUS_ID" ]]
[[ "$(basename "$(readlink -f "$DOCS_ROOT/current")")" == "$PREVIOUS_ID" ]]
[[ "$(basename "$(readlink -f "$WEBSITE_ROOT/current")")" == "$PREVIOUS_ID" ]]
[[ "$(cat "$DOCKER_STATE")" == previous-image-id ]]

: >"$LOG_FILE"
run_rollback
[[ "$(basename "$(readlink -f "$DEPLOY_ROOT/current")")" == "$TARGET_ID" ]]
[[ "$(basename "$(readlink -f "$DOCS_ROOT/current")")" == "$TARGET_ID" ]]
[[ "$(basename "$(readlink -f "$WEBSITE_ROOT/current")")" == "$TARGET_ID" ]]
[[ "$(cat "$DOCKER_STATE")" == target-image-id ]]
grep -Fq \
  'systemctl restart veetbot-execution veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api' \
  "$LOG_FILE"

printf 'manual rollback tests passed\n'
