---
title: Apple Client
---

# Apple client

The native Apple client under `clients/apple/` is a SwiftUI application for iOS
15+ and macOS 12+. It consumes only the versioned HTTP API. It does not import
the Python server, run an agent loop, or create a second source of truth.

## Build and connect

Open `clients/apple/Veetbot.xcodeproj` with a full Xcode installation and build
the `Veetbot` target for an iOS or macOS destination. The target has no
third-party dependencies. The companion Swift package supports command-line
compilation and transport/reducer tests:

```bash
swift build --package-path clients/apple
swift test --package-path clients/apple
```

The connection screen accepts an HTTPS base URL and a static bearer token.
Plaintext HTTP, embedded URL credentials, queries, and fragments are rejected.
The base URL may be stored in preferences. The token is stored as a
device-local generic Keychain password and is never written to `UserDefaults`.
The app uses the local Data Protection Keychain under its team-signed
application identifier, with synchronization disabled. The tracked project does
not pin a development team; each developer must select their own team under the
target's Signing & Capabilities settings. A
macOS upgrade from the earlier file-based Keychain item is attempted without
displaying an authentication prompt; if its ad-hoc signature no longer has
access, the user must enter the token once in the signed build. The transport
refuses redirects, uses the operating system trust store, and maps `401` to
re-authentication while preserving `403` as an authorization failure.

Approval status uses the API's uppercase five-value wire vocabulary. A pending
approval remains actionable in its tool card with Approve once and Deny controls.

Settings use a scrollable, resizable surface with a persistent action bar, so
connection actions remain visible as new settings sections are added. Appearance
preferences include app-wide text sizing and system, rounded, serif, or
monospaced typography. They are stored in device preferences, apply immediately,
and preserve the system Dynamic Type setting by default. The interface palette
uses the app icon's turquoise, orange, and navy while retaining semantic colors
for errors, approvals, and tool risk.

## Runtime behavior

One submitted message creates one run. A stable idempotency key is reused across
connection retries. If the server reports `active_run_exists`, the UI attaches
to the named run and does not queue a second message. A waiting user's answer is
routed to run input with the displayed question identifier. Stop requests use
the run-cancel route. The native composer uses padded multiline input; Return
sends its contents, while Command-Return inserts a newline.

The SSE reader parses the response incrementally. A bounded byte-to-line
decoder preserves the empty lines that delimit SSE frames because
`URLSession.AsyncBytes.lines` omits those separators on the supported Apple
platforms. Only persisted session sequences advance the replay cursor;
transient deltas are best effort. It does not infer a gap from non-contiguous
sequence values. Disconnects and overflow reconnect with `Last-Event-ID`,
suspension keeps the logical stream alive, and only completed, failed, or
cancelled run events close it. Raw reasoning text is discarded at the reducer
boundary and represented by a compact activity indicator.

The sidebar mirrors the server's authoritative, paginated session index.
SwiftData stores that cache on iOS 17+/macOS 14+. The minimum supported OS
versions predate SwiftData, so iOS 15–16 and macOS 12–13 use an atomic
Application Support file behind the same store protocol. Both contain only
`session_id`, title, agent identity, timestamps, and the last known run ID. The
client follows pagination until the server returns no next cursor, rejects a
repeated cursor as an invalid response, and reconciles that complete index after
connecting, whenever it returns to the foreground, and every 30 seconds while
it remains open. Server
sessions are inserted or refreshed and local rows absent from the authoritative
index are verified with scoped point reads under a bounded concurrency limit
before they are pruned. Those point reads prevent activity-driven movement
between keyset pages from looking like a deletion without serializing a large
history into one request per round trip. Confirmed pruning also clears the
process-local artifact cache.
Conversation activity, not selection, updates the server
ordering. Each row's activity timer shows seconds only during its first minute,
then uses minute-or-larger relative units.

Deleting a row is an irreversible `Delete Everywhere` operation. The client
first asks the server to delete the session and its associated conversation
data, then removes the local history row and cached artifact bytes only after a
successful response. A session with an active run returns `409`; the user must
stop that run before deleting. The same principal may safely repeat a completed
delete. Starting deletion fences in-flight reconciliation, and a successfully
deleted identifier remains excluded from later stale responses. Other open
clients remove the row on foreground reconciliation or their next active-phase
poll. A server release that predates the history routes produces an explicit
server-upgrade message during reconciliation or deletion rather than the generic
unsupported-request response. Initial connection setup propagates that
compatibility failure, or a reauthentication response, back to the settings
form so it remains open instead of reporting a successful save.

## Agent activity and artifacts

The timeline includes generic, collapsible tool activity, approval checkpoints,
clarifying questions, working state, and artifact links. Tool presentation is
driven by effect and risk taxonomy rather than a per-tool icon table. Structured
sandbox and workspace results receive terminal and file-preview treatments when
those fields are present. Messages and tool calls retain their first-seen event
order as later status events update an existing tool card. Approval rule
internals are intentionally not shown.

Artifact metadata and bytes are fetched separately. The process-local content
cache sends `If-None-Match` and reuses bytes on `304`, retains at most 32 MiB,
and evicts least-recently-used values. It is cleared when the app leaves the
foreground, the connection changes, or credentials are forgotten; it never
writes artifact bytes to disk. Artifacts can be previewed or exported through
the operating system file picker.

The current public SSE contract exposes tool result content and trust but does
not expose every invocation's stored `structured_result`, effect classification,
or a general tool-detail route. The client therefore renders those richer
specializations when the event payload contains them and otherwise falls back
to the public content/trust view. The server additions for authoritative history
and deletion are limited to the session list and delete routes described by
ADR-0050; no richer tool-detail route was added.
