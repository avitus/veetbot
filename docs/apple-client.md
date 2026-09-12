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
Xcode. It uses a debug-only hook to resize the real macOS SwiftUI window,
terminates the app, and verifies the relaunched window has the same size. It
also exercises the Mac Email mode and exact draft approval journey. It
then selects available iPhone and iPad simulators and launches a debug-only,
in-process fixture to verify that historical rows open and switch conversations,
new-conversation rows open the chat surface, and selected transcripts render.
Email journeys cover mode switching, feedback, editing, learning controls and
compact-trait navigation on both simulator families.
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

Settings use a compact header and a scrolling body. Connection, Website Access,
Appearance, and Data & Privacy cards group controls by user intent. The Connect
or Update Connection action sits inside the Connection card, while the footer is
limited to closing the configured settings surface. Configured macOS clients
open settings in a separate window that resizes horizontally and vertically and
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
user choose one `READY` profile for new conversations. Adding access sends
the exact public-HTTPS primary origin, optional `additionalOrigins` included
in `allowedOrigins`, and the login-page URL to Veetbot. Each additional value
must also be an exact public HTTPS origin. The client then presents a
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

## TestFlight delivery

The `apple-testflight` CircleCI job archives the generic macOS destination from
each tested `main` revision after the production API deploy succeeds. It uses
CircleCI's project-scoped pipeline number as the build number without editing
the tracked Xcode project, verifies the archived build number, bundle identifier,
and code signature, and uploads directly to App Store Connect. Xcode's automatic
build-number management is disabled during export so Apple receives the value
the job inspected. The signed archive is not retained as a CircleCI artifact.

The job installs the CircleCI-managed `veetbot-app-store` application-signing
bundle, imports the installer identity from the restricted
`veetbot-apple-signing` context into a job-owned temporary keychain, and uses
the separate restricted `veetbot-apple-testflight` context for App Store
Connect authentication. The setup procedure and exact context variables are in the
[production deployment guide](deployment.md#macos-testflight-delivery).
The first pipeline number used this way must be greater than the latest macOS
build already accepted by App Store Connect.

App Store Connect must assign accepted builds to the intended TestFlight group,
with automatic distribution enabled if every build should become available
without operator action. Each Mac must install Veetbot through TestFlight and
enable automatic updates there. Upload success means Xcode handed the build to
Apple successfully; later processing, group assignment, and installation remain
external states visible in App Store Connect and TestFlight.

## Runtime behavior

One submitted message creates one run. A stable idempotency key is reused across
connection retries. If the server reports `active_run_exists`, the UI attaches
to the named run and does not queue a second message. A waiting user's answer is
routed to run input with the displayed question identifier. Stop requests use
the run-cancel route. The native composer uses padded multiline input; Return
sends its contents, while Command-Return inserts a newline.

Submitting a top-level message inserts its user bubble before the first network
await. The persisted `user.message.created` event replaces that optimistic item
in place, preserving its position without rendering a duplicate. A submission
failure before server acceptance removes the optimistic item so the restored
composer remains the single retry surface.

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

Notification alerts explain the next action, include the tool name for approvals
and device actions when available, and distinguish schedule outcomes and skip
reasons. Schedule alerts also identify the title from the revision that ran and
its scheduled occurrence time, even if the schedule was renamed afterward
(ADR-0091). Conversation text, instructions, recipients, message bodies, run
results, and failure details are fetched after opening the app. Existing clients
can open the enriched alerts because their version-1 tap dictionary is unchanged.

On macOS, **System Settings → Notifications → Veetbot → Allow Notifications**
controls permission. On iOS, use **Settings → Notifications → Veetbot**.
The client checks existing authorization before asking, respects a denial
without showing a repeated application error, and still reports other push
registration failures. After turning permission on, relaunch Veetbot to register
for push delivery. A denied permission is separate from signing: a distributed
macOS build must also contain the `com.apple.developer.aps-environment`
entitlement, while iOS uses `aps-environment`.

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
tables. Every fenced or indented code block, including shell and CLI blocks, has
a labeled Copy action that writes the block's exact contents to the platform
clipboard and confirms the completed copy. Wide code blocks and tables scroll
horizontally rather than compressing their contents past readability. Messages
and tool calls retain their first-seen event order as later status events update
an existing tool card. Approval rule internals are intentionally not shown.

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

## Milestone 26 client modes

The approved [email experience](plan/email-experience.md) adds Chat and Email
presentation modules on iPhone, iPad and Mac, sharing the existing connection,
identity, agent, persona and memory. The mode shell preserves Chat streaming,
composer and navigation while email uses scoped server-owned projections and
versioned drafts. Foreground-only refresh, adaptive layouts, exact-send approval
and all existing transport-only client boundaries remain mandatory. Milestone
26's new native evidence is required independently of existing Apple gates.

`AppCoordinator` retains the Chat and Email view models and the mode shell keeps
both navigation trees mounted. Email uses the Chat connection's authenticated
transport. Its compact navigation opens a thread from the inbox; regular iPad
and Mac layouts show the list beside thread detail. The initial inbox requests
five important threads, with account filtering, search, Other mail, pagination,
per-account freshness and historical coverage. Background updates preserve row
order and announce newly qualifying mail. Opening Email or returning it to the
foreground starts refresh; the client admits a new refresh every sixty seconds
while Email remains visible, and stops admission when hidden or backgrounded.
Transient operation-status failures retry the same operation with bounded
backoff, stopping after three consecutive failures until the next active
refresh. Cancellation and responses from an old connection do not become
visible connection errors. Refresh failures survive successful cached reads
until a refresh completes; a recovered thread read clears its own error while
preserving errors from editing, feedback or send actions.
Accounts awaiting their first update show a pending message; the client shows
an account failure only when the server has recorded a refresh error.

The inbox keeps search, Important / Other mail and account filtering together
above the thread list. Rows distinguish correspondent, account, subject,
attention summary and reply state. A compact mailbox-status disclosure retains
per-account freshness; incomplete scans and account failures remain visible in
its collapsed label. Historical coverage lives in the scrollable Email learning
pane. Only the active mode contributes toolbar actions.
Mark handled remains available beside every inbox row and at the top of the
reading column; its confirmed state can be reversed with Mark unhandled.

The reading column separates the attention summary, original conversation and
reply composer. Why this matters and Improve priorities expand on demand, while
the persistent Your reply action reaches the editor without scrolling through
the conversation. From and To stay visible; Cc, Bcc and subject expand together
and initially open when copy recipients are present. Draft options collect save,
history, writing-example endorsement and discard. Review & Send remains the
primary composer action and opens a separate, scrollable exact-message review.
Native interaction coverage checks the reply shortcut, optional feedback,
recipient-field discovery and separation of Chat and Email toolbar actions in
light and dark appearance. Email uses adaptive surfaces and text accents while
filled action buttons retain contrasting text.

Thread detail renders text and attachment metadata without remote content.
Feedback distinguishes people, topics, thread importance and reply need, reports
the applied scope and judgment, and supports Undo. Learning controls expose
pause/resume, scoped resets and explicit source exclusion. Draft edits autosave
with optimistic revisions; conflicts preserve local and server versions, and
draft history can restore earlier text into the editor. The Writing style menu
can explicitly endorse the displayed wording as an example after saving any
edits; a save conflict prevents endorsement. The action binds the current draft
revision and preserves the learning pause state. The learning pane discloses
historical/Sent analysis, shared memories and style, and hosted-model processing.
Coverage counts describe threads retrieved, not completed semantic analysis.
Review & Send verifies
the approval's exact tool/account, provider thread, recipients, subject and body
against the frozen draft before presenting the existing approve-once action.
The client never invokes Gmail or substitutes a retry send after uncertainty.

Mail and unsaved edits stay in process memory; there is no durable offline mail
cache or authoritative offline write queue. A connection change, forgotten
credentials, reauthentication failure or denied email read scope clears Email
state. Unsupported servers show the Email capability as unavailable while Chat
remains usable. `EmailViewModelTests` covers conflict preservation, exact approval
matching, refresh admission, account isolation, cursor and expanded-list
handling, feedback scope and late-response isolation; native UI journeys cover
mode switching, keyboard editing, learning controls and explicit send review.

Run `uv run pytest tests/native/test_email_m26.py -q` for the three executable
native Email gate checks. This integration-marked bridge invokes real Swift
tests and focused XCTest journeys on Mac, iPhone and iPad, then verifies the
fresh xcresult pass counts with no skipped or expected failures. The iPad lane
also exercises compact navigation through a DEBUG-only launch-time size-class
override; ordinary launches retain the system's size class. Shared-state and
foreground checks also execute the backend's shared-profile/context and bounded
refresh-admission regressions. These checks need full Xcode and both simulator
families, but no database or live mailbox. An unavailable Apple lane is skipped
by pytest and remains an unpassed active gate in the gate report.
