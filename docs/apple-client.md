---
title: Apple Client
---

# Apple client

The native Apple client under `clients/apple/` is a SwiftUI application for iOS
27+ and macOS 26+ (ADR-0211). It consumes only the versioned HTTP API. It does not import
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
`make test-apple-ui` runs the Mac cases inside a local Tart macOS VM with
`make test-apple-ui-macos-vm`, so they never take over your desktop, then
runs `make test-apple-ui-ios`. Set the VM up once with
`tart clone ghcr.io/cirruslabs/macos-tahoe-xcode:27 veetbot-macos-ui`.
CircleCI runs `make test-apple` and the Mac cases in
the required `apple` job, and the simulator cases in the required `apple-ios`
job at the same time.

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
A `browser.act` card instead names the action, the element and the page, marks
website text as such, and offers Allow once, Deny, and, when the server offers
it, Allow all actions for this task after a confirmation. The card displays the
server's site scope, limits and exceptions beside that choice. An active task permission shows
above the composer with its remaining actions and time and a Stop control
(ADR-0129).

Settings are A2 pages, one per group: Connection, Models, Website access,
Notifications, Appearance, iOS's SMS integration, and Data & privacy. On the
Mac and iPad a sidebar on deep paper lists the groups under "Settings" in the
serif face, the chosen row in the selected tint and Website access marked with
an amber dot while a login needs the owner; the iPhone lists the groups with a
short value beside each and pushes the chosen one. Each group is a card on
paper with hairline rows, fields in the `a2Field` well, the one primary action
as a filled capsule and the rest as
hairline or quiet capsules. Connection opens with the server, the token
("Saved in Keychain · verified 2 min ago", from the last authenticated session
list) and the status, and the token field has a show/hide control.
Notifications says whether this device may show them (with Allow
notifications or Open System Settings), then lists the devices the server
notifies from `GET /v1/devices`, this one first, each with Send test (a
content-free test notification) and, for other devices, Remove after a
confirmation, which revokes it; both need `device.write`, and a server without
the routes says so. Muting kinds per device has no update route and is not
offered. Appearance adds a System, Light or Dark theme, applied to every window.
Data & privacy adds Clear local cache, which asks first and then empties this
device's cached chat list and artifacts and reloads them from the server; chats
stay on the server. Website rows offer "Use for new chats".

First-run setup is Connect: step 1 is the Connection group under "Connect this
Mac (or iPhone, iPad) to your Veetbot." and a sentence on where the token is
kept. Once the server accepts the token, step 2 explains notifications and
asks Allow notifications or Not now (connecting raises no system prompt before
this choice; Not now leaves it to Settings), and step 3, "Ready.", summarises
the server, the token, notifications and websites before Open Chat. The Models card (ADR-0119) reads and saves
`/v1/settings/models` under `settings.read` and `settings.write`: a chat model
and reasoning effort, which apply to new chats and to the next message, and a
memory model and effort limited to the evaluated combinations the server
offers. A server without the resource hides the pickers. The Connect
or Update Connection action sits inside the Connection card, while the footer is
limited to closing the configured settings surface. Configured macOS clients
open settings in a separate window that resizes horizontally and vertically and
restores its last frame; first-run setup remains embedded in the resizable main
window. The main window has a separate persisted frame, so its last size and
position are restored without interfering with the settings window. Appearance
preferences choose a text size and a system, rounded, serif, or monospaced face.
They are stored in device preferences, apply immediately, and reach transcript
body text only (and the composer that writes it): sidebars, tables, toolbars,
docks and controls keep the system face at their role sizes. System sizing
preserves the platform's accessibility setting, while the three explicit sizes
scale the body role deterministically on both platforms. The face defaults to
System.

### Design tokens

The interface draws from one set of semantic colours in
`clients/apple/Veetbot/Views/AppTheme.swift`, the "A2 Dock" palette the owner
chose on MagicPath in October 2026. Each token carries a light value and a
night-green dark value and resolves with the view's appearance, so no screen
chooses a colour by mode. The tokens are:

| Token | Role | Light | Dark |
| --- | --- | --- | --- |
| `paper` | the page: transcript and reading surfaces | `#fcfaf6` | `#0c211d` |
| `paperDeep` | the sidebar and the mode bar | `#f3f0e8` | `#091a17` |
| `surface` | a card on the page | `#ffffff` | `#14302a` |
| `field` | a sunken input or segment | ink at 5 % | ink at 8 % |
| `ink`, `inkSoft`, `inkFaint` | text in three weights | `#102923`, `#2f4640`, `#566a64` | `#ece6d8`, `#bccbc5`, `#95a8a1` |
| `teal`, `tealFill`, `tealText`, `tealWash` | marks, filled buttons, links, the owner's capsule | `#0e8b78`, `#0b7a69`, `#086657`, teal at 10 % | `#36d5bb`, `#36d5bb`, `#5ee3c9`, teal at 12 % |
| `amber`, `amberText`, `amberLine`, `amberWash` | what needs the owner: a dot, a line of text, a card border, a wash | `#e0a23a`, `#7a4a0e`, `rgba(201,128,26,.55)`, `rgba(242,169,59,.12)` | `#f2a93b`, `#f2a93b`, `#f2a93b` at 50 %, `#f2a93b` at 10 % |
| `red` | failure | `#b4452e` | `#e07a62` |
| `hairline`, `hairlineStrong`, `selected` | dividers, borders, the chosen row | ink at 14 %, 28 %, 9 % | ink at 10 %, 22 %, 12 % |
| `toastBackground`, `toastText`, `toastAction` | a toast: ink ground, paper text, its Undo | `#102923`, `#fcfaf6`, `#5ee3c9` | `#ece6d8`, `#0c211d`, `#086657` |

Type follows design-system type revision 2 (`VeetType.swift`). A view names
one of seven roles with `.veetType(_:weight:)`, never a point size or a raw
text style, and only the 400, 500 and 600 weights exist; `VeetTypeTests` fails
on any view under `Views/` that sets a literal size, a raw text style or bold.

| Role | Use | Mac | iPad | iPhone |
| --- | --- | --- | --- | --- |
| `display` | conversation and page titles, serif | 24 | 26 (scales as Title) | Title |
| `title` | headings inside content and sheets, 600 | 17 | Title 3 | Title 3 |
| `body` | transcript prose and the composer | 15 | Body | Body |
| `ui` | sidebar rows, buttons, fields, segments | 13 | Subheadline | Body |
| `dense` | tables, header at 600 | 13 | Subheadline | Subheadline |
| `meta` | counts, times, tool lines, help text | 12 | Footnote | Footnote |
| `label` | section labels, 600 | 11 | Caption | Footnote |

The Mac sizes are fixed; the iPhone and iPad roles scale with Dynamic Type.
`.veetMono()` sets the work ledger and code in SF Mono at the meta size, and
`.veetIcon(_:)` sizes symbols to the row icon (15, 18 or 20 points).
`.transcriptBody()` marks the transcript, where the body role follows the
owner's face and size. Gaps come from one scale, `VeetSpace` (4, 8, 12, 16, 24,
32, 48), and row, control and gutter sizes from `A2Metrics`.

Every sidebar row uses `SidebarRow`: a fixed icon column, the title in the ui
role, and a trailing slot in the meta role, so chats, folders, schedules,
History and Needs you titles start at one x. Section labels align with the icon
column; New folder is a + beside the Folders label, and New chat a teal compose
button beside the sidebar's search, which filters rows by title. A
conversation's move and delete actions replace the trailing slot on hover and
sit in its context menu, where nothing hovers. Copy and Select Text show on
hover on the Mac and iPad (a tap shows them on an iPad without a pointer); the
iPhone keeps them visible, since a long-press menu would take the gesture text
selection uses (ADR-0122). A conversation opens at its latest turn, and its
title reaches the toolbar only once the display title has scrolled away.

Capsule buttons come in three weights (filled teal, hairline, quiet) in
`A2Controls.swift`.

A change the server has confirmed is announced with a toast
(`ToastCenter`, drawn by `A2Toast.swift`): one short sentence in an ink
capsule at the bottom centre of the window or sheet, read out by VoiceOver.
A confirmation leaves after 2.2 seconds. Where the server can reverse the
change, the toast offers Undo, stays six seconds, holds while the pointer or
keyboard focus rests on it, and on success says what was restored; Undo is
also a VoiceOver action on the toast. Only one toast shows at a time, in the
topmost window or sheet, and a newer one replaces it. A failure never toasts:
its error stays beside the control that failed. Toasts confirm memory review
and deletion, Email's Handled (with Undo, through the reversible attention
command), a merge or keep-separate answer to a possible duplicate, persona
saves, and accepted or declined persona suggestions.

Chat is the first surface drawn with these tokens: the sidebar on `paperDeep`
with a "Needs you" group ahead of Recent, folders and Scheduled; answers on bare
paper behind a small teal mark; the owner's messages in `tealWash` capsules; each
tool run one sentence that opens into a monospaced ledger; a pending approval or
clarifying question docked above a translucent composer while the transcript
shows one amber "Waiting for your approval" line. On a compact window the chat
list slides over the conversation from the leading edge. Email follows (see
"Email mode" below), then Memory, People and Schedules (see "Memory and
People" below), and Persona, Settings and Connect complete the set: the
persona's entries as a card of hairline rows of fields, a server conflict and
each nomination from memory in an amber-edged card with Affirm as the filled
capsule and Decline as a quiet one. With a server that has them (ADR-0226),
Persona uses a section sidebar on Mac and regular-width iPad, and a section
menu on compact screens: Voice, What Veetbot assumes, Nominations and
Boundaries. Empty Nominations and Boundaries pages explain that no items are
available. UI navigation checks wait for foreground and hittable controls, then
anchor native menu touches to the app window and require the menu to close.
Moving between sections preserves unsaved edits. A server without
tone support opens What Veetbot assumes instead of Voice. Voice contains
the Lean towards chips (Brief, Warm, Direct, Formal,
Playful; up to three, Formal and Playful replacing each other) and How that
sounds, an example reply the server generates from the unsaved voice and labels
"Example — generated, not saved" (or a labelled illustration of the chips when
it cannot); each entry under What Veetbot assumes has an on/off switch and says
when the memory it came from has changed; and Boundaries lists the active
rules the server reports, each enforced by a named policy rule or setting. `HairlineDivider`,
`FilterPillButtonStyle`, the `a2Field`, `a2List` and `a2Card` modifiers, and
the reading-page pieces `A2SectionHeading`, `A2KindLabel`, `A2RecordGroup`,
`A2Quote`, `A2EmptyState` and `A2SheetHeader` join the shared controls for the
surfaces that follow.

Website Access lists the authenticated principal's browser profiles and lets the
user choose one `READY` profile for new conversations. Adding access, or
signing in again to a profile that is not revoked, uses Sign in on this device
by default (ADR-0128). The private sign-in window places the instructions and
confirmation beside the website on Mac and regular-width iPad; compact screens
keep the explanation above the website and confirmation below it. The app opens
a `WKWebView` with a non-persistent data store, the platform's own user agent,
no injected script or message handler, and top-level navigation confined to
the profile's origins. The user types usernames, passwords, and one-time codes
into the website itself, and the app reads none of them. When the user taps
I'm signed in, the app creates the profile if it is new, begins a device
ceremony, and sends that website's cookies and `localStorage` once, directly
to the isolated browser service's handoff address, with the ceremony's
single-use capability and never with the Veetbot API credential. It then
clears the window's data. No cookie, storage value, capability, or confirmed
page address is persisted, logged, or kept in view state. The service filters
and verifies the session and decides the outcome. Passkeys and identity
providers on other origins do not work in that window. Every profile row that
is not revoked has Sign in again, which opens the same window for that
profile. For a new website, Use Veetbot's remote browser is also available
before a sign-in attempt and after a supported failure: it clears the window's
data and continues with the remote ceremony below. While any task permission
is active (ADR-0129), a compact Task
permissions row at the end of Website Access shows how many, and its menu
stops any one of them at once.

Use Veetbot's remote browser remains the alternative. It sends the exact
public-HTTPS primary origin, optional `additionalOrigins` included in
`allowedOrigins`, and the login-page URL to Veetbot. Each additional value
must also be an exact public HTTPS origin. The client then presents a
separate Continue in web browser action for the server's five-minute,
single-use browser ceremony. A rejected system-browser handoff cancels the
ceremony and removes its unused profile; a ceremony-creation failure also
rolls its partial profile back. In that mode the user enters usernames,
passwords, passkeys, and MFA directly in the isolated browser surface; the
app has no website-credential fields and receives no keystrokes, cookies,
storage state, or provider material. It reads only the secret-free ceremony
status, when the user asks and whenever the app becomes active or Website
Access appears.
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
sends its contents, while Command-Return inserts a newline. The composer floats
over the end of the transcript, which keeps the composer's measured height free
below its last row. While the owner reads at the end, a growing answer is
followed so its newest line stays above the composer; an answer taller than the
view stops with its beginning at the top, and scrolling away while it streams
leaves the view alone until the owner returns to the end. The same holds when
a conversation opens on such an answer or one arrives whole, as a scheduled
report does: the view shows its beginning below the toolbar, and keeps it there
while rows above it change height, until the owner scrolls.

Files can be attached to a message (ADR-0120): dropped on the conversation or
the message field on Mac and iPad, or chosen with the paperclip from Files on
every device and from Photos on iOS. Each file uploads to
`POST /v1/sessions/{id}/artifacts` as soon as it is added, with progress, retry
and removal, and a message may be attachments alone. Images other than small
PNG, GIF, and WebP files are re-encoded as JPEG with a 2000-pixel long edge,
which drops their location metadata. A server without the upload route marks
each file as not accepted, and an answer to a clarifying question stays text.

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
boundary and represented by a compact activity indicator. A reasoning summary
contributes only the heading of its latest part, which the indicator shows as
"Thinking: <heading>" until the answer starts (ADR-0144). A failed run renders
the API's public failure message inside the conversation together with its
stable reason and available step and attempt numbers.

The conversation's sidebar title is the window title on the Mac and the
navigation title on iOS, with no repeated heading in the pane. The server
writes a model-generated title a few seconds after a reply ends and changes it
only when the subject moves (ADR-0155), so the client reads the session list
once more ten seconds after a reply it watched ends, besides its 30-second
poll. Beneath it, a
status names only what the run needs from the owner: Needs your approval while
an unanswered approval is pending, and Needs your answer for a question. A run
still parked as waiting for approval with nothing left to decide, because an
answered approval is resuming or a delegated child is working, shows Working…
in the activity indicator instead. People and Stop sit in the window toolbar on
the Mac and in the navigation bar on iOS.

The sidebar mirrors the server's authoritative, paginated session index.
SwiftData stores that cache behind the store protocol; if its container cannot
be opened, the client logs the failure and keeps the cache in memory for that
launch, refilled from the index on connect (ADR-0211). The cache contains only
`session_id`, title, agent identity, timestamps, the last known run ID, the
server-assigned folder identifier, and a scheduled session's schedule
identifier. The
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
pruning also clears the process-local artifact cache. People audit and import
sessions are not conversations: the sidebar skips them and prunes cached copies
without deleting them on the server, because they carry People evidence.
Pruning a cached operational row preserves any focused approval or question the
owner has opened, including when its selection races with the history refresh.
Conversation activity, not selection, updates the server
ordering. Each row's activity timer shows seconds only during its first minute,
then uses minute-or-larger relative units.

On macOS, iPhone, and iPad, a collapsible **Recent chats** section immediately
below New conversation shows up to five conversations in descending activity
order, including conversations inside closed folders. Scheduled sessions are
excluded. These are shortcuts to the same conversations: their folder and
History rows remain in place, and selecting a shortcut does not change its
activity or folder. The section starts expanded, remembers the owner's toggle
on the device independently of folder solo mode, and works with older servers
that do not offer folders. An empty history shows no Recent chats section.

Conversation folders (Milestone 29, `thread-folders.md`) are server state the
sidebar mirrors, never a local organization. The index's `folder_id` is merged
into the cached row on every reconciliation and the server's value wins, nil
included, so a move made on another device lands on the next poll. A server
whose index carries the `folder_id` key is asked for its folders and open
proposals with the history; both are held in memory only. The sidebar then
renders suggested folders, each listing the conversations it would file one
per line, with accept, decline, and for a new folder a rename that accepts it
under the owner's name; then the folders in one section, each a row that
expands to its conversations and whose context menu (a right-click on Mac, a
long press on iPhone and iPad) renames or deletes it or switches solo mode;
then the unfiled history and a new-folder control; and every row gains a move
menu listing the folders. Which folders are expanded is remembered on the
device and changed only by the owner's own toggles, never by a proposal or a
refresh. In solo mode, the default, at most one folder is open and opening one
closes the other; without it every folder starts open. Create, rename, and
accepting under a new name use a sheet whose refused or duplicate name is
shown inline, never in the global error banner; deleting a folder uses the
same confirmation idiom as deleting a conversation and says that the
conversations return to history.
Unfiling sends an explicit null, so an omitted field can never unfile a
conversation. Every folder mutation — a move, a create, a rename, or a delete —
fences reconciliation when its answer arrives, and a move or a delete also when
it starts, so an index or folder page read while the mutation was in flight
cannot put the conversation back, revive the folder, restore the old name, or
drop the new one. Against a server whose index lacks the key, or that answers 404
or 405 on the folder list, the client makes no further folder request, keeps
every folder control hidden, and renders the history without folders; that unavailability
is contained in reconciliation and never surfaces as an error.

Scheduled sessions never enter a folder (ADR-0113, `scheduling.md`). The
index's `schedule_id` metadata is cached with each row, the server's value
winning, and a schedule with two or more cached sessions renders as one
collapsible row in a Scheduled section between the folders and the unfiled
history, labelled with its newest session's title and holding its sessions in
activity order. A lone scheduled session stays in the history. Groups start
collapsed; the owner's toggles are remembered on the device. Scheduled rows
carry no move menu, and the groups render whether or not the server offers
folders.

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

Approvals, questions, and scheduled outcomes wait thirty seconds before push
delivery, giving you time to handle them on another device. A loaded conversation
visible in the active Chat window suppresses its foreground alerts. Active clients
also clear delivered alerts once the server confirms they are obsolete, and
viewing a terminal result acknowledges it across devices. Dismissing the compact
conversation drawer also clears coverage from its removed sidebar sheets;
notification navigation can then acknowledge the visible report. Covered or background
conversations do not count as viewed. Sleeping devices clear obsolete alerts
when next active; a banner already shown cannot be recalled (ADR-0143).

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

Opening a conversation writes notice-level lines to the unified log under
subsystem `com.veetbot.apple`, category `conversation`: the selection, the
restored message and row counts, each superseded or failed load, the end of the
run replay, and, three seconds after selection, the rows held against the
transcript's laid-out content and viewport heights. They carry identifiers and
counts only, never conversation content. When a conversation opens blank, read
them on that Mac with
`/usr/bin/log show --last 1h --predicate 'subsystem == "com.veetbot.apple" AND category == "conversation"'`;
a settled line with rows but no content height means the transcript failed to
draw state it holds. That is how the blank weekday briefing was found: on
2026-10-19 it settled at two rows and 208 points of content in a 991-point
window, about a fifth of its real height, and drew nothing. The transcript is
therefore laid out eagerly rather than in a lazy stack.

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

Approved checkpoints collapse automatically to a summary showing the action and
whether it was approved once or allowed for the task. Selecting the summary
expands the original details for inspection without restoring decision controls.
This applies to generic and website approvals, inside tool cards and standalone
in chat, including approvals loaded from history. Pending requests remain open
until the server confirms approval; a failed resolution leaves them actionable.
While a decision is on its way, the card disables its controls and the chosen
one shows progress (Approving…, Allowing…, or Denying…), so a second tap sends
nothing.

Every finished message with text has Copy and Select Text (ADR-0122): labelled
under an answer, icon-only under the owner's own messages and at accessibility
text sizes. SwiftUI selection stops at each rendered paragraph, list item, and
table cell, so Copy writes the whole message at once, rendered from the same
Markdown parser as formatted RTF, semantic HTML, and plain text without Markdown
symbols, with no text color so a dark-mode copy never pastes white. Select Text
opens the message in a native read-only text view (`NSTextView`, `UITextView`)
where any range, or all of it, can be selected and copied with its formatting;
the view keeps a selection when the transcript redraws. A file the agent exported
arrives as a button under its answer and opens the artifact viewer, which on the
Mac opens at a reading width.

Adjacent successful calls of the same tool, including `web.search` and
`web.fetch`, collapse into one count-labelled activity bundle. Expanding it
preserves access to each call's arguments, result, and risk. The bundle shows
the highest individual risk. Fetch bundles also include rejected, unavailable,
and failed calls, with outcome counts in the summary and each call's status
and URL in its expanded row. An error result on a completed fetch is displayed
as failed. Messages, different tools, approvals, queued or running calls,
denials, and uncertain outcomes break the bundle. Other tools bundle only
successful completions.

Artifact metadata and bytes are fetched separately. The process-local content
cache sends `If-None-Match` and reuses bytes on `304`, retains at most 32 MiB,
and evicts least-recently-used values. It is cleared when the app leaves the
foreground, the connection changes, or credentials are forgotten; it never
writes artifact bytes to disk. Artifacts can be previewed or exported through
the operating system file picker. The Mac viewer uses a single-pane sheet with
Close and Download controls; loading a file does not rebuild navigation columns.
Cancelling the save picker keeps the viewer open, and saving preserves the
original bytes, including SVG files whose preview cannot be decoded. Markdown
artifacts use the same bounded native reading renderer as Chat, including
headings and tables; other text remains literal, and HTML is never executed.
The macOS UI regression loads an SVG from a reply, cancels a download, then saves it and
compares the exported bytes.

### The Mac right panel

On the Mac each mode has a right panel (ADR-0237; MagicPath merry-moon-8243 and
clever-village-5022), a column on `paperDeep` behind a hairline the owner can
drag between 260 and 440 points. One toolbar button, contributed only by the
active mode, shows or hides it (⌥⌘I), and the panel has its own hide button.
Each mode's width and the owner's open-or-closed choice are kept on this Mac
(`veetbot.inspector.<mode>.width`, `.open`). A window narrower than 1,100
points starts with Chat's panel collapsed, and narrower than 1,300 points
Email's, whose inbox and thread already take three columns; crossing the
threshold collapses or restores it without recording a choice.

Chat's panel has two tabs. **Activity** holds a card naming what the run is
doing (running, waiting for an approval or an answer, finished, failed or
stopped) with its call count, the working notes, and every tool call the
conversation holds, grouped under the owner's request that started them and
numbered across the conversation. Each call exposes the result content and
trust supplied by its events, with arguments and risk when the payload includes
those invocation details. **Artifacts** lists every file or image found in the
conversation's tool results and messages, each once with the call that made it,
and previews the chosen one in place with Download. There is no session
artifact list on the server, so the list is as complete as the loaded
conversation. On the Mac the transcript keeps its one-line work strips, which
no longer expand in place: choosing one opens the panel at its group and tints
it, and a file chip opens its preview in Artifacts. The working notes leave the
top of the transcript. Approvals and clarifying questions stay docked above the
composer. iPhone and iPad keep the inline strips, the working-state disclosure
and the viewer sheet.

The current public SSE contract exposes tool result content and trust but does
not expose every invocation's stored `structured_result`, effect classification,
or a general tool-detail route. The client therefore renders those richer
specializations when the event payload contains them and otherwise falls back
to the public content/trust view. The server additions for authoritative history
and deletion are limited to the session list and delete routes described by
ADR-0050; no richer tool-detail route was added.

## Memory and People

Memory opens from the toolbar (on iOS, from the More menu) with two
collections; the server mounts its routes when `AGENT_MEMORY_API_ENABLED=1`.
**Memories** lists the principal's governed beliefs with status and
type filters and a flag toggle that shows only memories needing review; every
email-derived memory arrives flagged. On the Mac the sheet has three columns:
a group sidebar on deep paper (Review: Needs review; Browse: All memories, each
belief type and the People directory; Maintenance: Synthesis), the list, and
the detail; it replaces the collection picker and the review and type
filters, and each memory group shows its number from the counts route
(ADR-0224); an older server leaves the numbers off. A flagged memory asks "Is
this right?" in an amber-edged card with Looks right (the `dismiss` outcome) as
the filled capsule and Fix the wording, Not true and Not relevant here as quiet
ones; the Review menu offers the same with Edit wording for any live memory and
Delete memory, and a row can be swiped to delete after a confirmation (ADR-0117,
under `memory.write`). Fix the wording and Edit wording open one sheet: saving
supersedes the memory with the owner's words through the `changed` review
outcome, the detail then shows the new memory, and the earlier wording stays on
the server as history and does not form again (ADR-0224). Under "Where this came
from" the detail lists up to five cited sources: the owner's own words as a
quote, and an email, text, call, schedule or tool source by where it came from,
never its text. A deleted statement does not form again; its original messages
stay at their source.

**People** (Milestone 28, under `people.read` and `people.write`) lists the
people the owner knows or writes to (ADR-0121) in the A2 sections: Needs you
(the server's review filter: people known only by a name or role), Recent (the
server's newest-interaction order), Everyone and Pinned, then Possible
duplicates (each pair answered with Merge or Keep separate, ADR-0125) and All
identities. The collection picker shows each number from `GET /v1/people/counts`
(ADR-0227); an older server leaves them off. Needs you also lists the replies
the owner owes someone that are overdue or due within fourteen days, earliest
first: such a row reads "Needs you · Reply about the lease · due Friday" with
Done and Not needed beneath it, which send the existing commitment correction;
a person waiting to be identified reads "Needs you · Who is this?". Recent rows
say when the last contact was. A person's profile leads with Ask Veetbot about the
person, then Email and Call when a current address or number is known: each
opens Chat with a request naming the person, so sending keeps its confirmation
and a call its card (a number from People never counts as the owner's own,
ADR-0213). It shows names and contact details, relationships, a history
timeline that includes email summaries (ADR-0126), open threads, and facts
with Inspect evidence and correction actions; Manage person holds Merge with
another entry (a previewed merge), Repair identity and Forget person. Import history analyses selected chats or mail accounts under a
date range, record limit and spending cap; see
[People operations](people-operations.md).

Memory, People and Schedules draw with the A2 tokens. Each list sits on paper
with hairline rows. A memory row leads with its kind in small capitals and the
day it last changed, then the statement and where it came from, with "Needs
review" in amber; the People directory shows initials on a teal wash beside
each name and, on the Mac, marks the chosen person with the selected tint; a
schedule row leads with a calendar glyph, the name beside its state, the
instruction's preview and the cadence with the next run. A memory or a
schedule opens as a reading page: the statement, or the schedule's name on the
Mac, in the serif face, then its record as titled groups of key-value lines on
surface cards, the schedule's instruction on a deep-paper block, and a flagged
memory's reason in an amber-edged card. Mac and regular-width iPad keep the
Memory collection sidebar, records and selected record in three columns. The
People sheet likewise keeps its Directory and Maintenance navigation beside
the people list and selected profile, with server counts beside each collection.
Compact screens push records from the list. People also uses that full-width
navigation at accessibility text sizes on iPad, so its labels remain readable.
In that layout, search stays above the list and large-text actions stack vertically.
The sidebar’s merge guidance uses medium-weight ink for legibility on 1x Mac displays.
For the three known iOS directory contrast findings, ADR-0235 requires measured
screenshot contrast of at least 4.5:1 against the expected semantic palette.
The audit retains the finding and measurement; missing evidence and all other
findings still fail.
Schedules groups its index by state,
keeps New schedule above it, and shows the selected schedule beside it on Mac
and regular-width iPad. The synthesis journal keeps its list
with teal text for its actions. A person's profile keeps its sections and
actions: Ask Veetbot is the filled capsule, Email, Call and Pin hairline
capsules, Rename quiet; Manage person holds Merge with another entry, Repair
identity and Forget person. The Review
menu, swipe-to-delete and every People journey keep their controls.

Schedules (ADR-0225) can be created and run from the app. New schedule opens a
sheet where the owner describes the work in plain words; once typing pauses,
the server reads it (`POST /v1/schedules/drafts`) and the sheet shows "Veetbot
understood: <cadence> · first run <time>" before anything is saved, and Create
posts the server's own definition with an idempotency key. A schedule's page
offers Run now (a one-off run that leaves the cadence alone, under
`schedule.run`), Pause or Resume (Pause's toast offers Undo), and Stop behind an
inline confirmation, which cancels future runs and keeps the record in Recent
History for thirty days; it is never called Delete. A Delivery group holds
"Only notify when something needs you" and the per-run spending limit, and
Recent runs lists the latest occurrences with their outcome and Open report in
Chat, which closes the sheet and opens that conversation.

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
and Mac layouts keep a mailbox sidebar beside the list and thread detail.
Inbox is the initial collection; Needs you and Handled are distinct server
collections (ADR-0231). Counts cover the filtered collection before pagination;
older servers omit them. Same-query background reads retain the last confirmed
counts while loading or after a transient failure; a changed query clears them.
Account filtering, search, Important / Other mail,
pagination, per-account freshness and historical coverage remain available.
Rows read newest first by each
conversation's latest activity; background updates keep the rows on screen,
slide arrivals into place and move a conversation with new mail up (ADR-0234).
Returning to Email refreshes the loaded rows in place rather than clearing them. Opening Email or returning it to the
foreground starts refresh; the client admits a new refresh every sixty seconds
while Email remains visible, and stops admission when hidden or backgrounded.
Transient operation-status failures retry the same operation with bounded
backoff, stopping after three consecutive failures until the next active
refresh. Cancellation and responses from an old connection do not become
visible connection errors. Refresh failures survive successful cached reads
until a refresh completes; a recovered thread read clears its own error while
preserving errors from editing, feedback or send actions.
Messages appear as soon as the thread response arrives, even when an older
server requires a separate draft read. Foreground polling reuses a pending initial
thread read from the same activation instead of replacing it with another request.
Accounts awaiting their first update show a pending message; the client shows
an account failure only when the server has recorded a refresh error.

The wide Email sidebar contains account and search controls, New chat, the
Chat / Email switch, pending chat attention, and Mailbox navigation for Inbox,
Needs you, Handled and Subscriptions. Its bottom row opens Chat, Memory, People,
Schedules, Persona and Settings. The main page has an Email heading, count and
appearance toggle. Compact screens put the mailbox collections in horizontal
filter pills. Important / Other mail remains available under Find other mail.
Mac uses a native draggable split divider: the inbox can grow from 280 to 560
points while retaining at least 250 points for reading in a small window.
Its width, selected thread and loaded rows survive Chat / Email switching, and
the two modes cross-fade. The width the
owner drags the divider to is kept on this Mac (`veetbot.email.inboxWidth`) and
restored at the next launch; a window too narrow for it squeezes the inbox
without replacing the remembered width.
Rows follow the A2 Email reference: a Handled circle leads each row (empty
while the conversation is in the Inbox, a filled teal check once Gmail has
archived it, the same reversible label change under the same Archive in Gmail
name), then the correspondent's name in semibold (the address only when the
sender gave no name, each person once) with a recency label in faint ink (the
time today, Yesterday, the weekday within the week, then the date), the
subject, Veetbot's one-line gist, and a short state chip: an attention state
("Needs reply", "For your attention", "Check send status") on an amber wash, a
draft state on teal, and a handled, archived or reported conversation in soft
ink with no wash. The reason for inclusion is read in the conversation, not the
row (ADR-0234). The account label appears only when more than one account is
connected. On the Mac the selected row takes the `selected`
tint with no border; on touch the rows run full width above hairlines. The
Important / Other mail control is a segmented control on the Mac and filter
pills on touch. Full text stays available in the reading column. The five
initial priorities fit without scrolling in a 1200 by 900 Mac window at the
default text size. Larger accessibility text can expand rows naturally.

Thread detail opens with the subject in the display face and "sender · date"
under it, with the Archive in Gmail capsule and the thread menu on the line
above. On a compact iPhone, where the thread is pushed, Archive (as an icon)
and the thread menu join the back button in a paper navigation bar instead,
so nothing scrolls beneath the back button. Veetbot's gist sits behind a two-point teal rule with the state line and
its reason; the messages follow as prose, each under its sender's line and
parted by hairlines, with View in People and Message details kept beside the
sender. On the Mac, View in People opens the sender's card in the right panel's
**Context** tab (ADR-0237), which also says why the thread is in the priority
list (its level and reason), lists the attachments without opening them, shows
the draft's state with a way to ask for one, and holds Teach Veetbot; its
**Activity** tab replays the draft run's events and shows the calls that made
the draft. Elsewhere a "Teach Veetbot" card on `paperDeep` below the
conversation opens into the same scoped
feedback (thread, person, kind of content; Important / Less important; reply
need) and reports what was applied with Undo. Topic feedback uses selectable
chips and still requires an explicit topic. The thread menu offers Mark handled
and Mark not handled separately from Archive in Gmail. The reply opens with "Your reply"
in the display face and its save state at the trailing edge. From, To and the
folded Cc, Bcc and subject are hairline-parted rows above the body in one
surface well behind a strong hairline, whose shapes take no clicks. Anything
the draft waits on (new mail in the thread, an unknown send status, a pending
approval, an edit from another device) is one amber-ruled card that names the
decision with its action beside it; a save refused because another device
edited the draft shows only that card, never the refusal as a second error.
Review & Send is the filled capsule; Draft
options is a quiet icon menu at the trailing edge, and Refine with Veetbot
folds its field and button into one row.
The bottom bar keeps Discuss in Chat and the reply shortcut on the material
edge. The Email learning, Draft history, Review & Send and unsubscribe sheets
share `EmailSheetHeader` (a display-face title and a quiet way out); the
Subscriptions census takes the same row and capsule styles.
A compact mailbox-status disclosure retains
per-account freshness; incomplete scans and account failures remain visible in
its collapsed label. Archive guidance lives inside that disclosure and each
checkbox retains its explicit Archive in Gmail label and help. Historical
coverage lives in the scrollable Email learning
pane. Only the active mode contributes toolbar actions. The shared root owns
the Mac window toolbar, independently of the mounted Chat and Email panes.
It presents the active mode's controls in the trailing area so Settings, Memory,
Persona, and Schedules remain visible independently of the sidebar width.
The mounted navigation panes fit the available window height so mode controls
remain reachable in the default Mac window as well as larger windows.
Archive in Gmail is available beside each inbox row and in thread detail on
supported accounts. Checking it removes the row immediately while the server
archives the conversation asynchronously. Normal progress and success stay
silent; a failure or uncertain outcome restores the row with an error. The
client retains confirmed mailbox state, row order and unsaved replies, and
keeps other rows actionable. Pending rows stay hidden through refreshes and
status checks resume when Email reopens. An archived conversation found in
Other mail can be moved back to its originating account's Inbox.
Archiving the open conversation, from its inbox row or the reading pane, clears
the pane immediately and opens the next visible conversation, or the previous one
when archiving the last row. With no remaining rows, the pane becomes empty. Loading the successor does not wait for archive
admission or completion, and a later archive failure does not replace the new selection.

The reading column separates the attention summary, original conversation and
reply composer. Why this matters and Improve priorities expand on demand, while
the persistent Your reply action reaches the editor without scrolling through
the conversation. From and To stay visible; Cc, Bcc and subject expand together
and initially open when copy recipients are present. Draft options collect save,
history, writing-example endorsement and discard. Review & Send remains the
primary composer action and opens a separate, scrollable exact-message review.
Native interaction coverage checks the five-row inbox, divider dragging and
width preservation across mode changes and relaunch, the reply shortcut,
optional feedback, recipient-field discovery and separation of Chat and Email
toolbar actions in light and dark appearance. Email draws on the A2 tokens: paper behind the
list and the reading column, teal for the gist rule and draft states, amber
for attention, and filled capsules that keep contrasting text.

Thread detail renders text and attachment metadata without remote content.
Feedback distinguishes people, topics, thread importance and reply need, reports
the applied scope and judgment, and supports Undo. Changing the target clears
the previous person or topic value; person feedback requires an explicit choice.
Learning controls expose pause/resume, scoped resets and explicit source
exclusion. Draft edits autosave
with optimistic revisions; conflicts preserve local and server versions, and
draft history can restore earlier text into the editor. Typing continues while a
save is in flight, so Review & Send settles the newest revision through bounded
save attempts before freezing one, and reports a save that will not settle rather
than freezing text the owner cannot see. A failed owner action is reported beside
the control that caused it, and a failed mail read with the conversation it could
not refresh; an action refused far from its button reads as no action at all. The Writing style menu
can explicitly endorse the displayed wording as an example after saving any
edits; a save conflict prevents endorsement. The action binds the current draft
revision and preserves the learning pause state. The learning pane discloses
historical/Sent analysis, shared memories and style, and hosted-model processing.
Coverage counts describe threads retrieved, not completed semantic analysis.
Review & Send verifies
the approval's exact tool/account, provider thread, recipients, subject and body
against the frozen draft before presenting the existing approve-once action.
Because the approval view truncates a string over 512 characters, a reply longer
than that is verified through the published `argument_digests` entry for that
argument, which proves the whole value rather than the retained prefix; a
truncated argument with no digest stays unverifiable and is refused rather than
presented. The client never invokes Gmail or substitutes a retry send after
uncertainty.

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
families, but no database or live mailbox. The Mac journeys run inside the
local Tart VM described under Build and connect, through
`tests/native_mac_vm.py`, so they never take over the desktop; setting
`VEETBOT_NATIVE_MAC_ON_HOST=1` runs them on the host instead. Without the VM the
check still runs the simulator journeys and then skips. An unavailable Apple
lane is skipped by pytest and remains an unpassed active gate in the gate
report.

## Call results and text messages

Call results show the retained summary first, provider duration and date when
available, and the transcript under What was said. Truncation, unverified
caller statements, local retention and separate provider retention remain
explicit. Call again and Something is wrong stage an editable Chat follow-up
without sending, redialing or reporting anything; existing draft text is kept.
Only the call identifier enters that draft. Pending, active and erased records
offer no follow-up. Deleting call content retains its exact confirmation.

The SMS sheet adds the recipient and an explanation that the text comes from
this iPhone, above the system Messages composer. The system's Send tap and
compose delegate remain the sole send confirmation. The prototype's timer and
manual "I sent it" control are not completion evidence and are not implemented;
there is no duplicate approval step (ADR-0081).

## Subscriptions

Unsubscribe assistance (Milestone 31, `plan/email-unsubscribe.md`) adds a
Subscriptions surface to Email mode, in its own `EmailSubscriptionsViewModel`.
The entry appears in the Email toolbar — the shared Mac window toolbar, and the
iPhone and iPad navigation bar — only while an account's projection reports
`unsubscribe_supported`. The open Mac window updates that entry when accounts
finish loading or the connection is cleared. A server or account that advertises nothing shows no
entry and no thread action, and the client never substitutes another command
for the missing one. Rows come from `GET /v1/email/subscriptions` in the order
the server returns them, filtered by account and state and paged by cursor;
each row shows the sender, how many conversations arrived in the window, when
the last one did, what the sender offers in plain words, and its state.
The census is a flat list with a direct Unsubscribe or Try again action beside
eligible senders. That action opens the same exact confirmation as the detail
page; it never sends on the first click. Opening the sender reveals its full
detail beside the census on Mac and regular iPad, or pushes it on compact
screens. Closing the detail returns all available width to the flat census.

Selecting is explicit and bounded. Selection stops at twenty-five, and Select
all additionally skips protected senders and any row that is not eligible —
unverified, offering no mechanism, or already decided. An unverified row reads
*Checking…* and cannot be selected, while Keep and Report spam stay available
on it. Unsubscribe on one row, or *Unsubscribe N*, opens one confirmation that
names every sender with its mechanism — *One-click request to example.com* or
*Sends an email to unsub@…* — states that an unsubscribe cannot be undone, and
offers *Also archive existing mail from these senders*, off by default.
Confirming is the consent and sends exactly one command. The unsubscribe
address is never part of a client projection: a target carries a subscription
identifier, its evidence digest and its expected revision, and nothing else.

Rows then read *Unsubscribing…* and settle from the durable operation, polling
`GET /v1/email/operations/{id}` with the bounded backoff archive rows use
before reading the census back. Success is quiet. A failed run, an unreadable
status, a `failed` row and an `uncertain` outcome all restore the row with an
actionable error and offer what remains: try again, Report spam, or Keep. A
`still_sending` row says so and offers Report spam; a `reported_spam` row
offers Not spam; Keep is reversible. A bulk conversation whose thread
projection carries a `subscription` block shows the same Unsubscribe action
beside its sender and opens the same confirmation with one row; each surface
presents only the confirmation it opened.

A conversation the server flags as suspected spam (ADR-0165) carries a
*Suspected spam* tag in its row and in its detail. The detail explains that
nothing has changed in Gmail and offers Report spam and Not spam; Not spam
posts `POST /v1/email/threads/{id}/spam-flag` and writes nothing to Gmail.
Every Inbox conversation also offers Report spam in its thread actions. Report
spam posts `POST /v1/email/threads/{id}/spam` with Archive's optimistic
handling: the row leaves at once, the reading pane advances, and a failed,
uncertain or unreadable outcome restores the row with an actionable error. A
reported conversation reads *Reported as spam*, offers Not spam, and cannot be
moved back with the Archive checkbox. Without the account's `spam_supported`
none of these actions appear, and a missing route never falls back to Archive.
The `--ui-testing-email-suspected-spam` fixture serves a flagged conversation
for the `testSuspectedSpamNotSpamClearsTheFlag` and
`testSuspectedSpamReportSpamRemovesTheConversation` journeys, which the native
bridge runs beside the Subscriptions journeys.

Run `uv run pytest tests/native/test_email_unsubscribe_native_m31.py -q` for the
executable native unsubscribe gate check. This integration-marked bridge runs
the real `EmailSubscriptionModelTests`, `EmailSubscriptionsViewModelTests` and
`EmailSubscriptionViewStructureTests` Swift suites, plus the named spam cases
in `EmailArchiveModelTests` and `EmailViewModelTests`, and verifies each case
reported passing. It also runs the Subscriptions and spam journeys on Mac,
iPhone and iPad, the Mac ones inside the local Tart VM as for the Email checks
above. It needs full Xcode and both simulator families but no database or live
mailbox, and an unavailable Apple lane is skipped by pytest and remains an
unpassed active gate in the gate report.

## Approval feedback and deadline outcomes

Approval cards retain the first stored resolution when an older pending read
arrives. A resolution event, or a card disappearing from the pending list,
refreshes the stored decision across devices. After a successful decision the
client reads the run state, displaying a completed or failed outcome instead of
leaving a stale wait. Reads cannot update a different selected conversation or
connection. Pending approvals load only for a selected conversation; a late refresh
after starting a new conversation cannot fetch account-wide cards.
Historical cancellation events whose reason is `deadline` show the
same actionable deadline explanation as new failed runs (ADR-0209). These locally
reconstructed failures carry no diagnostic reference; authoritative server failures
retain their validated references.
