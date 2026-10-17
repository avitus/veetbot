# ADR-0211: The Apple client's deployment floors rise to iOS 27 and macOS 26, toward macOS 27

- Status: Accepted — the owner approved the interim floors on 2026-10-17;
  the macOS 27 floor follows when CircleCI ships a macOS 27 image
- Date: 2026-10-17
- Related: Section 29 (multi-device shared core), Milestone 26
  (`docs/plan/email-experience.md`), ADR-0049 (amends decisions 1 and 7),
  ADR-0050, ADR-0128 (cites the old floor), ADR-0114 (hosted verification)
- Client documentation: `docs/apple-client.md`, `clients/apple/README.md`

## Context

ADR-0049 set the native client's deployment floors at iOS 15 and macOS 12 at
the owner's request. Those floors predate SwiftData (iOS 17, macOS 14), so the
client carried a second, file-backed implementation of its local history
store, and they predate `NavigationSplitView`, `NavigationStack`,
`ViewThatFits`, `menuStyle(.button)` (iOS 16, macOS 13), fitted sheet sizing
(macOS 15), App Intents (iOS 16) and the accessibility auditor (iOS 17,
macOS 14), so the views branched on `#available` at each of those points.
Milestone 26's design asked the Email experience to preserve those floors.

On 2026-10-16 the owner began a from-scratch redesign of the iOS and macOS
clients and decided that it targets the current releases, iOS 27 and
macOS 27, so that the new screens build on the current material language and
carry no compatibility branches for systems the redesign will not ship to.
The owner's Mac runs macOS 27.0.1, and Xcode 27.1 ships the iOS 27.1 and
macOS 27.0 SDKs.

Hosted verification constrains the macOS half. The required `apple` job runs
`make test-apple` (the package's Swift Testing suite, on the runner itself)
and the Mac UI cases on a CircleCI Xcode image. On 2026-10-17 CircleCI's
supported-Xcode table listed Xcode 27.0 (27A266a), 27.1 (27A9275) and the
27.2 beta (27B5038j) all on macOS 26.6.2, the newest host it offers. A
product built with a macOS 27 floor does not load on that host, so a macOS 27
floor would fail both halves of the required job until a macOS 27 image
exists.

On 2026-10-17 the owner decided: "Land the branch with iOS 27.0 and
macOS 26.0 as the interim floor, and move macOS to 27.0 when CircleCI ships
a macOS 27 image."

## Decision

1. **iOS 27.0 is the iOS floor.** Every build configuration of the app and
   UI-test targets and the package manifest declare it.
2. **macOS 26.0 is the macOS floor for now.** It is the newest floor hosted
   CI can verify; until it rises the client supports macOS 26 and 27.
   **Trigger for the follow-up:** CircleCI's supported-Xcode table
   (circleci.com/docs/guides/execute/using-macos) lists an Apple-silicon
   image whose macOS host is 27.x. The follow-up then changes exactly:
   - `.circleci/config.yml`: the `xcode:` image of the `apple`, `apple-ios`,
     `apple-testflight` and `apple-signing-smoke` jobs to that image;
   - `clients/apple/Veetbot.xcodeproj/project.pbxproj`: the four
     `MACOSX_DEPLOYMENT_TARGET = 26.0;` values (the Veetbot and VeetbotUITests
     targets, Debug and Release) to `27.0`;
   - `clients/apple/Package.swift`: `.macOS("26.0")` to `.macOS("27.0")`;
   - `clients/apple/Tests/VeetbotCoreTests/DeploymentFloorTests.swift`: the
     `macOSFloor` constant from `(major: 26, minor: 0)` to `(major: 27,
     minor: 0)`;
   - this record's status and decision, `docs/apple-client.md`,
     `clients/apple/README.md` and a changelog entry, which name macOS 26;
   and verifies with the three Apple lanes and hosted CI. No source branch
   depends on the difference: nothing in the client checks for a version
   between macOS 15 and macOS 27.
3. **Every availability branch below the floors is removed.** The sidebar,
   Email mode, People, Memory synthesis and the fact-evidence sheet use
   `NavigationSplitView` and `NavigationStack` without a `NavigationView`
   fallback; Mac sheets always use fitted presentation sizing; `ViewThatFits`
   and `menuStyle(.button)` are unconditional; the SMS App Intent and the
   People accessibility audit carry no availability annotation. A package
   test reads every `#available` and `@available` clause in the client, its
   tests and its UI tests and fails on any version below the floors.
4. **Local history has one backend, SwiftData.** The file-backed store that
   served iOS 15–16 and macOS 12–13 is deleted with its tests. The store
   contract, its deterministic order and its folder and schedule round trips
   are kept and run against the SwiftData and in-memory stores. On every
   system the new floors admit, a release wrote the file only after SwiftData
   failed to open its container; that failure now logs and uses the in-memory
   store for the launch, and the authoritative session index repopulates it
   on connect (ADR-0050). The cache boundary of ADR-0049 decision 7 is what
   makes that degradation acceptable: nothing in the file was ever
   authoritative.
5. **Hosted CI keeps the Xcode 27.0.0 image.** Its macOS 26.6.2 host and
   iOS 27.0 simulators satisfy both floors; no job changes.
6. **The documents that named the old floors change with this record.**
   `docs/plan/email-experience.md` preserves the floors this ADR sets rather
   than iOS 15 and macOS 12; ADR-0049 decisions 1 and 7 are amended as above;
   `docs/apple-client.md` and the client README describe one history store.
   ADR-0128's note that CryptoKit HPKE sat above the floor is no longer a
   constraint, but its other reasons stand and that decision is not revisited.

## Consequences

- Devices on iOS 15–26 or macOS 12–25 cannot install releases from this
  change on. TestFlight and App Store builds declare the new floors.
- Hosted CI verifies the Mac client on macOS 26.6 only; macOS 27 behaviour
  is verified by the local lanes on the owner's Mac (macOS 27.0.1) until a
  hosted macOS 27 image exists. The iOS lanes verify iOS 27 on both.
- The redesign uses iOS 27 and macOS 26 APIs without branches. The remaining
  `NavigationView` uses were never behind an availability check; replacing
  them is the redesign's work, not this record's.
- The SMS capture ceremony's iOS 17 prerequisite is met by the floor.
- Package builds under Command Line Tools, which cannot link SwiftData, use
  the in-memory store, as the app itself never does.

## Alternatives considered

- **macOS 27 now, with the Mac lanes skipped or moved out of the required
  job:** rejected; it weakens hosted verification, which AGENTS.md forbids
  without the owner's approval.
- **Keep the file store as the SwiftData-failure fallback:** rejected; two
  persistence backends for a non-authoritative cache, one of them exercised
  only after a failure, against an in-memory store the server index refills.
- **Wait for a macOS 27 image before changing anything:** rejected; the iOS
  floor and every branch removal are independent of the macOS host version.
- **macOS 27 now, with the Mac lanes on another runner:** not taken; the
  owner chose to wait for CircleCI's macOS 27 image.
