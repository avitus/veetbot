---
title: Downloadable Client
---

# Downloadable client

The first Veetbot client is a small interactive terminal application distributed
as one executable Python zipapp. It talks only to the versioned HTTP API and
holds no authoritative agent state. Sessions, runs, approvals, events, memory,
and artifacts remain in the shared core.

The client requires Python 3.12 or newer but does not require this repository,
the server package, or any third-party Python dependency on the client machine.

## Build and download

Build the artifact locally with:

```bash
make client-build
```

The result is `build/veetbot-client.pyz`. CircleCI's static job runs the same
target and publishes `veetbot-client.pyz` as a downloadable job artifact.

Run the downloaded file directly on Unix-like systems or through Python on any
supported platform:

```bash
./veetbot-client.pyz
python veetbot-client.pyz
```

## Connect

The client connects to the API but does not start the server or worker. For a
fresh development checkout, prepare the database once:

```bash
cp .env.example .env
make db-up
make migrate
```

Then keep the worker and API running in separate terminals:

```bash
uv run agent worker --role worker
```

```bash
uv run agent api
```

The local development API at `http://127.0.0.1:8000` is the client default, so
no URL flag is required once those services are running:

```bash
python veetbot-client.pyz
```

Configure a deployed API and let the interactive client request its token
without echoing it:

```bash
export VEETBOT_API_URL=https://agent.example.com
python veetbot-client.pyz
```

The token has deliberately no command-line option, so it is not exposed in the
process list. If a token-mode API returns `401` and the client has an interactive
terminal, the client prompts without echoing and keeps the token in memory only.
For non-interactive use, a secret manager may supply `VEETBOT_API_TOKEN` in the
process environment. It never writes the token to disk. The client refuses a
bearer token over plain HTTP unless the API host is loopback. TLS verification
always uses the operating system's default trust store. The client never follows
HTTP redirects.
Remote text is stripped of terminal control sequences before display, including
ANSI color commands and OSC clipboard operations.

Resume an existing session or run one non-interactive prompt with:

```bash
python veetbot-client.pyz --session SESSION_ID
python veetbot-client.pyz --once "Summarize the current project state."
```

Inside the interactive client, `/new` creates a new session, `/session ID`
switches to an existing session, `/help` lists commands, and `/quit` exits.

## Runtime behavior

For each message the client generates one `Idempotency-Key` and reuses it if the
submission connection fails. It then watches the run's SSE stream. Persisted
event identifiers advance the reconnect cursor; transient token deltas never do.
After a disconnect or explicit `stream.overflow`, the client reconnects with the
last persisted identifier and replays durable history.

Token deltas are displayed for responsiveness, but the final transcript is
reconciled against `assistant.message.completed` or `run.completed`. A missed
transient delta therefore cannot truncate the durable answer. Tool activity is
rendered from tool events. Pending approvals are read through the approval API
and can be approved once or denied. `WAITING_FOR_USER` questions are answered
through the run-input endpoint. Artifact references in a final assistant message
are displayed as opaque IDs.

## Initial limitations

This release is deliberately a terminal client rather than a desktop GUI. It
does not persist sessions or credentials locally, list historical sessions or
runs, upload files, download artifact bytes, or implement device presence,
device-scoped tools, pairing, notifications, or offline-authoritative state.
Those omissions keep it within the existing fourteen-route API and the deferred
Device seam.
