#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_CONFIG="${1:-}"
DOCS_ARCHIVE="${2:-}"
DOCS_CHECKSUM="${3:-}"
WEBSITE_ARCHIVE="${4:-}"
WEBSITE_CHECKSUM="${5:-}"
DEPLOY_ROOT="${VEETBOT_ROOT:-/opt/veetbot}"
AVAILABLE="${VEETBOT_NGINX_AVAILABLE:-/etc/nginx/sites-available/veetbot}"
ENABLED="${VEETBOT_NGINX_ENABLED:-/etc/nginx/sites-enabled/veetbot}"
BACKUP_DIR="${VEETBOT_NGINX_BACKUP_DIR:-/etc/nginx/veetbot-backups}"
DOCS_ROOT="${VEETBOT_DOCS_ROOT:-$DEPLOY_ROOT/shared/docs}"
WEBSITE_ROOT="${VEETBOT_WEBSITE_ROOT:-$DEPLOY_ROOT/shared/website}"
NGINX_SERVICE="${VEETBOT_NGINX_SERVICE:-nginx}"
LOCK_WAIT_SECS="${VEETBOT_DEPLOY_LOCK_WAIT_SECS:-900}"
EXPECTED_RELEASE_ID="${VEETBOT_EXPECTED_RELEASE_ID:-}"
KEEP_DOCS_RELEASES="${VEETBOT_KEEP_DOCS_RELEASES:-5}"
KEEP_WEBSITE_RELEASES="${VEETBOT_KEEP_WEBSITE_RELEASES:-5}"
RELEASE_PATTERN='^[0-9]{8}-[0-9]{6}-[0-9a-f]{7,40}$'

fail() {
  printf 'nginx deployment failed: %s\n' "$*" >&2
  exit 1
}

release_identity_matches() {
  local release_file="$1"
  [[ -f "$release_file" ]] || return 1
  [[ "$(awk 'END { print NR }' "$release_file")" == 1 ]] || return 1
  [[ "$(<"$release_file")" == "$EXPECTED_RELEASE_ID" ]]
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
[[ "$DOCS_ROOT" = /* && "$DOCS_ROOT" != / ]] || fail \
  "VEETBOT_DOCS_ROOT must be a non-root absolute path"
[[ "$DOCS_ROOT" != "$DEPLOY_ROOT" ]] || fail \
  "VEETBOT_DOCS_ROOT must not replace VEETBOT_ROOT"
[[ "$WEBSITE_ROOT" = /* && "$WEBSITE_ROOT" != / ]] || fail \
  "VEETBOT_WEBSITE_ROOT must be a non-root absolute path"
[[ "$WEBSITE_ROOT" != "$DEPLOY_ROOT" ]] || fail \
  "VEETBOT_WEBSITE_ROOT must not replace VEETBOT_ROOT"
[[ "$WEBSITE_ROOT" != "$DOCS_ROOT" ]] || fail \
  "VEETBOT_WEBSITE_ROOT must differ from VEETBOT_DOCS_ROOT"
[[ "$LOCK_WAIT_SECS" =~ ^[1-9][0-9]*$ ]] || fail \
  "VEETBOT_DEPLOY_LOCK_WAIT_SECS must be a positive integer"
[[ "$KEEP_DOCS_RELEASES" =~ ^[1-9][0-9]*$ ]] || fail \
  "VEETBOT_KEEP_DOCS_RELEASES must be a positive integer"
[[ "$KEEP_WEBSITE_RELEASES" =~ ^[1-9][0-9]*$ ]] || fail \
  "VEETBOT_KEEP_WEBSITE_RELEASES must be a positive integer"
if [[ -n "$EXPECTED_RELEASE_ID" && ! "$EXPECTED_RELEASE_ID" =~ $RELEASE_PATTERN ]]; then
  fail "VEETBOT_EXPECTED_RELEASE_ID is malformed"
fi
if [[ -n "$DOCS_ARCHIVE" || -n "$DOCS_CHECKSUM" ]]; then
  [[ -n "$DOCS_ARCHIVE" && -f "$DOCS_ARCHIVE" ]] || fail \
    "pass the documentation archive as the second argument"
  [[ -n "$DOCS_CHECKSUM" && -f "$DOCS_CHECKSUM" ]] || fail \
    "pass the documentation checksum as the third argument"
  [[ -n "$EXPECTED_RELEASE_ID" ]] || fail \
    "VEETBOT_EXPECTED_RELEASE_ID is required when publishing documentation"
fi
if [[ -n "$WEBSITE_ARCHIVE" || -n "$WEBSITE_CHECKSUM" ]]; then
  [[ -n "$WEBSITE_ARCHIVE" && -f "$WEBSITE_ARCHIVE" ]] || fail \
    "pass the website archive as the fourth argument"
  [[ -n "$WEBSITE_CHECKSUM" && -f "$WEBSITE_CHECKSUM" ]] || fail \
    "pass the website checksum as the fifth argument"
  [[ -n "$EXPECTED_RELEASE_ID" ]] || fail \
    "VEETBOT_EXPECTED_RELEASE_ID is required when publishing the website"
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
    exit 3
  fi
fi

sudo install -d -m 0755 "$(dirname "$AVAILABLE")" "$(dirname "$ENABLED")" "$BACKUP_DIR"
BACKUP=""
ENABLED_TARGET=""
DOCS_PREVIOUS_TARGET=""
DOCS_PROMOTED=0
DOCS_STAGE=""
NEXT_DOCS_CURRENT=""
WEBSITE_PREVIOUS_TARGET=""
WEBSITE_PROMOTED=0
WEBSITE_STAGE=""
NEXT_WEBSITE_CURRENT=""
if sudo test -f "$AVAILABLE"; then
  BACKUP="$BACKUP_DIR/veetbot.$(date -u +%Y%m%d-%H%M%S).$$.conf"
  sudo cp -a "$AVAILABLE" "$BACKUP"
fi
if sudo test -L "$ENABLED"; then
  ENABLED_TARGET="$(sudo readlink "$ENABLED")"
elif sudo test -e "$ENABLED"; then
  fail "refusing to replace a non-symlink enabled site: $ENABLED"
fi
if [[ -n "$DOCS_ARCHIVE" ]]; then
  if [[ -L "$DOCS_ROOT/current" ]]; then
    DOCS_PREVIOUS_TARGET="$(readlink "$DOCS_ROOT/current")"
  elif [[ -e "$DOCS_ROOT/current" ]]; then
    fail "refusing to replace a non-symlink documentation release: $DOCS_ROOT/current"
  fi
fi
if [[ -n "$WEBSITE_ARCHIVE" ]]; then
  if [[ -L "$WEBSITE_ROOT/current" ]]; then
    WEBSITE_PREVIOUS_TARGET="$(readlink "$WEBSITE_ROOT/current")"
  elif [[ -e "$WEBSITE_ROOT/current" ]]; then
    fail "refusing to replace a non-symlink website release: $WEBSITE_ROOT/current"
  fi
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
  if (( DOCS_PROMOTED == 1 )); then
    if [[ -n "$DOCS_PREVIOUS_TARGET" ]]; then
      ln -sfn "$DOCS_PREVIOUS_TARGET" "$DOCS_ROOT/current"
    else
      rm -f -- "$DOCS_ROOT/current"
    fi
  fi
  if (( WEBSITE_PROMOTED == 1 )); then
    if [[ -n "$WEBSITE_PREVIOUS_TARGET" ]]; then
      ln -sfn "$WEBSITE_PREVIOUS_TARGET" "$WEBSITE_ROOT/current"
    else
      rm -f -- "$WEBSITE_ROOT/current"
    fi
  fi
}

ROLLBACK_PENDING=1
RELOAD_ATTEMPTED=0
rollback_on_exit() {
  local status=$?
  trap - EXIT
  if (( status != 0 && ROLLBACK_PENDING == 1 )); then
    set +e
    rollback
    if [[ -n "$DOCS_STAGE" ]]; then
      rm -rf -- "$DOCS_STAGE"
    fi
    if [[ -n "$NEXT_DOCS_CURRENT" ]]; then
      rm -f -- "$NEXT_DOCS_CURRENT"
    fi
    if [[ -n "$WEBSITE_STAGE" ]]; then
      rm -rf -- "$WEBSITE_STAGE"
    fi
    if [[ -n "$NEXT_WEBSITE_CURRENT" ]]; then
      rm -f -- "$NEXT_WEBSITE_CURRENT"
    fi
    sudo nginx -t || true
    if (( RELOAD_ATTEMPTED == 1 )); then
      sudo systemctl reload "$NGINX_SERVICE" || true
    fi
  fi
  exit "$status"
}
trap rollback_on_exit EXIT

if [[ -n "$DOCS_ARCHIVE" ]]; then
  [[ "$(awk 'END { print NR }' "$DOCS_CHECKSUM")" == 1 ]] || fail \
    "documentation checksum must contain exactly one entry"
  read -r EXPECTED_DIGEST EXPECTED_ARCHIVE EXTRA <"$DOCS_CHECKSUM" || fail \
    "documentation checksum is unreadable"
  [[ "$EXPECTED_DIGEST" =~ ^[0-9a-f]{64}$ ]] || fail \
    "documentation checksum must contain a lowercase SHA-256 digest"
  [[ "$EXPECTED_ARCHIVE" == "$(basename "$DOCS_ARCHIVE")" && -z "${EXTRA:-}" ]] || fail \
    "documentation checksum must name exactly $(basename "$DOCS_ARCHIVE")"
  ACTUAL_DIGEST="$(sha256sum "$DOCS_ARCHIVE" | awk '{print $1}')"
  [[ "$ACTUAL_DIGEST" == "$EXPECTED_DIGEST" ]] || fail \
    "documentation archive checksum verification failed"

  while IFS= read -r member; do
    normalized="${member#./}"
    if [[ "$normalized" == /* \
      || "$normalized" == ".." \
      || "$normalized" == ../* \
      || "$normalized" == */../* \
      || "$normalized" == */.. ]]; then
      fail "documentation archive contains an unsafe path: $member"
    fi
  done < <(tar -tzf "$DOCS_ARCHIVE")
  while IFS= read -r member_details; do
    member_type="${member_details:0:1}"
    if [[ "$member_type" != - && "$member_type" != d ]]; then
      fail "documentation archive contains a non-file entry"
    fi
  done < <(tar -tvzf "$DOCS_ARCHIVE")

  DOCS_RELEASE="$DOCS_ROOT/releases/$EXPECTED_RELEASE_ID"
  install -d -m 0755 "$DOCS_ROOT/releases"
  if [[ -e "$DOCS_RELEASE" ]]; then
    [[ -d "$DOCS_RELEASE" \
      && -f "$DOCS_RELEASE/index.html" \
      && -f "$DOCS_RELEASE/.artifact-sha256" \
      && "$(<"$DOCS_RELEASE/.artifact-sha256")" == "$EXPECTED_DIGEST" ]] \
      && release_identity_matches "$DOCS_RELEASE/release.txt" || fail \
      "existing documentation release is incomplete or has a different identity or checksum"
  else
    DOCS_STAGE="$DOCS_ROOT/.staging-$EXPECTED_RELEASE_ID-$$"
    mkdir -m 0755 "$DOCS_STAGE"
    tar --no-same-owner --no-same-permissions -xzf "$DOCS_ARCHIVE" -C "$DOCS_STAGE"
    [[ -f "$DOCS_STAGE/index.html" ]] || fail \
      "documentation archive does not contain index.html"
    release_identity_matches "$DOCS_STAGE/release.txt" || fail \
      "documentation archive release.txt does not match expected release"
    printf '%s\n' "$EXPECTED_DIGEST" >"$DOCS_STAGE/.artifact-sha256"
    mv "$DOCS_STAGE" "$DOCS_RELEASE"
    DOCS_STAGE=""
  fi

  NEXT_DOCS_CURRENT="$DOCS_ROOT/.current-$EXPECTED_RELEASE_ID-$$"
  ln -s "$DOCS_RELEASE" "$NEXT_DOCS_CURRENT"
  mv -Tf "$NEXT_DOCS_CURRENT" "$DOCS_ROOT/current"
  DOCS_PROMOTED=1
fi

if [[ -n "$WEBSITE_ARCHIVE" ]]; then
  [[ "$(awk 'END { print NR }' "$WEBSITE_CHECKSUM")" == 1 ]] || fail \
    "website checksum must contain exactly one entry"
  read -r EXPECTED_DIGEST EXPECTED_ARCHIVE EXTRA <"$WEBSITE_CHECKSUM" || fail \
    "website checksum is unreadable"
  [[ "$EXPECTED_DIGEST" =~ ^[0-9a-f]{64}$ ]] || fail \
    "website checksum must contain a lowercase SHA-256 digest"
  [[ "$EXPECTED_ARCHIVE" == "$(basename "$WEBSITE_ARCHIVE")" \
    && -z "${EXTRA:-}" ]] || fail \
    "website checksum must name exactly $(basename "$WEBSITE_ARCHIVE")"
  ACTUAL_DIGEST="$(sha256sum "$WEBSITE_ARCHIVE" | awk '{print $1}')"
  [[ "$ACTUAL_DIGEST" == "$EXPECTED_DIGEST" ]] || fail \
    "website archive checksum verification failed"

  while IFS= read -r member; do
    normalized="${member#./}"
    if [[ "$normalized" == /* \
      || "$normalized" == ".." \
      || "$normalized" == ../* \
      || "$normalized" == */../* \
      || "$normalized" == */.. ]]; then
      fail "website archive contains an unsafe path: $member"
    fi
  done < <(tar -tzf "$WEBSITE_ARCHIVE")
  while IFS= read -r member_details; do
    member_type="${member_details:0:1}"
    if [[ "$member_type" != - && "$member_type" != d ]]; then
      fail "website archive contains a non-file entry"
    fi
  done < <(tar -tvzf "$WEBSITE_ARCHIVE")

  WEBSITE_RELEASE="$WEBSITE_ROOT/releases/$EXPECTED_RELEASE_ID"
  install -d -m 0755 "$WEBSITE_ROOT/releases"
  if [[ -e "$WEBSITE_RELEASE" ]]; then
    [[ -d "$WEBSITE_RELEASE" \
      && -f "$WEBSITE_RELEASE/index.html" \
      && -f "$WEBSITE_RELEASE/privacy.html" \
      && -f "$WEBSITE_RELEASE/tos.html" \
      && -f "$WEBSITE_RELEASE/.artifact-sha256" \
      && "$(<"$WEBSITE_RELEASE/.artifact-sha256")" == "$EXPECTED_DIGEST" ]] \
      && release_identity_matches "$WEBSITE_RELEASE/release.txt" || fail \
      "existing website release is incomplete or has a different identity or checksum"
  else
    WEBSITE_STAGE="$WEBSITE_ROOT/.staging-$EXPECTED_RELEASE_ID-$$"
    mkdir -m 0755 "$WEBSITE_STAGE"
    tar --no-same-owner --no-same-permissions -xzf "$WEBSITE_ARCHIVE" \
      -C "$WEBSITE_STAGE"
    [[ -f "$WEBSITE_STAGE/index.html" ]] || fail \
      "website archive does not contain index.html"
    [[ -f "$WEBSITE_STAGE/privacy.html" ]] || fail \
      "website archive does not contain privacy.html"
    [[ -f "$WEBSITE_STAGE/tos.html" ]] || fail \
      "website archive does not contain tos.html"
    release_identity_matches "$WEBSITE_STAGE/release.txt" || fail \
      "website archive release.txt does not match expected release"
    printf '%s\n' "$EXPECTED_DIGEST" >"$WEBSITE_STAGE/.artifact-sha256"
    mv "$WEBSITE_STAGE" "$WEBSITE_RELEASE"
    WEBSITE_STAGE=""
  fi

  NEXT_WEBSITE_CURRENT="$WEBSITE_ROOT/.current-$EXPECTED_RELEASE_ID-$$"
  ln -s "$WEBSITE_RELEASE" "$NEXT_WEBSITE_CURRENT"
  mv -Tf "$NEXT_WEBSITE_CURRENT" "$WEBSITE_ROOT/current"
  WEBSITE_PROMOTED=1
fi

sudo install -m 0644 "$SOURCE_CONFIG" "$AVAILABLE"
sudo ln -sfn "$AVAILABLE" "$ENABLED"

if ! sudo nginx -t; then
  fail "nginx -t rejected the candidate; the previous configuration was restored"
fi

RELOAD_ATTEMPTED=1
if ! sudo systemctl reload "$NGINX_SERVICE"; then
  fail "Nginx reload failed; the previous configuration was restored"
fi
ROLLBACK_PENDING=0
trap - EXIT

if [[ -n "$DOCS_ARCHIVE" ]]; then
  retained=0
  pruned=0
  while IFS= read -r candidate; do
    candidate_name="$(basename "$candidate")"
    [[ "$candidate_name" =~ $RELEASE_PATTERN ]] || continue
    retained=$((retained + 1))
    if (( retained > KEEP_DOCS_RELEASES )) && [[ "$candidate" != "$DOCS_RELEASE" ]]; then
      rm -rf -- "$candidate"
      pruned=$((pruned + 1))
    fi
  done < <(find "$DOCS_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -print | sort -r)
fi
if [[ -n "$WEBSITE_ARCHIVE" ]]; then
  retained=0
  website_pruned=0
  while IFS= read -r candidate; do
    candidate_name="$(basename "$candidate")"
    [[ "$candidate_name" =~ $RELEASE_PATTERN ]] || continue
    retained=$((retained + 1))
    if (( retained > KEEP_WEBSITE_RELEASES )) \
      && [[ "$candidate" != "$WEBSITE_RELEASE" ]]; then
      rm -rf -- "$candidate"
      website_pruned=$((website_pruned + 1))
    fi
  done < <(find "$WEBSITE_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -print | sort -r)
fi

printf 'Nginx configuration installed, validated, and reloaded.\n'
if [[ -n "$DOCS_ARCHIVE" ]]; then
  printf 'Documentation release published: %s\n' "$DOCS_RELEASE"
  if (( pruned > 0 )); then
    printf 'Pruned %s superseded documentation release(s).\n' "$pruned"
  fi
fi
if [[ -n "$WEBSITE_ARCHIVE" ]]; then
  printf 'Website release published: %s\n' "$WEBSITE_RELEASE"
  if (( website_pruned > 0 )); then
    printf 'Pruned %s superseded website release(s).\n' "$website_pruned"
  fi
fi
if [[ -n "$BACKUP" ]]; then
  printf 'Previous configuration backup: %s\n' "$BACKUP"
fi
