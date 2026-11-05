#!/usr/bin/env bash
# Run one command in a local Linux container, so real Chromium draws to a
# private Xvfb display instead of the owner's Mac desktop (ADR-0236). The image
# is linux/amd64 to match the sidecar and hosted CI; Docker Desktop emulates it
# on Apple silicon. The checkout is mounted, so reports land in it.
set -euo pipefail

if (($# == 0)); then
  echo "usage: $0 <command>" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "$0 needs a running Docker; see docs/plan/development-toolchain.md." >&2
  exit 1
fi

root=$(git rev-parse --show-toplevel)
cd "$root"
image=veetbot-browser-test:local
# Only the lock inputs are sent as build context, so cached layers make this a
# quick no-op until uv.lock or the Dockerfile changes.
tar -cf - pyproject.toml uv.lock deploy/browser-test.Dockerfile \
  | docker build --quiet --platform linux/amd64 -f deploy/browser-test.Dockerfile -t "$image" - >/dev/null

# A worktree's .git names the shared git directory by absolute path; mount it
# read-only at that path so the gate report can record the revision.
git_common=$(cd "$(git rev-parse --git-common-dir)" && pwd)
exec docker run --rm --init --platform linux/amd64 --shm-size=2g \
  -v "$root:$root" -w "$root" \
  -v "$git_common:$git_common:ro" \
  -e HOST_OS=Linux \
  -e GIT_OPTIONAL_LOCKS=0 \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  "$image" sh -c "$*" </dev/null
