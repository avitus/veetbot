# M20 Apple client map (explored 2026-09-01, HEAD 56efe50)

For the Milestone 20 iOS work (docs/plan/device-channel-and-sms.md §"The iOS
client and the owner ceremony"). Line numbers from the exploration; re-grep if
edits have moved them. NO M20 iOS code exists yet.

## 1. Build systems (two, parallel, over one source tree)
- clients/apple/Package.swift — swift-tools 6.0, package VeetbotAppleClient, platforms iOS 15 / macOS 12 (:74-77), swiftLanguageModes [.v5] (:100). Library target `VeetbotCore`, path "Veetbot", excludes Resources, ConversationNavigationUITestFixture.swift, VeetbotApp.swift, both entitlements files (:82-91). **Every new .swift under Veetbot/ auto-compiles into VeetbotCore for iOS 15 AND macOS 12 — guard with #if os(iOS) and availability (@available) as needed.** Test target VeetbotCoreTests path Tests/VeetbotCoreTests (:92-98) — new test files picked up automatically. :5-72 has a DEVELOPER_DIR probe injecting Testing-framework flags (load-bearing for swift test).
- Veetbot.xcodeproj/project.pbxproj — objectVersion 56 (NO synchronized groups). Targets: Veetbot (app, :246-260), VeetbotUITests (:263-278). App target compiles files DIRECTLY (explicit Sources phase, 34 entries) — it does not consume the package. **Adding a file to the app build = four hand edits with synthetic sequential IDs (1000…/2000…): PBXBuildFile, PBXFileReference, child in the right PBXGroup (Models 400000000000000000000003, Networking …004, Streaming …005, Store …006, ViewModels …007, Views …008), Sources-phase entry. Last used IDs: 100000000000000000000024 / 200000000000000000000025.**
- Build settings (:494-528 Debug, :537-570 Release): IPHONEOS_DEPLOYMENT_TARGET 15.0, MACOSX 12.0, SUPPORTED_PLATFORMS "iphoneos iphonesimulator macosx", SWIFT_VERSION 5.0, GENERATE_INFOPLIST_FILE YES (no Info.plist file — INFOPLIST_KEY_* build settings). UITests deployment iOS 15 / macOS 14 (:581-587).
- Shared scheme Veetbot.xcscheme TestAction has ONLY VeetbotUITests (:42-51) — SPM unit tests not in the scheme.

## 2. Commands
- `make test-apple` (Makefile:52-62) → swift test --package-path clients/apple (requires full Xcode DEVELOPER_DIR).
- `make test-apple-ui` (Makefile:64-95) → macOS lane: xcodebuild test -project ... -scheme Veetbot -destination platform=macOS -only-testing:VeetbotUITests/ConversationNavigationUITests/testMainWindowSizePersistsAcrossApplicationRestart with code signing/entitlements BLANKED; simulator lanes: newest iPhone + iPad UDIDs via simctl JSON + inline python, -only-testing:VeetbotUITests.
- Build: swift build --package-path clients/apple.
- CI job `apple` (.circleci/config.yml:99-110): macos xcode "26.6.0", m4pro.medium, runs both make targets; required upstream of package-release (:336, :343).

## 3. Device registration (client side)
- Networking/DeviceRegistrationCoordinator.swift — DeviceRegistrationAPI protocol :10-19 (notificationServerID, registerDevice(_:idempotencyKey:), listDevices, revokeDevice); VeetbotAPIClient conforms :177-181.
- AppleDeviceDescriptor :21-66; .current (@MainActor :42-65): iOS → UIDevice name / .mobile / "ios"; macOS → .desktop / "macos". environment .sandbox under DEBUG :53-57. bundleID :62.
- register(deviceToken:descriptor:using:) :89-115 — keychain installation id :94; AppleDeviceRegistration(...) :95-103; push token hex :101.
- **Idempotency key = "\(clientDeviceID):\(sha256 of sorted-keys JSON body)" :117-126 — a new capabilities field changes the digest automatically (re-register, not replay).**
- Registration trigger: ONLY from app delegate on APNs token delivery (ChatViewModel.registerRemoteNotifications :211-226). No re-post on foreground or settings change. forgetCredentials() revokes server device first :176-209. notificationFeatureAvailable :222 (published, never read by any view).

## 4. Networking
- Networking/HTTPTransport.swift — actor HTTPTransport :81; TransportRequest :14-40 (method, path, queryItems, body, headers, requiresAuthentication, retryAttempts); send<Response> :114-120 (JSONDecoder.server); retries with Retry-After :229-247; Bearer from token store :196-202; 401→reauthenticationRequired, 403→authorizationDenied :209-227; redirects rejected :69-79.
- Networking/VeetbotAPIClient.swift — struct :26-31; **adding a route = one method (transport.send one-liner)**. Device routes: registerDevice :208-221 (POST /v1/devices, Idempotency-Key header, retryAttempts 3), listDevices :223-234, revokeDevice :236-244. Private request-body DTOs at file bottom with snake_case CodingKeys :422-467. 404/405→typed feature-unavailable mapping :402-420.
- ConnectionConfiguration.swift — HTTPS-only validation :25-42; url(path:queryItems:) :52-68; **ConnectionConfigurationStore actor over UserDefaults (keys veetbot.connection.baseURL, veetbot.browser.selectedProfileID) :70-103 — the established home for a device-local preference.**
- DTO conventions: Models/APIModels.swift (SessionView, RunView, ApprovalView, Page<Item> :441, ContentBlock :558) + Models/NotificationModels.swift; all public Codable Sendable, hand-written snake_case CodingKeys; coders JSONEncoder/.server JSONDecoder/.server in Models/JSONValue.swift:87-121 (RFC3339 w/ and w/o fractional seconds, sortedKeys).

## 5. Push handling
- NotificationApplicationDelegate.swift — NotificationApplicationDelegateBase :12 (@MainActor, UNUserNotificationCenterDelegate); iOS subclass :129-155, macOS :156-179; wired via adaptors in VeetbotApp.swift:6-13 + attach(to:) onAppear :30.
- attach(to:) :33-53 — drains pendingResponsePayloads; requests remote notifications on configured baseURL.
- Authorization :55-70 — requestAuthorization([.alert,.badge,.sound]). **No UNNotificationCategory registered anywhere; no UNNotificationAction; response.actionIdentifier never inspected (:95-106).** Foreground presentation [.banner,.list,.sound] :120-126. UI-test suppression launch arg --ui-testing-conversation-navigation :17-31.
- **NotificationPushPayload (NotificationModels.swift:156-267) — strict closed parse: reads userInfo["veetbot"], key subset of allowedKeys :176-180, version==1, title must EXACTLY match per-kind table :219-228, exact per-kind identifier sets :230-239, allowed statuses :241-252. A new push kind must be added to NotificationKind (:27-36) + all three tables or init? returns nil and the push is silently dropped.**
- Deep link: NotificationFocus :269-279 (.approval/.question + scrollID), NotificationDeepLink :281-291, reducer :293-309 (needs sessionID+runID). Consumption ChatViewModel.openNotification :228-266 (stash-if-unconfigured :231-232, replay :997; select session; publish notificationFocus + notificationNavigationID).

## 6. Settings UI
- Views/ConnectionSettingsView.swift — ConnectionSettingsSection enum :3-10 (connection, websiteAccess, appearance, dataAndPrivacy); body iterates allCases → sectionView(_:) :51-80; **new settings group = new case + new switch arm at :105**. SettingsCard :532; settingsField :368; SettingsInfoRow :583.
- **No Toggle exists anywhere in the client.** Patterns: Pickers bound to AppearancePreferences (:291-310); destructive Buttons (:356, :407).
- Preference storage precedents: AppearancePreferences (Views/AppTheme.swift:84-101 — ObservableObject, @Published didSet → injected UserDefaults; @StateObject in VeetbotApp:6, @EnvironmentObject in settings :34) OR ConnectionConfigurationStore actor. @AppStorage unused.
- Feature gating precedents: model.isConfigured conditionals (:352); #if os(iOS/macOS); if #available(iOS 16.0, macOS 13.0, *) (RootView.swift:88 etc.). Settings presented inline / .sheet on iOS (RootView:43-48, presentSettings :110-114) / AppKit window on macOS (:499-558). Accessibility ids like "website-access.origin" (:167).

## 7. Extensions / App Intents / entitlements / bundle IDs
- **No app extensions; no App Intents/Intents/SiriKit/MessageUI usage anywhere. Zero AppIntent/MFMessageComposeViewController/sms hits.**
- Entitlements: Veetbot/Veetbot.entitlements (iOS: aps-environment $(APS_ENVIRONMENT); keychain-access-groups [$(AppIdentifierPrefix)com.veetbot.apple]); Veetbot.macOS.entitlements (com.apple.developer.aps-environment; same group). Selected per-SDK pbxproj:494-495/:537-538. **No app groups; no sandbox/hardened-runtime keys.**
- Keychain: KeychainTokenStore.makeBaseQuery :161-168 and InstallationIdentityStore :55-65 — kSecClassGenericPassword, service/account, kSecAttrSynchronizable false, kSecUseDataProtectionKeychain true; writes add kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly (:137 / :38). **kSecAttrAccessGroup NOT set** (entitlement declares the group but queries don't use it). Services com.veetbot.client.bearer-token/static-bearer-token, com.veetbot.client.device-identity/installation-id. AfterFirstUnlock ⇒ background intent can read post-first-unlock.
- Bundle ids: app com.veetbot.apple (:520/:562), UITests com.veetbot.apple.UITests (:589/:615).
- README documents owner-only Apple Developer portal actions (push capability, provisioning) — same constraint for any new capability.

## 8. Tests
- Unit: Tests/VeetbotCoreTests/ (14 files), Swift Testing (@Suite/@Test/#expect/#require), @testable import VeetbotCore; run via make test-apple. NotificationCoordinatorTests.swift is the M20 registration precedent: @Suite(.serialized) :7; FakeDeviceRegistrationAPI actor :279-323; DeviceView.fixture :325+. **Adding capabilities to AppleDeviceRegistration touches DeviceView.fixture and HTTPTransportTests.swift:747.**
- **Two tests assert on repo files as text**: testInstallationIdentityUsesTheLocalDataProtectionKeychain :186-195; testPlatformSpecificEntitlementsAreTrackedAndUITestFixtureSuppressesPermissionPrompt :197-258 (asserts exact entitlement pairs; each CODE_SIGN_ENTITLEMENTS line appears EXACTLY TWICE in pbxproj :245-255; delegate file contains the launch arg + requestAuthorization :256-257). **Any new target/entitlement breaks this test unless updated.**
- UI: VeetbotUITests/ConversationNavigationUITests.swift, XCTest, 5 tests; launch arg in setUp :10. New UI-test file = manual pbxproj Sources-phase edit (500000000000000000000005, one entry today).
- UI fixture: Veetbot/ConversationNavigationUITestFixture.swift (#if DEBUG && os(iOS), excluded from SPM) — ChatViewModel with isolated UserDefaults suite, InMemoryTokenStore, stub URLProtocol switch-matching (method, path) → canned JSON (:52+). **The mechanism for exercising new routes/settings in UI tests without a server (iOS-only).**

## M20 gaps (all greenfield)
- capabilities field in AppleDeviceRegistration/DeviceView + coordinator digest ride-along.
- UNNotificationCategory/action registration + actionIdentifier branching + new push kind in NotificationKind + the three payload tables.
- MessageUI compose sheet (UIViewControllerRepresentable; iOS-only guards).
- App Intent (AppIntents framework, iOS 16+; SMS feature gates on iOS 17 anyway) — an IN-APP intent (no extension target) avoids app groups and keychain sharing entirely; keychain access is the app's own.
- Pending-invocation fetch + result post + ingest routes on VeetbotAPIClient.
- A Toggle-based "SMS integration" settings section (first Toggle in the app).
