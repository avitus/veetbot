# ADR-0047: A dependency-free zipapp for the first downloadable client

- Status: Proposed
- Date: 2026-08-10
- Related: Section 16 (HTTP API), Section 17 (CLI), Section 29 (multi-device
  shared core), ADR-0010 (live event transport), ADR-0011 (multi-device shared
  core), ADR-0028 (HTTP API and SSE consumer semantics), ADR-0034 (the deferred
  Device seam)
- User authorization: implement a simple downloadable client that talks to the
  API

## Context

The shared core and its fourteen-route HTTP API are complete, but there is no
separately distributable client. The existing `agent` console script is an
operator and development entry point: it installs the whole server distribution
and calls application services directly. Reusing it as a remote client would
couple a thin device to database, provider, worker, and sandbox dependencies and
would blur Section 17's service-level CLI contract with a transport client.

The first client needs to be easy to download, work on common development and
operator machines, preserve the API's SSE replay rules, and avoid creating the
Device, presence, pairing, or notification mechanisms that Section 29.8 defers.

## Proposed decision

1. **Distribute a separate terminal client as one Python zipapp.** Its source
   lives under `client/`, uses only the Python 3.12 standard library, and builds
   to `build/veetbot-client.pyz`. It is not imported by the server package.
2. **The client is transport-only.** It uses the public HTTP API and never
   imports server domain models, application services, the composition root, or
   adapters. The existing `agent` CLI remains the service-level operator CLI.
3. **No authoritative or secret state is persisted.** A session may be supplied
   explicitly, but the client stores neither session state nor bearer tokens.
   Environment input and a no-echo interactive prompt are the token sources.
4. **Remote bearer authentication requires HTTPS.** Plain HTTP is permitted
   only for loopback development. Redirects are refused so an authorization
   header cannot cross to another origin, and the standard TLS verifier remains
   enabled.
5. **Submission retries reuse one idempotency key.** Connection failures may be
   retried without creating a second run. A server conflict naming an active run
   attaches the client to that run rather than starting another.
6. **Only persisted SSE events advance replay.** Transient deltas carry no ID,
   reconnect uses the last persisted sequence, overflow reconnects from its
   stated watermark, and terminal text is reconciled against the durable
   completed message.
7. **Interactive suspension handling stays in the client.** Approval events call
   the approval routes, user questions call the input route, and tool activity
   is rendered from events. None of those paths inspects model responses or
   implements a runtime loop.
8. **CircleCI publishes the zipapp.** The existing static job builds it after
   checks and stores it as a job artifact. No second CI provider or release
   service is introduced.

## Consequences

- A user can download one small file and connect to either a loopback or deployed
  Veetbot API with no server installation.
- Python 3.12 remains a client prerequisite. A native GUI or self-contained
  operating-system binary would require a later packaging decision and platform
  build matrix.
- The client can resume known sessions and active runs but cannot list history,
  because the API intentionally has no session or run list route.
- Missed transient deltas may reduce animation fidelity, but durable final text
  remains exact after reconnect.
- The work adds no API route, database schema, principal field, scope, Device
  model, or device-local capability channel.

## Alternatives considered

- **Put remote HTTP behavior into `agent chat`:** rejected because that command
  is specified as a caller of application services and ships with the full
  server distribution.
- **Ship a browser-only HTML file:** rejected for the first release because a
  remote bearer client would require a new CORS security surface and browser
  credential-storage decisions.
- **Electron or Tauri desktop application:** deferred. Both add a second language
  toolchain, platform packaging, and substantially more release machinery before
  the transport behavior has user feedback.
- **PyInstaller native binaries:** deferred. It would remove the Python runtime
  prerequisite but add a large packaging dependency and per-platform builds.
- **Persist the last session and token:** rejected for the first release. Session
  caching is optional convenience; token persistence requires an operating-system
  credential-store design.
