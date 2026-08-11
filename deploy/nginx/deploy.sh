#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_CONFIG="${1:-}"
DEPLOY_ROOT="${VEETBOT_ROOT:-/opt/veetbot}"
AVAILABLE="${VEETBOT_NGINX_AVAILABLE:-/etc/nginx/sites-available/veetbot}"
ENABLED="${VEETBOT_NGINX_ENABLED:-/etc/nginx/sites-enabled/veetbot}"
BACKUP_DIR="${VEETBOT_NGINX_BACKUP_DIR:-/etc/nginx/veetbot-backups}"
NGINX_SERVICE="${VEETBOT_NGINX_SERVICE:-nginx}"
LOCK_WAIT_SECS="${VEETBOT_DEPLOY_LOCK_WAIT_SECS:-900}"
EXPECTED_RELEASE_ID="${VEETBOT_EXPECTED_RELEASE_ID:-}"
RELEASE_PATTERN='^[0-9]{8}-[0-9]{6}-[0-9a-f]{7,40}$'

fail() {
  printf 'nginx deployment failed: %s\n' "$*" >&2
  exit 1
}

[[ -n "$SOURCE_CONFIG" && -f "$SOURCE_CONFIG" ]] || fail \
  "pass the repository Nginx configuration as the first argument"
[[ "$DEPLOY_ROOT" = /* && "$DEPLOY_ROOT" != / ]] || fail \
  "VEETBOT_ROOT must be a non-root absolute path"
[[ "$AVAILABLE" = /* && "$AVAILABLE" != / ]] || fail \
  "VEETBOT_NGINX_AVAILABLE must be a non-root absolute path"
[[ "$ENABLED" = /* && "$ENABLED" != / ]] || fail \
  "VEETBOT_NGINX_ENABLED must be a non-root absolute path"
[[ "$BACKUP_DIR" = /* && "$BACKUP_DIR" != / ]] || fail \
  "VEETBOT_NGINX_BACKUP_DIR must be a non-root absolute path"
[[ "$LOCK_WAIT_SECS" =~ ^[1-9][0-9]*$ ]] || fail \
  "VEETBOT_DEPLOY_LOCK_WAIT_SECS must be a positive integer"
if [[ -n "$EXPECTED_RELEASE_ID" && ! "$EXPECTED_RELEASE_ID" =~ $RELEASE_PATTERN ]]; then
  fail "VEETBOT_EXPECTED_RELEASE_ID is malformed"
fi
[[ -d "$DEPLOY_ROOT/shared" ]] || fail \
  "deployment shared directory does not exist: $DEPLOY_ROOT/shared"

exec 9>"$DEPLOY_ROOT/shared/deploy.lock"
printf 'Waiting up to %ss for the Veetbot deployment lock...\n' "$LOCK_WAIT_SECS"
flock -w "$LOCK_WAIT_SECS" 9 || fail "timed out waiting for the deployment lock"

if [[ -n "$EXPECTED_RELEASE_ID" ]]; then
  ACTIVE_RELEASE_ENV="$DEPLOY_ROOT/current/.release.env"
  if [[ ! -f "$ACTIVE_RELEASE_ENV" ]]; then
    fail "active release identity is missing: $ACTIVE_RELEASE_ENV"
  fi
  if ! grep -Fqx "VEETBOT_RELEASE_ID=$EXPECTED_RELEASE_ID" "$ACTIVE_RELEASE_ENV"; then
    printf 'Skipping stale Nginx deployment for %s; a newer application release is active.\n' \
      "$EXPECTED_RELEASE_ID"
    exit 0
  fi
fi

sudo install -d -m 0755 "$(dirname "$AVAILABLE")" "$(dirname "$ENABLED")" "$BACKUP_DIR"
BACKUP=""
ENABLED_TARGET=""
if sudo test -f "$AVAILABLE"; then
  BACKUP="$BACKUP_DIR/veetbot.$(date -u +%Y%m%d-%H%M%S).$$.conf"
  sudo cp -a "$AVAILABLE" "$BACKUP"
fi
if sudo test -L "$ENABLED"; then
  ENABLED_TARGET="$(sudo readlink "$ENABLED")"
elif sudo test -e "$ENABLED"; then
  fail "refusing to replace a non-symlink enabled site: $ENABLED"
fi

rollback() {
  if [[ -n "$BACKUP" ]]; then
    sudo cp -a "$BACKUP" "$AVAILABLE"
  else
    sudo rm -f -- "$AVAILABLE"
  fi
  if [[ -n "$ENABLED_TARGET" ]]; then
    sudo ln -sfn "$ENABLED_TARGET" "$ENABLED"
  else
    sudo rm -f -- "$ENABLED"
  fi
}

sudo install -m 0644 "$SOURCE_CONFIG" "$AVAILABLE"
sudo ln -sfn "$AVAILABLE" "$ENABLED"

if ! sudo nginx -t; then
  rollback
  sudo nginx -t || true
  fail "nginx -t rejected the candidate; the previous configuration was restored"
fi

if ! sudo systemctl reload "$NGINX_SERVICE"; then
  rollback
  sudo nginx -t || true
  sudo systemctl reload "$NGINX_SERVICE" || true
  fail "Nginx reload failed; the previous configuration was restored"
fi

printf 'Nginx configuration installed, validated, and reloaded.\n'
if [[ -n "$BACKUP" ]]; then
  printf 'Previous configuration backup: %s\n' "$BACKUP"
fi
