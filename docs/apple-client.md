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
application identifier, with synchronization disabled. The project is
configured for the repository owner's development team; another developer must
select their own team under the target's Signing & Capabilities settings. A
macOS upgrade from the earlier file-based Keychain item is attempted without
displaying an authentication prompt; if its ad-hoc signature no longer has
access, the user must enter the token once in the signed build. The transport
refuses redirects, uses the operating system trust store, and maps `401` to
re-authentication while preserving `403` as an authorization failure.

Approval status uses the API's uppercase five-value wire vocabulary. A pending
approval remains actionable in its tool card with Approve once and Deny controls.

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

The sidebar is local because the API has no list-sessions route. SwiftData
stores the session-history index on iOS 17+/macOS 14+. The minimum supported OS
versions predate SwiftData, so iOS 15–16 and macOS 12–13 use an atomic
Application Support file behind the same store protocol. Both contain only
`session_id`, title, agent identity, timestamps, and the last known run ID; a
selected session is refreshed from the server without changing its position in
the list. Conversation activity, not selection, updates the history ordering.
Each row can be removed from the device-local history after confirmation. The
API has no session-delete route, so this action does not delete authoritative
server data.

## Agent activity and artifacts

The timeline includes generic, collapsible tool activity, approval checkpoints,
clarifying questions, working state, and artifact links. Tool presentation is
driven by effect and risk taxonomy rather than a per-tool icon table. Structured
sandbox and workspace results receive terminal and file-preview treatments when
those fields are present. Approval rule internals are intentionally not shown.

Artifact metadata and bytes are fetched separately. The in-memory content cache
sends `If-None-Match` and reuses bytes on `304`; artifacts can be previewed or
exported through the operating system file picker.

The current public SSE contract exposes tool result content and trust but does
not expose every invocation's stored `structured_result`, effect classification,
or a general tool-detail route. The client therefore renders those richer
specializations when the event payload contains them and otherwise falls back
to the public content/trust view. No server route was added for this client.
