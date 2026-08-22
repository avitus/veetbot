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

The settings surface groups Connection, Website Access, Appearance, and Data & Privacy in a
scrolling layout with connection actions pinned below it. On macOS, configured
clients use a separate settings window that resizes in both dimensions and
remembers its frame. The main and settings windows persist their sizes and
positions independently. Device-local text-size and font-style controls apply
immediately throughout the client; system text sizing remains the default.

Website Access creates and lists dedicated browser profiles. The app opens the
server-provided isolated login ceremony, where the user enters website
credentials directly; usernames, passwords, passkeys, MFA values, cookies, and
browser storage never pass through this client or chat. Selecting a ready
profile binds only its opaque UUID to newly created conversations.

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
conversation. Its launch fixture is debug-only and uses an isolated in-process
transport, so it needs no server or credential.

SwiftData is used for local history on iOS 17+/macOS 14+. Because SwiftData does
not exist on the app's minimum OS versions, iOS 15–16 and macOS 12–13 use the
same `SessionHistoryStore` contract backed by an atomic Application Support JSON
file. Neither store is authoritative server state. The app reconciles both from
the paginated server session index on connect, foreground entry, and a periodic
poll. The row action is `Delete Everywhere`: it deletes the authoritative
session first and removes local state only after the server succeeds. Active
runs must be stopped before their session can be deleted.

Selecting a conversation reads its complete, paginated durable message
transcript from the shared core before attaching to the active or latest run.
The client uses persisted session sequences to prevent the latest run's replay
from duplicating messages already restored from the transcript.

Adjacent successful completions of the same tool are displayed as one counted
activity bundle. Expanding the bundle retains access to every call's arguments
and result, including its individual risk. The collapsed bundle uses the
highest risk among its calls. Messages, different tools, approvals, failures,
denials, uncertain outcomes, and error results remain separate activity items.

The Command Line Tools-only Swift installation can compile the package but may
not include a functioning Apple test-bundle runner. Use full Xcode to execute
the Swift Testing suite when `swift test` builds without discovering tests.
