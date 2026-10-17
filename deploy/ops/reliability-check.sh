#!/bin/sh
# Explicitly enabled operational checks; no provider calls or action replay.
set -eu

case "${AGENT_RELIABILITY_MONITOR_ENABLED:-0}" in
    0) exit 0 ;;
    1) ;;
    *) echo 'AGENT_RELIABILITY_MONITOR_ENABLED must be 0 or 1.' >&2; exit 1 ;;
esac

exec .venv/bin/agent run reliability --check --notify --json \
    --hours "${AGENT_RELIABILITY_WINDOW_HOURS:-1}" \
    --minimum-outcomes "${AGENT_RELIABILITY_MINIMUM_OUTCOMES:-10}" \
    --failure-rate "${AGENT_RELIABILITY_FAILURE_RATE:-0.2}" \
    --internal-errors "${AGENT_RELIABILITY_INTERNAL_ERRORS:-3}" \
    --queue-seconds "${AGENT_RELIABILITY_QUEUE_SECONDS:-120}"
