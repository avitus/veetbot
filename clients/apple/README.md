# Veetbot for Apple platforms

This directory contains the native SwiftUI client for iOS 15+ and macOS 12+.
It is a transport-only client of the public `/v1` HTTP API; the shared core
remains authoritative for sessions, runs, approvals, events, and artifacts.

Open `Veetbot.xcodeproj` in a full Xcode installation and select an iOS or macOS
destination. Before the first signed build, select the `Veetbot` target, open
Signing & Capabilities, and choose your Apple development team. The target has
no third-party dependencies. On first launch, enter an HTTPS API base URL and a
static bearer token. The base URL is stored as a preference; the token is stored
only in Keychain.

After a connection is configured, the application delegate requests notification
permission and registers with APNs. A client-minted installation identifier is
stored once in the local, nonsynchronizing Data Protection Keychain beside the
bearer credential; APNs tokens are uploaded to the feature-gated `/v1/devices`
surface on launch and whenever Apple rotates them. Forgetting the connection
revokes the server device before deleting the local bearer. A server release
without the device routes is detected as an older compatible server and does not
prevent normal conversation use.

Notification payloads contain identifiers and a closed status vocabulary, never
conversation content. Tapping a notification restores the authoritative
transcript, attaches to the payload's exact run, and focuses the referenced
approval or question. The client does not persist a notification inbox. Before a
physical-device push can work, the application identifier must have the push
capability enabled and its provisioning profiles regenerated in the Apple
Developer portal; the tracked project cannot perform those owner actions.
The shared Xcode target selects platform-specific entitlement files: iOS uses
`aps-environment`, while macOS uses `com.apple.developer.aps-environment`.
The generated application property list declares
`ITSAppUsesNonExemptEncryption=NO`: the client uses platform-provided HTTPS and
Keychain protection and implements no non-exempt encryption. Reassess that
declaration before adding a custom or third-party cryptographic implementation.

Files can be attached to a chat message (ADR-0120). On Mac and iPad, drop files
anywhere on the conversation or on the message field; plain text dropped on the
field is still inserted as text. The paperclip beside the field opens Files on
every device and, on iOS, the photo library, which needs no photo permission.
Each file uploads as soon as it is added and shows its progress; a failed upload
can be retried or removed, and a message may be attachments alone. Images other
than small PNG, GIF, and WebP files are re-encoded as JPEG with a 2000-pixel
long edge before upload, which removes their location metadata. A server
without the upload route shows each file as not accepted, and an answer to a
clarifying question stays text: staged files wait for the next message.

A file the agent exports arrives as a button under its answer and opens the
artifact viewer with a preview and Download (ADR-0122). Every finished message
has Copy, which writes formatted text (RTF and HTML, plus plain text without
Markdown symbols), and Select Text, which opens the message in a native text view
where any range can be selected.

The settings surface groups Connection, Models, Website Access, Appearance, and
Data & Privacy in a scrolling layout. The Connect or Update Connection action sits in
the Connection card, while device-local text-size and font-style controls save
automatically and apply immediately throughout the client. The Models card reads
and writes the server's `/v1/settings/models` resource (scopes `settings.read`
and `settings.write`): a chat model and reasoning effort, and a memory-formation
model and effort limited to the combinations the server offers. Each picker change
saves the whole state at once against the version it read; a concurrent change
elsewhere reloads the server's values, any other failure reverts the pickers, and
a server without the resource hides them. On macOS, configured
clients use a separate settings window that resizes in both dimensions and
remembers its frame. The main and settings windows persist their sizes and
positions independently; system text sizing remains the default.
On Mac, sheets open at readable widths: 640 points for Persona, 600 for email
draft history and send review, and 540 for call results and email learning.
Before macOS 15, which ignores a sheet's ideal size, they open at their 480- to
520-point minimums.
On iOS, the sidebar toolbar exposes Memory, Schedules, Persona, and Settings in
an explicit accessible More menu so every destination remains usable at narrow
split-view widths.

Website Access creates and lists dedicated browser profiles from one Website URL.
Enter a homepage such as `example.com` or a full login link such as
`https://www.example.com/login?next=%2Fhome`. HTTPS is added when omitted. The app
derives the primary allowed origin and opens the full URL in the isolated browser.
The isolated browser automatically loads the site's public HTTPS scripts,
styles, images, fonts, APIs, and embedded verification frames, including those
hosted on CDNs. No additional domain configuration is required, including for
existing profiles. Top-level navigation remains scoped to the profile's website
origins, and private-network access is blocked. By default the app signs in on
this device: it opens the website in a private sign-in window that Veetbot
does not read or keep, the user signs in there, and after I'm signed in the
app hands only that website's session to Veetbot's isolated browser service,
once, and clears the window. The window cannot be closed while the sign-in is
being checked; if it closes anyway, the sign-in stops, nothing is chosen for
new chats, and a website login the window created is removed. Passkeys and
sign-in through another website's identity provider do not work in that
window. Sign in again on a profile row opens the same window for a profile
that is not revoked; if a remote sign-in is still open for that profile, the
device sign-in cancels it and the app forgets its link, so Start over cannot
remove the signed-in profile. Use Veetbot's remote
browser remains available, and the sign-in window offers it when a new
website's sign-in cannot start on this device: the app opens the
server-provided isolated login ceremony only after a separate Continue in web
browser action, where the user enters website credentials directly. In either mode, usernames, passwords, cookies,
and browser storage never pass through chat, the Veetbot API, or the agent.
Selecting a ready profile binds only its opaque UUID to newly created
conversations.
Veetbot reuses the encrypted browser session across runs while the website
accepts it. There is no fixed reauthentication interval: expiration, logout, or
a site's MFA/CAPTCHA challenge requires user sign-in again. The five-minute
ceremony limit applies only to the interactive login window. Website passwords
are not stored for automatic sign-in.
The saved selection is revalidated against the current principal when the
bearer credential changes and when the app reconnects after launch; missing or
non-ready profiles are cleared before another conversation can use them. If the
platform rejects the browser handoff, the app cancels the ceremony and removes
the unused profile. Start over provides the same recovery for a closed,
reloaded, or expired one-time link. The installed version and build number are
shown under Data & Privacy; this recovery release is 0.1.1 (2).

A `browser.act` approval card names the action, the element and the page,
and shows the website's own text in quotes, labelled as coming from the
website. When the server offers it, Allow for this task lets Veetbot act
inside that site scope without asking again, for up to thirty minutes and
two hundred actions. A banner above the composer counts them, and Stop ends
the permission at once; Website Access lists active permissions in a compact
Task permissions row whose menu stops each one. Passwords, payments,
purchases, account changes and messages still ask every time.

In Email feedback, **This kind of content** offers the topics identified on the
selected thread. Choose a topic before marking it Important or Less important;
the confirmation names that topic and offers Undo. If no topics are available,
the view explains that feedback can still apply to **This thread**. Older
servers that omit topics remain readable. Missing or unsupported feedback
targets return selection guidance without echoing private content.

The source is organized into `Models`, `Networking`, `Streaming`, `Store`,
`ViewModels`, and `Views`. A Swift package builds the shared source and hosts its
wire, transport, reducer, SSE, and local-history tests:

```bash
swift build --package-path clients/apple
make test-apple
make test-apple-ui
```

Run the test targets from the repository root. Both require full Xcode so a
Command Line Tools build cannot be mistaken for an executed Swift Testing run.
`make test-apple-ui` selects available iPhone and iPad simulators and exercises
opening and switching durable historical transcripts and starting a new
conversation, and the `--ui-testing-folders` journeys file, rename, accept and
decline conversation folders while the default fixture proves an older server
stays flat. Its launch fixture is debug-only, suppresses notification
authorization, and uses an isolated in-process transport, so it needs no server
or credential. It runs `make test-apple-ui-macos` and then
`make test-apple-ui-ios`; either target runs alone when only one platform
family changed. Each UI case sets its launch arguments and environment before
calling `app.launch()` once. Terminate and relaunch only in a case that tests
relaunch behavior.
Every Mac case also launches with `-ApplePersistenceIgnoreState YES`. XCTest
ends the app without quitting it, so AppKit would otherwise restore the window
list the previous launch saved; an empty list leaves the app with no window.
The main window's saved size is the app's own preference and still persists.
AppKit reads launch arguments as `-key value` pairs, so the pair comes before
the bare `--ui-testing-*` flags: a flag ahead of it would take the key as its
value, and AppKit would open the leftover `YES` as a document instead of a
window. Under the fixture the composer also turns off Writing Tools, whose
macOS 27 affordance window otherwise floats over the controls the cases click.
The keyboard-dismissal case holds its fake submission pending until teardown,
so a slow accessibility query cannot consume the response-delay window.
The adjacent-folders case measures the two list rows that hold the folders and
requires them to touch. The space between the labels inside depends on the
system's row and label heights, which differ between macOS releases: 16 points
on the hosted macOS 26.6.2 runner and 13.5 on macOS 27, against 26.5 on
macOS 27 once each folder sits in a section of its own.

The simulator runs pass `-collect-test-diagnostics never`. Under Xcode 27
xcodebuild ended both hosted simulator runs with `simctl diagnose
--timeout=600`: the iPad run after one failure, and the iPhone run after none,
only its standing skipped case. On the hosted image that collection prints
nothing, times out after its 600 seconds, and yields no diagnostics, while
CircleCI ends a step after ten minutes without output, so the job died before
xcodebuild could name the failing case. The result bundle still holds each
failure, its screenshot, and the element tree.

`make test-apple-ui-ios` boots both simulators to completion, concurrently with
`xcrun simctl bootstatus -b`, before it starts the two runs. Xcode 27.0's
xcodebuild installs `com.veetbot.apple.UITests.xctrunner` about four seconds
into a boot it starts itself, without waiting for SpringBoard. One cold boot has
SpringBoard up by then. Two at once delayed it to about seven seconds on the
owner's Mac, so SpringBoard started after the install, logged "Cannot launch
application scene … while it's application is being updated" for each of its
launch retries, and xcodebuild exited 65 with `Busy ("Application failed
preflight checks")` before any case ran. Separate copies of the test products
failed the same way, so the shared `.xctestproducts` is not the cause, and a
staggered second start only hides the race. Read the refusal from the
simulator's own log with `xcrun simctl spawn <udid> log show --predicate
'process == "SpringBoard"'`. A simulator whose data migration failed ends
`bootstatus` within a second or two with "Data Migration Failed" and exit 0, so
for a boot that does not report "Finished" the target waits until `notifyutil -g
com.apple.springboard.finishedstartup` holds SpringBoard's process id, and says
so; `xcrun simctl erase <udid>` repairs such a simulator. Simulators the target
booted are shut down when it ends, as xcodebuild did when it booted them; one
that was already open stays open.
The cases tap an overflow-menu item at its centre rather than as an element,
after requiring it to exist and be hittable. On the iOS 27.0 iPad simulator
XCUITest's element tap on the menu's first item is swallowed and the menu stays
open, while a touch anywhere on the item activates it. A touch that lands while
the menu is still opening is dropped as well, so the helper waits for the item's
frame to stop changing, and taps again, up to three times, until the menu closes.
The email cases allow twenty seconds for a Gmail archive outcome: the fixture
holds the operation pending for four thread reads and the client polls once a
second, so the outcome takes three seconds on an idle host and more beside a
second simulator.

The People accessibility audit uses the app's Large text preference on Mac and
the largest accessibility Dynamic Type category on iPhone and iPad. The Mac
debug fixture reads `VEETBOT_UI_TEST_TEXT_SIZE` into isolated appearance defaults,
leaving the owner's preferences untouched and avoiding positional AppKit launch
arguments. UIKit's content-size launch preference is restricted to the iOS test
targets.
Person-profile empty states use primary text contrast so missing history and
coverage remain readable at enlarged text sizes.
On Mac, People sheets open at readable widths: 600 points for short editors and
760 points for the import history, source conversations, and identity repair.
Before macOS 15, which ignores a sheet's ideal size, they open at their 560- and
680-point minimums.

On Mac, Memory's People collection is a split view whose directory selects the
person the second column shows. Each row is a button that carries the selected
trait. Its earlier navigation links stopped responding after the first choice
once the directory was longer than its column, so the first profile stayed
open; `testMemoryPeopleShowsEachChosenPerson` reproduces this with the
`--ui-testing-people-directory` fixture, which pages 72 people at the client's
limit of 50. `PeopleDetailView` keys its state to the person, because SwiftUI
keeps a view's state when a column shows it again for someone else. The
column's placeholder has a zero minimum height: a column that cannot shrink
made the split view grow to the directory's length and pushed its first rows
above the sheet. Related people open in place, and a fact's evidence opens in
a sheet, since the column has no way back.

The person profile is one scrolling column at a readable width on every
platform: a header with initials, name, relationship to the owner, main
address and actions, then cards for names and contact details, relationships,
a history timeline, open threads, facts and evidence, and Manage person.
Sections with nothing recorded collapse into one note. Its palette in
`PeopleProfileComponents.swift` reuses Email's adaptive accent and surfaces and
adds an attention orange and a destructive red that keep 4.5:1 contrast in both
appearances. People views set that adaptive accent as their tint: under the
app's fixed turquoise tint, bordered buttons were unreadable in dark
appearance. At accessibility sizes the header stacks, row symbols drop out,
and buttons use rounded rectangles so a wrapped label is not clipped.

SwiftData is used for local history on iOS 17+/macOS 14+. Because SwiftData does
not exist on the app's minimum OS versions, iOS 15–16 and macOS 12–13 use the
same `SessionHistoryStore` contract backed by an atomic Application Support JSON
file. Both carry the optional server-assigned folder and schedule identifiers;
the SwiftData columns are optional attributes added by automatic lightweight
migration, and a failed migration falls back to the file store as before.
Neither store is
authoritative server state. The app reconciles both from
the paginated server session index on connect, foreground entry, and a periodic
poll. The row action is `Delete Everywhere`: it deletes the authoritative
session first and removes local state only after the server succeeds. Active
runs must be stopped before their session can be deleted.

Selecting a conversation reads its complete, paginated durable message
transcript from the shared core before attaching to the active or latest run.
The client uses persisted session sequences to prevent the latest run's replay
from duplicating messages already restored from the transcript.

Adjacent terminal tool calls share one compact, expandable summary, including
alternating tools or Gmail accounts and mixed successful/failed outcomes. The
summary shows total calls and outcome counts together and uses the highest risk.
Expanding reveals every exact tool name, status, argument, result, and individual
risk. Messages, unknown names, approvals, unfinished calls, denials, and uncertain
outcomes remain separate. Error results are counted as failed, including when the
wire event says completed; corrected retries retain their specific status.
On iOS, tapping Send or pressing Return immediately clears the submitted draft
and dismisses the software keyboard, before waiting for the server. A persistent
activity row above the composer shows **Sending…** during submission, then
**Working…** or **Reasoning…** while the accepted run is active, even when the
transcript is scrolled away from the bottom. The indicator clears when work ends
or needs input. A failed submission restores the draft if the composer is still
empty, without overwriting newer text. A delayed success does not dismiss a
keyboard reopened to write the next message.

The Command Line Tools-only Swift installation can compile the package but may
not include a functioning Apple test-bundle runner. Use full Xcode to execute
the Swift Testing suite when `swift test` builds without discovering tests.

People identity-evidence paging retains the final fetched page and stops when a
server cursor repeats. If saving during Review & Send makes a draft stale, the
composer asks the owner to refresh the thread and review the draft again.
