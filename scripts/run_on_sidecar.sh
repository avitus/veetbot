#!/usr/bin/env bash
# Run one command on this checkout's Linux chunk sidecar, so work that would
# take over the owner's Mac desktop runs off it (ADR-0229). chunk syncs the
# checkout, reuses the active sidecar or creates one from the project
# snapshot, and returns the command's exit status.
set -euo pipefail

if (($# == 0)); then
  echo "usage: $0 <command>" >&2
  exit 2
fi
if ! command -v chunk >/dev/null 2>&1; then
  echo "$0 needs the chunk CLI and a sidecar; see docs/plan/development-toolchain.md." >&2
  exit 1
fi
# A new sidecar has no project environment until install runs; on one that
# has it, install is a quick no-op sync. chunk waits on stdin when backgrounded.
chunk validate --remote install </dev/null
exec chunk validate --remote --cmd "$*" </dev/null
