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
make test-apple
make test-apple-ui
```

`make test-apple` requires full Xcode and guarantees that Swift Testing suites
execute; it fails instead of accepting the Command Line Tools behavior that can
compile the bundle without running it. `make test-apple-ui` also requires full
Xcode, selects available iPhone and iPad simulators, and launches a debug-only,
in-process fixture to verify that historical rows open and switch conversations,
new-conversation rows open the chat surface, and selected transcripts render.
Both targets run in the required CircleCI Apple job.

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
keeps an already-unlocked token only in process memory for the current app
session, so ordinary API requests do not repeatedly reopen Keychain. It refuses
redirects, uses the operating system trust store, and maps `401` to
re-authentication while preserving `403` as an authorization failure.

Once a connection is configured, the application-delegate adaptor requests
notification authorization and registers with APNs. The client mints one
`client_device_id`, stores it in the nonsynchronizing Data Protection Keychain
beside the bearer credential, and posts the APNs token and build-derived sandbox
or production environment to `POST /v1/devices`. System registration runs again
on launch and Apple invokes the same upload path whenever it rotates the token.
Forgetting a connection attempts to revoke that server device first, but still
deletes the local bearer and clears the local connection if revocation fails.
A `404` from the feature-gated device surface marks notifications unavailable
without preventing the rest of the client from using an older server.

The tracked target selects platform-specific push entitlements: iOS declares
`aps-environment`, and macOS declares
`com.apple.developer.aps-environment`. Enabling the push capability for the
application identifier and regenerating provisioning profiles remain owner
actions in the Apple Developer portal. Debug and release simulator builds remain
unsigned-build compatible, and the debug UI-test fixture suppresses the
permission request.

Approval status uses the API's uppercase five-value wire vocabulary. A pending
approval remains actionable in its tool card with Approve once and Deny controls.

Settings use a compact header, a scrolling body, and a persistent action bar, so
connection actions remain visible as sections grow. Connection, Website Access,
Appearance, and Data & Privacy cards group controls by user intent. Configured macOS clients open
settings in a separate window that resizes horizontally and vertically and
restores its last frame; first-run setup remains embedded in the resizable main
window. The main window has a separate persisted frame, so its last size and
position are restored without interfering with the settings window. Appearance
preferences include app-wide text sizing and system, rounded,
serif, or monospaced typography. They are stored in device preferences and apply
immediately. System sizing preserves the platform's accessibility setting, while
the three explicit sizes use deterministic scales on both platforms. The
interface palette uses the app icon's turquoise, orange, and navy while retaining
semantic colors for errors, approvals, and tool risk.

Website Access lists the authenticated principal's browser profiles and lets the
user choose one `READY` profile for new conversations. Adding access sends only
the exact public-HTTPS origin and login-page URL to Veetbot, then presents a
separate Continue in web browser action for the server's five-minute, single-use
browser ceremony. A rejected system-browser handoff cancels the ceremony and
removes its unused profile; a ceremony-creation failure also rolls its partial
profile back. The user enters usernames,
passwords, passkeys, and MFA directly in that isolated browser surface; the app
has no website-credential fields and receives no keystrokes, cookies, storage
state, or provider material. It polls only the secret-free ceremony status.
The direct surface gives numbered focused-field instructions and identifies a
closed, reloaded, incomplete, or expired one-time link. The app exposes Start
over to remove that setup and obtain a fresh ceremony.
The selected opaque profile UUID is a device preference, is cleared when the
server connection changes or credentials are forgotten, and is included only
when the client creates a new session. The server revalidates ownership and
readiness before persisting that binding.

Data & Privacy displays the installed marketing version and build number. The
first build with recoverable Website Access is version 0.1.1 (2), so an older
installed binary can be identified without comparing source revisions.

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
boundary and represented by a compact activity indicator. A failed run renders
the API's public failure message inside the conversation together with its
stable reason and available step and attempt numbers; the header status is not
the only failure indication.

The sidebar mirrors the server's authoritative, paginated session index.
SwiftData stores that cache on iOS 17+/macOS 14+. The minimum supported OS
versions predate SwiftData, so iOS 15–16 and macOS 12–13 use an atomic
Application Support file behind the same store protocol. Both contain only
`session_id`, title, agent identity, timestamps, and the last known run ID. The
client follows pagination until the server returns no next cursor, rejects a
repeated cursor as an invalid response, and reconciles that complete index after
connecting, whenever it returns to the foreground, and every 30 seconds while
it remains open. The first top-level user message provides an optimistic local
title from its first non-empty text block; if that message has no non-empty text
block, the title remains unset. The server stores the authoritative normalized
title and recovers titles for older sessions from their first user-message
event. Moving to a new machine therefore does not turn established
conversations into `New conversation` rows.
Server sessions are inserted or refreshed and local rows absent from the
authoritative index are verified with scoped point reads under a bounded
concurrency limit before they are pruned. Those point reads prevent
activity-driven movement between keyset pages from looking like a deletion
without serializing a large history into one request per round trip. Confirmed
pruning also clears the process-local artifact cache.
Conversation activity, not selection, updates the server
ordering. Each row's activity timer shows seconds only during its first minute,
then uses minute-or-larger relative units.

In compact iPhone and iPad layouts, sidebar rows push an activating chat
destination before selecting a historical session or resetting to a new
conversation. On regular-width iPad layouts and macOS, where the split-view
detail is already visible, those rows activate the detail directly. This
adaptive navigation prevents a compact-width tap from mutating an unbound
sidebar value and prevents a regular-width selection from leaving the visible
detail stale.

A notification tap accepts only the closed, content-free `veetbot` payload
dictionary. If it names a session and run, the client restores the complete
durable transcript before attaching to that exact run. Approval and question
notifications then focus their corresponding card; a cold-launch tap waits for
the saved connection to install before following the same path. The client keeps
only transient navigation focus and never persists notification state; offline
recovery remains the server's `/v1/notifications` authority.

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
those fields are present. Conversation text renders Markdown headings, emphasis,
links, lists and task lists, block quotes, thematic rules, code blocks, and
tables. Wide code blocks and tables scroll horizontally rather than compressing
their contents past readability. Messages and tool calls retain their first-seen
event order as later status events update an existing tool card. Approval rule
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
