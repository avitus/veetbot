import Testing

@testable import VeetbotCore

@Suite struct ConnectionSettingsLayoutTests {
    @Test
    func testSettingsAreGroupedByUserIntent() {
        #expect(
            ConnectionSettingsSection.allCases == [
                .connection,
                .models,
                .websiteAccess,
                .smsIntegration,
                .appearance,
                .dataAndPrivacy,
            ]
        )
    }

    @Test
    func testSmsIntegrationIsOfferedOnlyWhereTheDeviceCanSendTexts() {
        #if os(iOS)
        #expect(ConnectionSettingsSection.visibleCases.contains(.smsIntegration))
        #expect(ConnectionSettingsSection.visibleCases == ConnectionSettingsSection.allCases)
        #else
        #expect(!ConnectionSettingsSection.visibleCases.contains(.smsIntegration))
        #expect(
            ConnectionSettingsSection.visibleCases == [
                .connection,
                .models,
                .websiteAccess,
                .appearance,
                .dataAndPrivacy,
            ]
        )
        #endif
    }

    @Test
    func testBuildIdentityMakesInstalledClientVersionVisible() {
        let identity = ClientBuildIdentity(
            infoDictionary: [
                "CFBundleShortVersionString": "0.1.1",
                "CFBundleVersion": "2",
            ]
        )

        #expect(identity.displayName == "Version 0.1.1 (2)")
    }

    @Test
    func testConnectionActionIsScopedToConnectionCard() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent(
                "Veetbot/Views/ConnectionSettingsView.swift"
            ),
            encoding: .utf8
        )
        let connectionStart = try #require(source.range(of: "case .connection:"))
        let websiteAccessStart = try #require(source.range(of: "case .websiteAccess:"))
        let actionBarStart = try #require(source.range(of: "private var actionBar:"))
        let closeStart = try #require(source.range(of: "private func close()"))
        let connectionSection = source[connectionStart.lowerBound..<websiteAccessStart.lowerBound]
        let actionBar = source[actionBarStart.lowerBound..<closeStart.lowerBound]

        #expect(connectionSection.contains("connectionAction"))
        #expect(!actionBar.contains("saveConnection()"))
    }

    // MARK: - Website Access (ADR-0128 D12, CS13)

    private func settingsSource() throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/ConnectionSettingsView.swift"),
            encoding: .utf8
        )
    }

    private func websiteAccessSection() throws -> Substring {
        let source = try settingsSource()
        let start = try #require(source.range(of: "case .websiteAccess:"))
        let end = try #require(source.range(of: "case .smsIntegration:"))
        return source[start.lowerBound..<end.lowerBound]
    }

    @Test
    func testSignInOnThisDeviceIsThePrimaryWebsiteAccessAction() throws {
        let section = try websiteAccessSection()
        let device = try #require(section.range(of: "Label(\"Sign in on this device\""))
        let remote = try #require(section.range(of: "Label(\"Use Veetbot's remote browser\""))

        #expect(device.lowerBound < remote.lowerBound)
        let deviceButton = section[device.lowerBound..<remote.lowerBound]
        #expect(deviceButton.contains(".buttonStyle(.borderedProminent)"))
        #expect(deviceButton.contains(".accessibilityIdentifier(\"website-access.sign-in-on-device\")"))
        let remoteButton = section[remote.lowerBound...]
        #expect(remoteButton.contains(".buttonStyle(.bordered)"))
        #expect(remoteButton.contains(".accessibilityIdentifier(\"website-access.remote-browser\")"))
        #expect(!section.contains("Create secure login"))
    }

    @Test
    func testTheNotReadyGuardAppliesToBothSignInModes() throws {
        let source = try settingsSource()
        let section = try websiteAccessSection()

        #expect(source.contains("private var websiteAccessBlocked: Bool"))
        #expect(source.contains("(model.browserAuthentication.map { $0.status != .ready } ?? false)"))
        #expect(section.components(separatedBy: ".disabled(websiteAccessBlocked)").count == 3)
    }

    @Test(arguments: [
        (BrowserProfileStatus.provisioning, true),
        (.authenticationRequired, true),
        (.ready, true),
        (.needsUser, true),
        (.revoked, false),
    ])
    func testSignInAgainIsOfferedOnlyOnProfilesThatAreNotRevoked(
        status: BrowserProfileStatus, offered: Bool
    ) throws {
        #expect(WebsiteAccessActions.offersSignInAgain(status) == offered)
        let source = try settingsSource()
        #expect(source.contains("if WebsiteAccessActions.offersSignInAgain(profile.status) {"))
        #expect(source.contains("model.beginDeviceSignIn(profile: profile)"))
        #expect(source.contains("Button(\"Sign in again\")"))
    }

    @Test
    func testTaskPermissionsIsACompactMenuRowAtTheEndOfWebsiteAccess() throws {
        let section = try websiteAccessSection()
        let rows = try #require(section.range(of: "ForEach(model.browserProfiles)"))
        let permissions = try #require(section.range(of: "if !model.activeTaskGrants.isEmpty {"))

        #expect(rows.lowerBound < permissions.lowerBound)
        let row = section[permissions.lowerBound...]
        #expect(row.contains("Text(\"Task permissions\")"))
        #expect(row.contains("Menu {"))
        #expect(row.contains("await model.stopTaskGrant(grant.id)"))
        #expect(row.contains(".accessibilityIdentifier(\"website-access.task-permissions\")"))
        let source = try settingsSource()
        #expect(source.contains("await model.refreshTaskPermissions()"))
    }

    @Test
    func testWebsiteAccessAddsNoListSectionAboveAssertedRows() throws {
        let section = try websiteAccessSection()

        #expect(!section.contains("List {"))
        #expect(!section.contains("List("))
        #expect(!section.contains("Section {"))
        #expect(!section.contains("Section("))
    }

    @Test
    func testTheSignInWindowIsPresentedFromSettingsAndRemoteCeremoniesRefreshOnActivation() throws {
        let source = try settingsSource()

        #expect(source.contains(".deviceSignInPresentation(model: model)"))
        #expect(source.contains("@Environment(\\.scenePhase) private var scenePhase"))
        #expect(source.contains("if phase == .active {"))
        #expect(source.contains("await model.refreshOpenRemoteAuthentication()"))
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let sheet = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/DeviceSignInSheet.swift"),
            encoding: .utf8
        )
        #expect(sheet.contains(".fullScreenCover(item:"))
        #expect(sheet.contains("userInterfaceIdiom == .phone"))
        #expect(sheet.contains(".frame(minWidth: 820, minHeight: 680)"))
        #expect(sheet.contains("Button(\"I'm signed in\")"))
        #expect(sheet.contains("DeviceSignInNavigationPolicy.canConfirm("))
        #expect(sheet.contains("DeviceSignInNavigationPolicy.decide("))
        #expect(sheet.contains("navigationResponse.canShowMIMEType"))
        #expect(sheet.contains("createWebViewWith configuration"))
        #expect(sheet.contains("contentWorld: .defaultClient"))
        #expect(sheet.contains("removeData(\n") || sheet.contains("removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes()"))
        #expect(sheet.contains(
            "const o=[];for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o.push([k,localStorage.getItem(k)]);}return JSON.stringify(o);"
        ))
        #expect(sheet.contains("await model.completeDeviceSignIn("))
        #expect(!sheet.contains("websiteAuthenticationLaunchURL"))
        #expect(!sheet.contains("@SceneStorage"))
        #expect(!sheet.contains("@AppStorage"))
    }

    /// While I'm signed in is being checked, Cancel is disabled, and a swipe
    /// on the iPad sheet must not close the window either; a success closes
    /// only the window it belongs to.
    @Test
    func testTheSignInWindowStaysOpenWhileItChecksTheSignIn() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let sheet = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/DeviceSignInSheet.swift"),
            encoding: .utf8
        )

        #expect(sheet.contains(".disabled(isConfirming)"))
        #expect(sheet.contains(".interactiveDismissDisabled(isConfirming)"))
        #expect(sheet.contains("model.finishDeviceSignIn(request)"))
    }
}

#if os(macOS)
import AppKit

private final class FrameAutosaveIgnoringWindow: NSWindow {
    override func saveFrame(usingName name: NSWindow.FrameAutosaveName) {}
}

@Suite(.serialized) struct SettingsWindowConfigurationTests {
    @Test
    func testAppWindowUsesAStableNamedSceneRoot() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/VeetbotApp.swift"),
            encoding: .utf8
        )
        let normalizedSource = source
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")

        #expect(normalizedSource.contains("WindowGroup { VeetbotSceneRoot("))
        #expect(!normalizedSource.contains("WindowGroup { RootView("))
        let rootTypeName = String(reflecting: VeetbotSceneRoot.self)
        #expect(rootTypeName.hasSuffix(".VeetbotSceneRoot"))
        #expect(!rootTypeName.contains("unknown context"))
    }

    @Test
    func testMainAndSettingsWindowsHaveDistinctPersistentFrames() {
        #expect(MainWindowConfiguration.frameName == "VeetbotMainWindow")
        #expect(SettingsWindowConfiguration.frameName == "VeetbotSettingsWindow")
        #expect(MainWindowConfiguration.frameName != SettingsWindowConfiguration.frameName)
    }

    @Test
    func testSettingsPresenterAttachesExplicitResizePersistence() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/RootView.swift"),
            encoding: .utf8
        )
        let normalizedSource = source
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")

        #expect(
            normalizedSource.contains(
                "resizePersistence = PopupWindowResizePersistence( window: window, key: SettingsWindowConfiguration.storageKey )"
            )
        )
    }

    @Test @MainActor
    func testMainWindowInstallsItsFrameAutosaveName() {
        let window = FrameAutosaveIgnoringWindow(
            contentRect: NSRect(x: 80, y: 80, width: 900, height: 640),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )

        MainWindowConfiguration.apply(to: window)

        #expect(window.frameAutosaveName == MainWindowConfiguration.frameName)
    }

    @Test @MainActor
    func testMainWindowPersistsWhenSwiftUIOwnsAppKitAutosave() async {
        let swiftUIFrameName: NSWindow.FrameAutosaveName =
            "SwiftUI.Test.MainWindow.\(UUID().uuidString)"
        UserDefaults.standard.removeObject(forKey: MainWindowConfiguration.storageKey)
        NSWindow.removeFrame(usingName: MainWindowConfiguration.frameName)
        NSWindow.removeFrame(usingName: swiftUIFrameName)
        defer {
            UserDefaults.standard.removeObject(forKey: MainWindowConfiguration.storageKey)
            NSWindow.removeFrame(usingName: MainWindowConfiguration.frameName)
            NSWindow.removeFrame(usingName: swiftUIFrameName)
        }

        let savedFrame = NSRect(x: 120, y: 140, width: 1_180, height: 760)
        let seedWindow = NSWindow(
            contentRect: savedFrame,
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        seedWindow.setFrame(savedFrame, display: false)
        MainWindowConfiguration.saveFrame(of: seedWindow)

        let swiftUIDefaultFrame = NSRect(x: 40, y: 60, width: 900, height: 592)
        let launchedWindow = FrameAutosaveIgnoringWindow(
            contentRect: swiftUIDefaultFrame,
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )

        MainWindowConfiguration.apply(to: launchedWindow)
        launchedWindow.setFrameAutosaveName(swiftUIFrameName)
        launchedWindow.setFrame(swiftUIDefaultFrame, display: false)
        NotificationCenter.default.post(
            name: NSWindow.didBecomeMainNotification,
            object: launchedWindow
        )
        await withCheckedContinuation { continuation in
            DispatchQueue.main.async { continuation.resume() }
        }

        #expect(launchedWindow.frame == savedFrame)
        #expect(launchedWindow.frameAutosaveName == MainWindowConfiguration.frameName)

        let resizedFrame = NSRect(x: 160, y: 180, width: 1_240, height: 820)
        launchedWindow.setFrameAutosaveName(swiftUIFrameName)
        launchedWindow.setFrame(resizedFrame, display: false)
        NotificationCenter.default.post(
            name: NSWindow.didResizeNotification,
            object: launchedWindow
        )

        let relaunchedWindow = NSWindow(
            contentRect: NSRect(x: 20, y: 20, width: 780, height: 560),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        MainWindowConfiguration.apply(to: relaunchedWindow)

        #expect(relaunchedWindow.frame == resizedFrame)
    }

    @Test @MainActor
    func testWindowCanGrowHorizontallyAndVertically() {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: SettingsWindowConfiguration.initialSize),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )

        SettingsWindowConfiguration.apply(to: window)

        #expect(window.styleMask.contains(.resizable))
        #expect(window.contentMinSize.width < SettingsWindowConfiguration.initialSize.width)
        #expect(window.contentMinSize.height < SettingsWindowConfiguration.initialSize.height)
        #expect(window.contentMaxSize.width > SettingsWindowConfiguration.initialSize.width)
        #expect(window.contentMaxSize.height > SettingsWindowConfiguration.initialSize.height)
    }

    @Test @MainActor
    func testEveryPopupRestoresItsOwnPersistedContentSize() {
        let defaults = UserDefaults.standard
        let settingsKey = "veetbot.settingsWindow.contentSize"
        let memoryKey = "veetbot.memoryBrowser.contentSize"
        let scheduleKey = "veetbot.scheduleBrowser.contentSize"
        let settingsSize = NSSize(width: 811, height: 733)
        let memorySize = NSSize(width: 877, height: 699)
        let scheduleSize = NSSize(width: 923, height: 741)

        for (key, size) in [
            (settingsKey, settingsSize),
            (memoryKey, memorySize),
            (scheduleKey, scheduleSize),
        ] {
            defaults.set(NSStringFromSize(size), forKey: key)
        }
        defer {
            for key in [settingsKey, memoryKey, scheduleKey] {
                defaults.removeObject(forKey: key)
            }
        }

        let settingsWindow = popupWindow()
        let memoryWindow = popupWindow()
        let scheduleWindow = popupWindow()

        SettingsWindowConfiguration.apply(to: settingsWindow)
        MemoryBrowserWindowConfiguration.apply(to: memoryWindow)
        ScheduleBrowserWindowConfiguration.apply(to: scheduleWindow)

        #expect(settingsWindow.contentView?.frame.size == settingsSize)
        #expect(memoryWindow.contentView?.frame.size == memorySize)
        #expect(scheduleWindow.contentView?.frame.size == scheduleSize)
    }

    @Test @MainActor
    func testPopupSizeStoreRecordsTheCurrentContentSize() throws {
        let key = "veetbot.test.popup.contentSize.\(UUID().uuidString)"
        let defaults = UserDefaults.standard
        let size = NSSize(width: 849, height: 707)
        defer { defaults.removeObject(forKey: key) }
        let window = popupWindow()
        window.setContentSize(size)

        PopupWindowContentSizeStore.save(window: window, key: key)

        let stored = try #require(defaults.string(forKey: key))
        #expect(NSSizeFromString(stored) == size)
    }

    @Test @MainActor
    func testResizePersistenceSavesAtTheEndOfAUserResize() throws {
        let key = "veetbot.test.popup.liveResize.\(UUID().uuidString)"
        let defaults = UserDefaults.standard
        let size = NSSize(width: 901, height: 759)
        defer { defaults.removeObject(forKey: key) }
        let window = popupWindow()
        let persistence = PopupWindowResizePersistence(window: window, key: key)
        window.setContentSize(size)

        NotificationCenter.default.post(
            name: NSWindow.didEndLiveResizeNotification,
            object: window
        )

        let stored = try #require(defaults.string(forKey: key))
        #expect(NSSizeFromString(stored) == size)
        _ = persistence
    }

    @MainActor
    private func popupWindow() -> NSWindow {
        NSWindow(
            contentRect: NSRect(x: 40, y: 60, width: 600, height: 540),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
    }
}
#endif
