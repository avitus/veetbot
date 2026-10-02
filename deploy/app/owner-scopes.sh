#!/usr/bin/env bash
# Emit one non-secret EnvironmentFile assignment for this release's owner units.
set -Eeuo pipefail
export LC_ALL=C

template="${1:?production environment template is required}"
managed="$(awk '
  /^AUTH_SCOPES=/ { count++; value = substr($0, 13) }
  END { if (count != 1 || value == "") exit 1; print value }
' "$template")" || { printf 'owner scope template must contain one nonempty AUTH_SCOPES assignment\n' >&2; exit 1; }

# Never source the template, copy credentials, or allow shell/systemd syntax in
# the generated assignment. Application startup rejects unknown scopes.
scopes="$managed${AUTH_SCOPES:+,$AUTH_SCOPES}"
pattern='^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+(,[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+)*$'
[[ "$scopes" =~ $pattern ]] || { printf 'invalid owner scope list\n' >&2; exit 1; }
scopes="$(printf '%s\n' "$scopes" | tr ',' '\n' | sort -u | paste -sd, -)"
printf 'AUTH_SCOPES=%s\n' "$scopes"
