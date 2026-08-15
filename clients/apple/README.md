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

The resizable settings surface keeps connection actions pinned below its
scrolling content and includes device-local text-size and font-style controls.
Appearance changes apply immediately throughout the client; system text sizing
remains the default.

The source is organized into `Models`, `Networking`, `Streaming`, `Store`,
`ViewModels`, and `Views`. A Swift package builds the shared source and hosts its
wire, transport, reducer, SSE, and local-history tests:

```bash
swift build --package-path clients/apple
make test-apple
```

Run the test target from the repository root. It requires full Xcode so a
Command Line Tools build cannot be mistaken for an executed Swift Testing run.

SwiftData is used for local history on iOS 17+/macOS 14+. Because SwiftData does
not exist on the app's minimum OS versions, iOS 15–16 and macOS 12–13 use the
same `SessionHistoryStore` contract backed by an atomic Application Support JSON
file. Neither store is authoritative server state. The app reconciles both from
the paginated server session index on connect, foreground entry, and a periodic
poll. The row action is `Delete Everywhere`: it deletes the authoritative
session first and removes local state only after the server succeeds. Active
runs must be stopped before their session can be deleted.

The Command Line Tools-only Swift installation can compile the package but may
not include a functioning Apple test-bundle runner. Use full Xcode to execute
the Swift Testing suite when `swift test` builds without discovering tests.
