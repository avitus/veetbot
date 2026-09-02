# ADR-0074: Automated macOS TestFlight delivery

- **Status:** Accepted
- **Date:** 2026-08-26
- **Related:** ADR-0025, ADR-0035, ADR-0048, ADR-0049, ADR-0062
- **User authorization:** add a CircleCI archive and upload job after enabling
  the macOS build in TestFlight

## Context

The native Apple client supports macOS and already runs its package and simulator
tests in CircleCI, but the required Apple job stops at verification. A successful
`main` pipeline packages and deploys the server and documentation without
archiving or distributing the macOS application. An installed TestFlight client
therefore cannot receive a new build from a repository change unless the owner
archives and uploads it manually.

TestFlight identifies successive uploads by `CFBundleVersion`. The tracked Xcode
project keeps a useful local default, but a clean CI checkout cannot increment
and commit that value without creating a release commit loop. CircleCI already
provides a project-scoped, increasing pipeline number, and the release graph
already serializes production-changing jobs.

Apple distribution credentials can sign and publish software under the owner's
identity. They must not be stored in the repository, ordinary project variables,
artifacts, caches, or logs. The production server context is also the wrong
boundary: its SSH coordinates grant unrelated host authority and must not be
shared with an Apple publication job.

## Decision

1. Add a dedicated `apple-testflight` job to the existing CircleCI file. It uses
   the pinned Xcode macOS executor and runs only on `main` after `deploy-app`
   succeeds. The dependency means all five verification partitions have passed
   and the matching server revision is publicly ready before an automatically
   updating client can be distributed.
2. The job archives only the native macOS destination from the exact checked-out
   commit. It overrides `CURRENT_PROJECT_VERSION` with CircleCI's
   project-scoped `pipeline.number`; it does not edit or commit the Xcode project.
   The package is built from that inspected archive, so the archived and uploaded
   values remain identical.
3. Before upload, the job reads the archived `Info.plist`, requires the build
   number to equal the pipeline number and the bundle identifier to equal
   `com.veetbot.apple`, and verifies the code signature. Any mismatch fails
   before external publication.
4. CircleCI's managed signing facility owns the `Apple Distribution` identity
   and macOS App Store provisioning profile in a signing bundle named
   `veetbot-app-store`, plus the `Mac Installer Distribution` identity required
   to sign the App Store package in `veetbot-mac-installer`. The job invokes
   `install_signing_bundle` for both; certificate and profile bytes never become
   repository variables or workspace artifacts. Before package signing, the job
   unlocks CircleCI's ephemeral null-password signing keychain and grants its
   signing keys the `apple-tool:`, `apple:`, and `codesign:` partitions. That
   runner-local access prevents `productbuild` from blocking on an unavailable
   GUI keychain prompt and disappears with the temporary keychain after the job.
5. The non-secret Apple team ID remains authoritative in the checked-in Xcode
   project. The archive uses that Release build setting. A separate restricted
   context, `veetbot-apple-testflight`, supplies only the App Store Connect API
   key's base64-encoded private key, key ID, and issuer ID. The private key is
   decoded under a process-local temporary directory with mode-restricting
   umask, passed directly to `altool`, and deleted on every shell exit. The
   archive is not stored as a CircleCI artifact.
6. Apple's `productbuild` creates one non-empty installer package directly from
   the verified archived app, sets `/Applications` as the package's install
   location, and signs it with the exact installed
   `3rd Party Mac Developer Installer` certificate.
   `pkgutil` verifies that package signature before the job invokes the
   Xcode-bundled `xcrun altool --upload-app` with the package, API key ID,
   issuer ID, and temporary private-key file. `altool` upload acceptance is the
   CI success boundary. Apple's later processing, TestFlight group assignment,
   and device installation remain observable external states rather than
   claims made by this job.
7. Apple delivery has its own CircleCI serial group. A newer `main` pipeline
   cannot race an older upload, and the Apple signing authority is not coupled
   to the production host's serial group or context.
8. The owner must configure automatic distribution for the intended TestFlight
   group and enable automatic updates in TestFlight on each Mac. Those user and
   App Store Connect settings are prerequisites outside the repository; the app
   cannot force them.

This extends ADR-0048's post-gate delivery graph and ADR-0049's native-client
verification without changing an engineering-plan requirement or milestone
gate.

## Consequences

- Every successful `main` application deployment attempts one signed macOS
  TestFlight upload, including commits that do not change Apple source. This
  preserves commit-atomic delivery and consumes additional macOS executor and
  App Store processing capacity.
- The first automated pipeline number must be greater than the latest accepted
  build number for the current marketing version. Once automation is active,
  manual uploads must not jump ahead of the CI counter.
- A rerun of a pipeline whose upload reached Apple but whose response was lost
  reuses the same build number. Apple may report it as a duplicate; the operator
  must inspect App Store Connect rather than publishing a different untested
  identity from that rerun.
- Context access, signing-bundle access, branch protection, and review of this
  job are release-signing security controls. Compromise of either Apple
  credential boundary can publish a trusted binary even though it grants no
  production-host or agent credential.
- External setup is intentionally fail-closed. Until the signing bundle and
  restricted context exist, the `main` delivery workflow cannot report success.

## Alternatives considered

- **Continue manual Organizer uploads:** rejected because it does not connect a
  tested `main` revision to the build installed by TestFlight.
- **Store the distribution certificate and provisioning profile as ordinary
  environment variables:** rejected in favor of CircleCI's managed, ephemeral
  signing bundle.
- **Use Xcode Cloud:** viable, but rejected because it creates a second CI
  authority beside the repository's required CircleCI workflow.
- **Add Fastlane:** rejected because Apple's native `xcodebuild`, `productbuild`,
  and Xcode-bundled `altool`, plus CircleCI signing, satisfy this delivery
  without a new runtime or package dependency.
- **Upload before the server deploys:** rejected because automatic installation
  could expose a client that expects an API revision not yet active.
- **Path-filter Apple uploads:** deferred. The existing production delivery is
  commit-atomic and deliberately not path-filtered; this first delivery path
  follows the same rule.
