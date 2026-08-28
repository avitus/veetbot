import Testing

@testable import VeetbotCore

@Suite struct ConnectionSettingsLayoutTests {
    @Test
    func testSettingsAreGroupedByUserIntent() {
        #expect(
            ConnectionSettingsSection.allCases == [
                .connection,
                .websiteAccess,
                .appearance,
                .dataAndPrivacy,
            ]
        )
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
}
#endif
